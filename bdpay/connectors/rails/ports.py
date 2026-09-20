"""Narrow local ports consumed by the rail adapters (spec/11 NOT-owned seams).

Surfaces owned by other specs are consumed through Protocols with
deterministic in-memory defaults, mirroring ``bdpay.connectors.ports``:

- MAC generation/verification belongs to ``hsm_thales_v1`` (spec/12); rails
  own only the call boundary, so :class:`MacService` is injected. The default
  :class:`HmacMacService` is the SIMULATOR/SANDBOX implementation (HMAC-SHA256
  over the dictionary-defined preimage); PRODUCTION wiring injects the
  HSM-backed implementation. Fail-closed: any MAC error refuses the message.
- PAN/account detokenization belongs to the vault (spec/14); BEFTN
  detokenize-at-render and the card vault-proxy consume :class:`Detokenizer` /
  :class:`VaultProxy` ports. The connector process never holds a clear PAN.
- Session windows / working-day arithmetic belong to ``platform.scheduler``
  (spec/02, built by lane A); :class:`BdCalendar` is the deterministic local
  default (Asia/Dhaka is a fixed UTC+6 zone — Bangladesh has no DST; BD
  weekend is Friday+Saturday) to be swapped for the platform scheduler
  calendar at integration.
"""

from __future__ import annotations

import hmac
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
from typing import Protocol, runtime_checkable

__all__ = [
    "BD_WEEKEND",
    "BEFTN_SESSIONS",
    "BdCalendar",
    "DHAKA_TZ",
    "Detokenizer",
    "HmacMacService",
    "MacService",
    "StaticDetokenizer",
    "VaultProxy",
]

#: Bangladesh standard time — fixed UTC+6, no DST (deterministic, no tzdata).
DHAKA_TZ = timezone(timedelta(hours=6), "Asia/Dhaka")

#: BD weekend: Friday (4) and Saturday (5) per datetime.weekday().
BD_WEEKEND = frozenset({4, 5})

#: BEFTN session codes (spec/11 — 3 sessions/day).
BEFTN_SESSIONS = ("MORNING", "AFTERNOON", "EOD")


@runtime_checkable
class MacService(Protocol):
    """ISO 8583 MAC boundary (spec/12 ``hsm_thales_v1`` owns the real one)."""

    def generate_mac(self, preimage: bytes) -> str: ...

    def verify_mac(self, preimage: bytes, mac: str) -> bool: ...


class HmacMacService:
    """HMAC-SHA256 MAC service — SIMULATOR/SANDBOX implementation.

    16 uppercase hex chars (DE64 ``ascii_hex`` FIXED 16). Constant-time
    comparison; verification never raises (False on any anomaly, fail-closed).
    """

    def __init__(self, key: bytes) -> None:
        if not isinstance(key, bytes) or not key:
            raise ValueError("MAC key must be non-empty bytes")
        self._key = key

    def generate_mac(self, preimage: bytes) -> str:
        return hmac.new(self._key, preimage, sha256).hexdigest()[:16].upper()

    def verify_mac(self, preimage: bytes, mac: str) -> bool:
        if not isinstance(mac, str) or len(mac) != 16:
            return False
        return hmac.compare_digest(self.generate_mac(preimage), mac.upper())


@runtime_checkable
class Detokenizer(Protocol):
    """Vault detokenize-at-render seam (spec/14). Raises on unknown token."""

    def detokenize(self, token: str) -> str: ...


class StaticDetokenizer:
    """Deterministic token -> account map for SIMULATOR mode and tests."""

    def __init__(self, accounts: dict[str, str] | None = None) -> None:
        self._accounts = dict(accounts or {})

    def add(self, token: str, account: str) -> None:
        self._accounts[token] = account

    def detokenize(self, token: str) -> str:
        if token not in self._accounts:
            raise LookupError(f"unresolvable account token (vault miss): {token!r}")
        return self._accounts[token]


@runtime_checkable
class VaultProxy(Protocol):
    """Card vault-proxy port (spec/14): detokenize-and-forward inside the CDE.

    The adapter hands the assembled request plus the vault token; the proxy
    detokenizes and forwards to the acquirer host, returning the acquirer
    response dict. The connector process never sees the PAN.
    """

    async def detokenize_and_forward(
        self, pan_token: str, operation: str, payload: dict
    ) -> dict: ...


class BdCalendar:
    """Deterministic BD working-day calendar + BEFTN cutoff arithmetic.

    Default holiday set is empty (BB holiday table is deployment config via
    the platform scheduler at integration); the working-day rule (Fri/Sat
    weekend) and the 12:30 Asia/Dhaka same-day cutoff are structural.
    """

    CUTOFF_HOUR = 12
    CUTOFF_MINUTE = 30

    def __init__(self, holidays: frozenset[date] | None = None) -> None:
        self._holidays = holidays or frozenset()

    def is_working_day(self, day: date) -> bool:
        return day.weekday() not in BD_WEEKEND and day not in self._holidays

    def add_working_days(self, start: date, days: int) -> date:
        if days < 0:
            raise ValueError("days must be >= 0")
        day = start
        if days == 0:
            while not self.is_working_day(day):
                day = day + timedelta(days=1)
            return day
        remaining = days
        while remaining > 0:
            day = day + timedelta(days=1)
            if self.is_working_day(day):
                remaining -= 1
        return day

    def dhaka_local(self, now: datetime) -> datetime:
        if now.tzinfo is None:
            raise ValueError("naive datetimes are rejected (spec/00 §7)")
        return now.astimezone(DHAKA_TZ)

    def before_cutoff(self, now: datetime) -> bool:
        """True while a same-day BEFTN submission is still allowed (12:30 BST)."""
        local = self.dhaka_local(now)
        return (local.hour, local.minute) < (self.CUTOFF_HOUR, self.CUTOFF_MINUTE)

    def effective_date(self, now: datetime, *, high_value: bool = False) -> date:
        """T+0 high-value / same-day before cutoff, else next working day."""
        local = self.dhaka_local(now)
        if high_value or self.before_cutoff(now):
            return self.add_working_days(local.date(), 0)
        return self.add_working_days(local.date(), 1)

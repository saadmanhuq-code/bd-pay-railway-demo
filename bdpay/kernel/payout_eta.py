"""payout_expected_by / deferred_until — spec/16 §G read-surface fields.

``GET /v1/payment-intents/{id}`` gains two nullable COMPUTED fields (additive;
no schema break, no stored-state change):

- ``payout_expected_by`` — populated once the intent has a charged/succeeded
  attempt. When a :class:`SettlementInstruction` already exists, the value IS
  the instruction's ``earliest_release_at`` (spec/04 owns that engine);
  revised to ``null`` while the instruction's holding batch is ``HELD``.
  Before instruction creation, the value is an estimate from the merchant's
  settlement cycle + the spec/04 calendar (errata E-S16-07: the pre-
  instruction estimate is the spec/04 ``calculate_earliest_release`` formula;
  for the no-hold ``OTHER`` MCC category the expected payout instant is the
  start of the next working day's settlement cycle — 09:00 Dhaka, the spec/04
  release instant — because "the next available batch" pays out on the next
  cycle date, not at the charge instant).
- ``deferred_until`` — the kernel's scheduled redispatch instant while the
  intent sits in store-and-forward deferral (persisted by the orchestrator,
  spec/02); ``null`` otherwise.

Wire values are RFC3339 UTC strings (E12 form). The enrichment is read-only:
it never mutates the intent row.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from bdpay.ledger.calendar_ext import add_working_days, business_day_start_utc
from bdpay.platform.scheduler import DHAKA_TZ, BangladeshBankCalendar

__all__ = [
    "PayoutEtaService",
    "SettlementVisibilityPort",
    "estimate_payout_expected_by",
]

#: Attempt/intent states that mean money was captured (spec/02).
_CHARGED_ATTEMPT_STATES = frozenset({"CHARGED"})
_CHARGED_INTENT_STATES = frozenset({"SUCCEEDED", "REFUND_INITIATED", "PARTIALLY_REFUNDED"})

#: spec/04 MCC hold categories (mirrors settlement engine vocabulary —
#: ``MCC_CATEGORIES = {"DAILY_ESSENTIAL", "DIRECT_MERCHANT", "OTHER"}``).
_HOLD_DAYS = {"DAILY_ESSENTIAL": 5, "DIRECT_MERCHANT": 7}


def _rfc3339(at: datetime) -> str:
    return at.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@runtime_checkable
class SettlementVisibilityPort(Protocol):
    """Narrow read view over spec/04 settlement state for the §G fields.

    Implementations live with the settlement store. Both methods are pure
    reads; absence of data returns ``None`` (the estimator then takes over).
    """

    def earliest_release_for_intent(self, payment_intent_id: str) -> datetime | None:
        """``settlement_instructions.earliest_release_at`` or ``None``."""
        ...

    def holding_batch_is_held(self, payment_intent_id: str) -> bool:
        """True when the intent's instruction sits in a ``HELD`` batch."""
        ...


def estimate_payout_expected_by(
    calendar: BangladeshBankCalendar,
    *,
    charged_at: datetime,
    mcc_category: str = "OTHER",
    delivery_confirmed_at: datetime | None = None,
) -> datetime:
    """Pre-instruction estimate (spec/04 formula; errata E-S16-07).

    Mirrors the settlement engine's ``calculate_earliest_release`` semantics
    with one read-surface adjustment: the no-hold ``OTHER`` category maps to
    the start of the NEXT working day's cycle (a merchant cannot be paid
    before the next batch runs), where the engine's internal form returns the
    charge instant to mean "next available batch".
    """
    local_date = charged_at.astimezone(DHAKA_TZ).date()
    five_wd = add_working_days(calendar, local_date, 5)
    if mcc_category == "OTHER":
        return business_day_start_utc(add_working_days(calendar, local_date, 1))
    hold_days = _HOLD_DAYS.get(mcc_category)
    if hold_days is None:
        raise ValueError(f"unknown mcc_category {mcc_category!r}")
    if delivery_confirmed_at is not None:
        hold_end = add_working_days(
            calendar, delivery_confirmed_at.astimezone(DHAKA_TZ).date(), 0
        )
    else:
        hold_end = add_working_days(calendar, local_date, hold_days)
    return business_day_start_utc(min(hold_end, five_wd))


class PayoutEtaService:
    """Computes the two spec/16 §G fields for one intent read."""

    def __init__(
        self,
        calendar: BangladeshBankCalendar | None = None,
        settlement: SettlementVisibilityPort | None = None,
    ) -> None:
        self._calendar = calendar or BangladeshBankCalendar()
        self._settlement = settlement

    def payout_expected_by(
        self,
        *,
        payment_intent_id: str,
        intent_status: str,
        charged_at: datetime | None,
        attempt_states: tuple[str, ...] = (),
        mcc_category: str = "OTHER",
        delivery_confirmed_at: datetime | None = None,
    ) -> datetime | None:
        """spec/16 §G derivation, most-authoritative source first."""
        charged = (
            intent_status in _CHARGED_INTENT_STATES
            or any(state in _CHARGED_ATTEMPT_STATES for state in attempt_states)
        )
        if not charged or charged_at is None:
            return None
        if self._settlement is not None:
            if self._settlement.holding_batch_is_held(payment_intent_id):
                return None  # revised to null while the holding batch is HELD
            release_at = self._settlement.earliest_release_for_intent(payment_intent_id)
            if release_at is not None:
                return release_at
        return estimate_payout_expected_by(
            self._calendar,
            charged_at=charged_at,
            mcc_category=mcc_category,
            delivery_confirmed_at=delivery_confirmed_at,
        )

    def enrich_intent_view(
        self, intent: dict[str, Any], *, attempt_states: tuple[str, ...] = ()
    ) -> dict[str, Any]:
        """Add the two §G fields (RFC3339 strings or None) to an intent dict.

        ``intent`` is the ``dataclasses.asdict`` form of a
        :class:`~bdpay.kernel.models.PaymentIntentRecord` (or an equivalent
        mapping). The input dict is not mutated.
        """
        out = dict(intent)
        deferred_until = out.get("deferred_until")
        out["deferred_until"] = (
            _rfc3339(deferred_until) if isinstance(deferred_until, datetime) else None
        )
        expected = self.payout_expected_by(
            payment_intent_id=str(out.get("payment_intent_id", "")),
            intent_status=str(out.get("status", "")),
            charged_at=(
                out.get("succeeded_at")
                if isinstance(out.get("succeeded_at"), datetime)
                else None
            ),
            attempt_states=attempt_states,
            delivery_confirmed_at=(
                out.get("delivery_confirmed_at")
                if isinstance(out.get("delivery_confirmed_at"), datetime)
                else None
            ),
        )
        out["payout_expected_by"] = _rfc3339(expected) if expected is not None else None
        return out

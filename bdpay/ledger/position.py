"""PSO-mode position accounts, Net Debit Caps, SettlementWindow FSM (spec/05).

Mode gate (binding): every operation here REFUSES when ``PLATFORM_MODE`` is
not ``PSO`` (``PsoModeError``). The authoritative Net-Debit-Cap enforcement
is the database trigger in db/migrations/0012_pso_position.sql; this module
is its unit-testable application-side mirror — identical decision logic
(advisory check first for user-facing messages, the DB trigger is the safety
net no code path can bypass).

SettlementWindow FSM is the binding spec/05 FSM 1 transition table,
refusal-first: any (state, trigger) pair not in the table is DENIED.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, Protocol

import psycopg

from bdpay.ledger.errors import (
    NdcBreachError,
    NdcError,
    NdcNotConfiguredError,
    NoOpenWindowError,
    PsoModeError,
    WindowInvalidTransitionError,
)
from bdpay.platform.clock import Clock
from bdpay.platform.config import Settings
from bdpay.platform.ids import make_id

__all__ = [
    "WINDOW_TERMINAL_STATES",
    "WINDOW_TRANSITIONS",
    "InMemoryPositionStore",
    "NetDebitCapRow",
    "NdcBreachLogRow",
    "PositionAccountRow",
    "PositionService",
    "PostgresPositionStore",
    "SettlementWindowRow",
]

# --- SettlementWindow FSM (spec/05 FSM 1) ------------------------------------

WINDOW_STATES: frozenset[str] = frozenset(
    {
        "WINDOW_OPEN",
        "ACCUMULATING_POSITIONS",
        "CUTOFF_REACHED",
        "NETTING_CALCULATED",
        "WINDOW_BLOCKED",
        "SETTLEMENT_INSTRUCTED",
        "SETTLEMENT_CONFIRMED",
        "RECONCILIATION_RUNNING",
        "RECONCILIATION_EXCEPTION",
        "WINDOW_CLOSED",
        "SETTLEMENT_FAILED",
    }
)
WINDOW_TERMINAL_STATES: frozenset[str] = frozenset({"WINDOW_CLOSED"})

WINDOW_TRANSITIONS: dict[tuple[str, str], tuple[str, ...]] = {
    ("WINDOW_OPEN", "window_open_tick"): ("ACCUMULATING_POSITIONS",),
    ("ACCUMULATING_POSITIONS", "cutoff_reached"): ("CUTOFF_REACHED",),
    ("ACCUMULATING_POSITIONS", "force_close"): ("CUTOFF_REACHED",),
    ("CUTOFF_REACHED", "netting_computed"): ("NETTING_CALCULATED",),
    ("NETTING_CALCULATED", "tcsa_shortfall_detected"): ("WINDOW_BLOCKED",),
    ("NETTING_CALCULATED", "tcsa_ok"): ("SETTLEMENT_INSTRUCTED",),
    ("WINDOW_BLOCKED", "tcsa_shortfall_resolved"): ("SETTLEMENT_INSTRUCTED",),
    ("WINDOW_BLOCKED", "admin_override_shortfall"): ("SETTLEMENT_INSTRUCTED",),
    ("SETTLEMENT_INSTRUCTED", "settlement_confirmed"): ("SETTLEMENT_CONFIRMED",),
    ("SETTLEMENT_INSTRUCTED", "settlement_rejected"): ("SETTLEMENT_FAILED",),
    ("SETTLEMENT_INSTRUCTED", "settlement_timeout"): ("SETTLEMENT_FAILED",),
    ("SETTLEMENT_CONFIRMED", "reconciliation_started"): ("RECONCILIATION_RUNNING",),
    ("RECONCILIATION_RUNNING", "recon_matched"): ("WINDOW_CLOSED",),
    ("RECONCILIATION_RUNNING", "recon_exception"): ("RECONCILIATION_EXCEPTION",),
    ("RECONCILIATION_EXCEPTION", "exception_resolved"): ("WINDOW_CLOSED",),
    ("SETTLEMENT_FAILED", "settlement_retry"): ("SETTLEMENT_INSTRUCTED",),
    # --- spec/19 FSM 4: additive rows ONLY (spec/05 stays authoritative) ---
    # Participant suspended mid-window: no state change; postings already
    # accepted REMAIN (accepted-is-final); the D-19 trigger guard refuses
    # further postings for that participant (bdpay.ledger.netting).
    ("ACCUMULATING_POSITIONS", "participant_suspended"): ("ACCUMULATING_POSITIONS",),
    # D-19-2 unwind: two-eyes approved; excluded participant's entries are
    # reversed by offsetting entries and netting re-fires over the survivors.
    ("SETTLEMENT_FAILED", "unwind_and_recompute"): ("CUTOFF_REACHED",),
}


@dataclass(frozen=True, slots=True)
class SettlementWindowRow:
    window_id: str
    dns_session: str
    window_date: date
    status: str
    opened_at: datetime
    cutoff_at: datetime | None = None
    netting_completed_at: datetime | None = None
    instructed_at: datetime | None = None
    confirmed_at: datetime | None = None
    closed_at: datetime | None = None
    net_settlement_amount_minor: int | None = None
    currency: str = "BDT"
    block_reason: str | None = None
    blocked_at: datetime | None = None
    unblocked_at: datetime | None = None
    shortfall_override: bool = False
    shortfall_override_approval_id: str | None = None
    settlement_attempt_number: int = 1
    settlement_batch_id: str | None = None
    schema_version: int = 1


@dataclass(frozen=True, slots=True)
class PositionAccountRow:
    """One per (participant, settlement window); net position accumulates."""

    position_account_id: str
    participant_id: str
    settlement_window_id: str
    net_position_minor: int  # negative = net debit (participant owes)
    currency: str
    net_debit_cap_minor: int
    debit_count: int
    credit_count: int
    last_movement_at: datetime | None
    window_state: str
    created_at: datetime
    schema_version: int = 1

    @property
    def headroom_minor(self) -> int:
        return self.net_debit_cap_minor - max(0, -self.net_position_minor)


@dataclass(frozen=True, slots=True)
class NetDebitCapRow:
    ndc_id: str
    participant_id: str
    cap_amount_minor: int
    currency: str
    effective_from: datetime
    superseded_at: datetime | None
    approved_by: str
    approval_request_id: str
    rationale: str
    created_at: datetime
    schema_version: int = 1


@dataclass(frozen=True, slots=True)
class NdcBreachLogRow:
    """Append-only forensic row for every blocked debit (spec/05)."""

    breach_id: str
    participant_id: str
    settlement_window_id: str
    position_account_id: str
    attempted_debit_minor: int
    net_position_before_minor: int
    net_debit_cap_minor: int
    headroom_before_minor: int
    blocked_at: datetime
    blocked_posting_entry_id: str | None
    source_payment_id: str | None
    schema_version: int = 1


class PositionStore(Protocol):
    """Persistence boundary for windows, position accounts, caps, breach log."""

    def insert_window(self, row: SettlementWindowRow) -> None: ...

    def get_window(self, window_id: str) -> SettlementWindowRow | None: ...

    def update_window(self, window_id: str, **changes: object) -> SettlementWindowRow: ...

    def find_accumulating_window(self) -> SettlementWindowRow | None: ...

    def insert_position_account(self, row: PositionAccountRow) -> None: ...

    def get_position_account(
        self, participant_id: str, window_id: str
    ) -> PositionAccountRow | None: ...

    def update_position_account(
        self, position_account_id: str, **changes: object
    ) -> PositionAccountRow: ...

    def insert_cap(self, row: NetDebitCapRow) -> None: ...

    def active_cap(self, participant_id: str) -> NetDebitCapRow | None: ...

    def supersede_cap(self, ndc_id: str, superseded_at: datetime) -> None: ...

    def insert_breach(self, row: NdcBreachLogRow) -> None: ...

    def list_breaches(self, participant_id: str | None = None) -> list[NdcBreachLogRow]: ...


_WINDOW_MUTABLE = frozenset(
    {
        "status",
        "cutoff_at",
        "netting_completed_at",
        "instructed_at",
        "confirmed_at",
        "closed_at",
        "net_settlement_amount_minor",
        "block_reason",
        "blocked_at",
        "unblocked_at",
        "shortfall_override",
        "shortfall_override_approval_id",
        "settlement_attempt_number",
        "settlement_batch_id",
    }
)

_POSITION_MUTABLE = frozenset(
    {"net_position_minor", "debit_count", "credit_count", "last_movement_at", "window_state"}
)


class InMemoryPositionStore:
    """Deterministic in-memory store; mirrors the Postgres semantics."""

    def __init__(self) -> None:
        self._windows: dict[str, SettlementWindowRow] = {}
        self._positions: dict[str, PositionAccountRow] = {}
        self._caps: dict[str, NetDebitCapRow] = {}
        self._breaches: list[NdcBreachLogRow] = []

    def insert_window(self, row: SettlementWindowRow) -> None:
        if row.window_id in self._windows:
            raise NdcError(f"duplicate window_id {row.window_id}")
        for existing in self._windows.values():
            if (
                existing.window_date == row.window_date
                and existing.dns_session == row.dns_session
            ):
                raise NdcError("one window per (window_date, dns_session)")
        self._windows[row.window_id] = row

    def get_window(self, window_id: str) -> SettlementWindowRow | None:
        return self._windows.get(window_id)

    def update_window(self, window_id: str, **changes: object) -> SettlementWindowRow:
        row = self._windows.get(window_id)
        if row is None:
            raise NdcError(f"unknown window {window_id}")
        unknown = set(changes) - _WINDOW_MUTABLE
        if unknown:
            raise NdcError(f"non-updatable column(s) {sorted(unknown)}")
        updated = dataclasses.replace(row, **changes)  # type: ignore[arg-type]
        self._windows[window_id] = updated
        return updated

    def find_accumulating_window(self) -> SettlementWindowRow | None:
        for row in sorted(self._windows.values(), key=lambda r: r.opened_at, reverse=True):
            if row.status == "ACCUMULATING_POSITIONS":
                return row
        return None

    def insert_position_account(self, row: PositionAccountRow) -> None:
        if row.position_account_id in self._positions:
            raise NdcError(f"duplicate position account {row.position_account_id}")
        for existing in self._positions.values():
            if (
                existing.participant_id == row.participant_id
                and existing.settlement_window_id == row.settlement_window_id
            ):
                raise NdcError("one position account per (participant, window)")
        self._positions[row.position_account_id] = row

    def get_position_account(
        self, participant_id: str, window_id: str
    ) -> PositionAccountRow | None:
        for row in self._positions.values():
            if row.participant_id == participant_id and row.settlement_window_id == window_id:
                return row
        return None

    def update_position_account(
        self, position_account_id: str, **changes: object
    ) -> PositionAccountRow:
        row = self._positions.get(position_account_id)
        if row is None:
            raise NdcError(f"unknown position account {position_account_id}")
        unknown = set(changes) - _POSITION_MUTABLE
        if unknown:
            raise NdcError(f"non-updatable column(s) {sorted(unknown)}")
        updated = dataclasses.replace(row, **changes)  # type: ignore[arg-type]
        self._positions[position_account_id] = updated
        return updated

    def insert_cap(self, row: NetDebitCapRow) -> None:
        if row.ndc_id in self._caps:
            raise NdcError(f"duplicate ndc_id {row.ndc_id}")
        if any(
            c.participant_id == row.participant_id and c.superseded_at is None
            for c in self._caps.values()
        ):
            raise NdcError(f"participant {row.participant_id} already has an active cap")
        self._caps[row.ndc_id] = row

    def active_cap(self, participant_id: str) -> NetDebitCapRow | None:
        for row in self._caps.values():
            if row.participant_id == participant_id and row.superseded_at is None:
                return row
        return None

    def supersede_cap(self, ndc_id: str, superseded_at: datetime) -> None:
        row = self._caps.get(ndc_id)
        if row is None:
            raise NdcError(f"unknown ndc_id {ndc_id}")
        self._caps[ndc_id] = dataclasses.replace(row, superseded_at=superseded_at)

    def insert_breach(self, row: NdcBreachLogRow) -> None:
        self._breaches.append(row)

    def list_breaches(self, participant_id: str | None = None) -> list[NdcBreachLogRow]:
        return [
            row
            for row in self._breaches
            if participant_id is None or row.participant_id == participant_id
        ]


class PositionService:
    """PSO position-account engine — the application-side NDC mirror.

    Decision logic is IDENTICAL to ``enforce_net_debit_cap()`` in migration
    0012: debit outside an ACCUMULATING_POSITIONS window refused; no active
    cap refused; ``cap - max(0, -(net - debit)) < 0`` refused with a breach
    log row. All refusals fail closed.
    """

    def __init__(self, store: PositionStore, *, clock: Clock, settings: Settings) -> None:
        self._store = store
        self._clock = clock
        self._settings = settings

    def _require_pso(self) -> None:
        if self._settings.platform_mode != "PSO":
            raise PsoModeError(
                f"PSO-only operation refused: PLATFORM_MODE={self._settings.platform_mode!r}"
            )

    # ------------------------------------------------------------------
    # SettlementWindow FSM
    # ------------------------------------------------------------------

    def _window_transition(self, row: SettlementWindowRow, trigger: str, to_state: str) -> None:
        allowed = WINDOW_TRANSITIONS.get((row.status, trigger))
        if allowed is None or to_state not in allowed:
            raise WindowInvalidTransitionError(
                f"window {row.window_id}: {row.status} --[{trigger}]--> {to_state} "
                "is not in the spec/05 FSM 1 table (denied)"
            )

    def open_window(self, window_date: date, dns_session: str) -> SettlementWindowRow:
        self._require_pso()
        now = self._now()
        window_id = make_id(
            "swin", {"window_date": window_date.isoformat(), "dns_session": dns_session}
        )
        row = SettlementWindowRow(
            window_id=window_id,
            dns_session=dns_session,
            window_date=window_date,
            status="WINDOW_OPEN",
            opened_at=now,
        )
        self._store.insert_window(row)
        return row

    def window_tick(self, window_id: str) -> SettlementWindowRow:
        """``window_open_tick``: WINDOW_OPEN -> ACCUMULATING_POSITIONS."""
        self._require_pso()
        row = self._require_window(window_id)
        self._window_transition(row, "window_open_tick", "ACCUMULATING_POSITIONS")
        return self._store.update_window(window_id, status="ACCUMULATING_POSITIONS")

    def transition_window(
        self, window_id: str, trigger: str, to_state: str, **changes: object
    ) -> SettlementWindowRow:
        """Generic guarded transition for the remaining FSM rows."""
        self._require_pso()
        row = self._require_window(window_id)
        self._window_transition(row, trigger, to_state)
        return self._store.update_window(window_id, status=to_state, **changes)

    # ------------------------------------------------------------------
    # Net debit caps
    # ------------------------------------------------------------------

    def set_net_debit_cap(
        self,
        participant_id: str,
        cap_amount_minor: int,
        *,
        approved_by: str,
        approval_request_id: str,
        rationale: str,
        effective_from: datetime | None = None,
    ) -> NetDebitCapRow:
        self._require_pso()
        if cap_amount_minor <= 0:
            raise NdcError(f"cap_amount_minor must be > 0, got {cap_amount_minor}")
        if not rationale or len(rationale) > 1000:
            raise NdcError("rationale is required (max 1000 chars)")
        now = self._now()
        effective = effective_from or now
        if effective < now:
            raise NdcError("effective_from cannot be backdated")
        existing = self._store.active_cap(participant_id)
        if existing is not None:
            self._store.supersede_cap(existing.ndc_id, now)
        row = NetDebitCapRow(
            ndc_id=make_id(
                "ndc",
                {
                    "participant_id": participant_id,
                    "cap_amount_minor": cap_amount_minor,
                    "effective_from": effective,
                },
            ),
            participant_id=participant_id,
            cap_amount_minor=cap_amount_minor,
            currency="BDT",
            effective_from=effective,
            superseded_at=None,
            approved_by=approved_by,
            approval_request_id=approval_request_id,
            rationale=rationale,
            created_at=now,
        )
        self._store.insert_cap(row)
        return row

    # ------------------------------------------------------------------
    # Position movements — the NDC mirror
    # ------------------------------------------------------------------

    def _open_position_account(
        self, participant_id: str, window: SettlementWindowRow, cap_minor: int
    ) -> PositionAccountRow:
        existing = self._store.get_position_account(participant_id, window.window_id)
        if existing is not None:
            return existing
        now = self._now()
        row = PositionAccountRow(
            position_account_id=make_id(
                "posn",
                {"participant_id": participant_id, "window_id": window.window_id},
            ),
            participant_id=participant_id,
            settlement_window_id=window.window_id,
            net_position_minor=0,
            currency="BDT",
            net_debit_cap_minor=cap_minor,
            debit_count=0,
            credit_count=0,
            last_movement_at=None,
            window_state=window.status,
            created_at=now,
        )
        self._store.insert_position_account(row)
        return row

    def post_debit(
        self,
        participant_id: str,
        amount_minor: int,
        *,
        entry_id: str | None = None,
        source_payment_id: str | None = None,
    ) -> PositionAccountRow:
        """Apply a debit to the participant's position; refuse on breach.

        Mirror of the 0012 trigger: refusal order is (1) no open window,
        (2) no active cap, (3) headroom breach — each refusal fails closed
        and (1)/(3) write a breach-log row first.
        """
        self._require_pso()
        if amount_minor <= 0:
            raise NdcError(f"debit amount_minor must be > 0, got {amount_minor}")
        now = self._now()
        window = self._store.find_accumulating_window()
        if window is None:
            self._store.insert_breach(
                NdcBreachLogRow(
                    breach_id=make_id(
                        "ndc",
                        {
                            "participant_id": participant_id,
                            "reason": "NO_OPEN_WINDOW",
                            "blocked_at": now,
                        },
                    ),
                    participant_id=participant_id,
                    settlement_window_id="NONE",
                    position_account_id="NONE",
                    attempted_debit_minor=amount_minor,
                    net_position_before_minor=0,
                    net_debit_cap_minor=0,
                    headroom_before_minor=0,
                    blocked_at=now,
                    blocked_posting_entry_id=entry_id,
                    source_payment_id=source_payment_id,
                )
            )
            raise NoOpenWindowError(
                f"NDC_NO_OPEN_WINDOW: no ACCUMULATING_POSITIONS window for {participant_id}"
            )
        cap = self._store.active_cap(participant_id)
        if cap is None:
            raise NdcNotConfiguredError(
                f"NDC_NOT_CONFIGURED: no active Net Debit Cap for {participant_id}"
            )
        position = self._open_position_account(participant_id, window, cap.cap_amount_minor)
        new_net = position.net_position_minor - amount_minor
        headroom = cap.cap_amount_minor - max(0, -new_net)
        if headroom < 0:
            self._store.insert_breach(
                NdcBreachLogRow(
                    breach_id=make_id(
                        "ndc",
                        {
                            "participant_id": participant_id,
                            "position_account_id": position.position_account_id,
                            "attempted": amount_minor,
                            "blocked_at": now,
                        },
                    ),
                    participant_id=participant_id,
                    settlement_window_id=window.window_id,
                    position_account_id=position.position_account_id,
                    attempted_debit_minor=amount_minor,
                    net_position_before_minor=position.net_position_minor,
                    net_debit_cap_minor=cap.cap_amount_minor,
                    headroom_before_minor=position.headroom_minor,
                    blocked_at=now,
                    blocked_posting_entry_id=entry_id,
                    source_payment_id=source_payment_id,
                )
            )
            raise NdcBreachError(participant_id, amount_minor, headroom)
        return self._store.update_position_account(
            position.position_account_id,
            net_position_minor=new_net,
            debit_count=position.debit_count + 1,
            last_movement_at=now,
        )

    def post_credit(self, participant_id: str, amount_minor: int) -> PositionAccountRow:
        """Apply a credit to the participant's position in the open window."""
        self._require_pso()
        if amount_minor <= 0:
            raise NdcError(f"credit amount_minor must be > 0, got {amount_minor}")
        now = self._now()
        window = self._store.find_accumulating_window()
        if window is None:
            raise NoOpenWindowError(
                f"NDC_NO_OPEN_WINDOW: no ACCUMULATING_POSITIONS window for {participant_id}"
            )
        cap = self._store.active_cap(participant_id)
        if cap is None:
            raise NdcNotConfiguredError(
                f"NDC_NOT_CONFIGURED: no active Net Debit Cap for {participant_id}"
            )
        position = self._open_position_account(participant_id, window, cap.cap_amount_minor)
        return self._store.update_position_account(
            position.position_account_id,
            net_position_minor=position.net_position_minor + amount_minor,
            credit_count=position.credit_count + 1,
            last_movement_at=now,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _require_window(self, window_id: str) -> SettlementWindowRow:
        row = self._store.get_window(window_id)
        if row is None:
            raise NdcError(f"unknown window {window_id}")
        return row

    def _now(self) -> datetime:
        dt = self._clock.now()
        if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
            raise NdcError("clock returned a naive datetime (spec/00 §7)")
        return dt.astimezone(UTC)


# ---------------------------------------------------------------------------
# Postgres store (matches 0012_pso_position.sql)
# ---------------------------------------------------------------------------

_WINDOW_COLUMNS = (
    "window_id, dns_session, window_date, status, opened_at, cutoff_at, "
    "netting_completed_at, instructed_at, confirmed_at, closed_at, "
    "net_settlement_amount_minor, currency, block_reason, blocked_at, unblocked_at, "
    "shortfall_override, shortfall_override_approval_id, settlement_attempt_number, "
    "settlement_batch_id, schema_version"
)

_POSITION_COLUMNS = (
    "position_account_id, participant_id, settlement_window_id, net_position_minor, currency, "
    "net_debit_cap_minor, debit_count, credit_count, last_movement_at, window_state, "
    "created_at, schema_version"
)

_CAP_COLUMNS = (
    "ndc_id, participant_id, cap_amount_minor, currency, effective_from, superseded_at, "
    "approved_by, approval_request_id, rationale, created_at, schema_version"
)

_BREACH_COLUMNS = (
    "breach_id, participant_id, settlement_window_id, position_account_id, "
    "attempted_debit_minor, net_position_before_minor, net_debit_cap_minor, "
    "headroom_before_minor, blocked_at, blocked_posting_entry_id, source_payment_id, "
    "schema_version"
)


class PostgresPositionStore:
    """Sync psycopg3 store over an autocommit connection (ledger schema)."""

    def __init__(self, conn: psycopg.Connection[tuple[Any, ...]]) -> None:
        if not conn.autocommit:
            raise ValueError("PostgresPositionStore requires an autocommit connection")
        self._conn = conn

    @classmethod
    def connect(cls, dsn: str, *, role: str | None = None) -> PostgresPositionStore:
        conn = psycopg.connect(dsn, autocommit=True, options="-c TimeZone=UTC")
        if role is not None:
            conn.execute(psycopg.sql.SQL("SET ROLE {}").format(psycopg.sql.Identifier(role)))
        return cls(conn)

    def close(self) -> None:
        self._conn.close()

    def insert_window(self, row: SettlementWindowRow) -> None:
        self._conn.execute(
            f"INSERT INTO ledger.settlement_windows ({_WINDOW_COLUMNS})"
            f" VALUES ({', '.join(['%s'] * 20)})",
            (
                row.window_id,
                row.dns_session,
                row.window_date,
                row.status,
                row.opened_at,
                row.cutoff_at,
                row.netting_completed_at,
                row.instructed_at,
                row.confirmed_at,
                row.closed_at,
                row.net_settlement_amount_minor,
                row.currency,
                row.block_reason,
                row.blocked_at,
                row.unblocked_at,
                row.shortfall_override,
                row.shortfall_override_approval_id,
                row.settlement_attempt_number,
                row.settlement_batch_id,
                row.schema_version,
            ),
        )

    def get_window(self, window_id: str) -> SettlementWindowRow | None:
        cur = self._conn.execute(
            f"SELECT {_WINDOW_COLUMNS} FROM ledger.settlement_windows WHERE window_id = %s",
            (window_id,),
        )
        row = cur.fetchone()
        return SettlementWindowRow(*row) if row else None

    def update_window(self, window_id: str, **changes: object) -> SettlementWindowRow:
        unknown = set(changes) - _WINDOW_MUTABLE
        if unknown:
            raise NdcError(f"non-updatable column(s) {sorted(unknown)}")
        if changes:
            columns = sorted(changes)
            sets = ", ".join(f"{column} = %s" for column in columns)
            params: list[Any] = [changes[column] for column in columns]
            params.append(window_id)
            cur = self._conn.execute(
                f"UPDATE ledger.settlement_windows SET {sets} WHERE window_id = %s",  # noqa: S608
                params,
            )
            if cur.rowcount != 1:
                raise NdcError(f"unknown window {window_id}")
        updated = self.get_window(window_id)
        assert updated is not None
        return updated

    def find_accumulating_window(self) -> SettlementWindowRow | None:
        cur = self._conn.execute(
            f"SELECT {_WINDOW_COLUMNS} FROM ledger.settlement_windows"
            " WHERE status = 'ACCUMULATING_POSITIONS' ORDER BY opened_at DESC LIMIT 1"
        )
        row = cur.fetchone()
        return SettlementWindowRow(*row) if row else None

    def insert_position_account(self, row: PositionAccountRow) -> None:
        self._conn.execute(
            f"INSERT INTO ledger.position_accounts ({_POSITION_COLUMNS})"
            f" VALUES ({', '.join(['%s'] * 12)})",
            (
                row.position_account_id,
                row.participant_id,
                row.settlement_window_id,
                row.net_position_minor,
                row.currency,
                row.net_debit_cap_minor,
                row.debit_count,
                row.credit_count,
                row.last_movement_at,
                row.window_state,
                row.created_at,
                row.schema_version,
            ),
        )

    def get_position_account(
        self, participant_id: str, window_id: str
    ) -> PositionAccountRow | None:
        cur = self._conn.execute(
            f"SELECT {_POSITION_COLUMNS} FROM ledger.position_accounts"
            " WHERE participant_id = %s AND settlement_window_id = %s",
            (participant_id, window_id),
        )
        row = cur.fetchone()
        return PositionAccountRow(*row) if row else None

    def update_position_account(
        self, position_account_id: str, **changes: object
    ) -> PositionAccountRow:
        unknown = set(changes) - _POSITION_MUTABLE
        if unknown:
            raise NdcError(f"non-updatable column(s) {sorted(unknown)}")
        if changes:
            columns = sorted(changes)
            sets = ", ".join(f"{column} = %s" for column in columns)
            params: list[Any] = [changes[column] for column in columns]
            params.append(position_account_id)
            cur = self._conn.execute(
                "UPDATE ledger.position_accounts SET "  # noqa: S608
                f"{sets} WHERE position_account_id = %s",
                params,
            )
            if cur.rowcount != 1:
                raise NdcError(f"unknown position account {position_account_id}")
        cur = self._conn.execute(
            f"SELECT {_POSITION_COLUMNS} FROM ledger.position_accounts"
            " WHERE position_account_id = %s",
            (position_account_id,),
        )
        row = cur.fetchone()
        assert row is not None
        return PositionAccountRow(*row)

    def insert_cap(self, row: NetDebitCapRow) -> None:
        self._conn.execute(
            f"INSERT INTO ledger.net_debit_caps ({_CAP_COLUMNS})"
            f" VALUES ({', '.join(['%s'] * 11)})",
            (
                row.ndc_id,
                row.participant_id,
                row.cap_amount_minor,
                row.currency,
                row.effective_from,
                row.superseded_at,
                row.approved_by,
                row.approval_request_id,
                row.rationale,
                row.created_at,
                row.schema_version,
            ),
        )

    def active_cap(self, participant_id: str) -> NetDebitCapRow | None:
        cur = self._conn.execute(
            f"SELECT {_CAP_COLUMNS} FROM ledger.net_debit_caps"
            " WHERE participant_id = %s AND superseded_at IS NULL LIMIT 1",
            (participant_id,),
        )
        row = cur.fetchone()
        return NetDebitCapRow(*row) if row else None

    def supersede_cap(self, ndc_id: str, superseded_at: datetime) -> None:
        cur = self._conn.execute(
            "UPDATE ledger.net_debit_caps SET superseded_at = %s WHERE ndc_id = %s",
            (superseded_at, ndc_id),
        )
        if cur.rowcount != 1:
            raise NdcError(f"unknown ndc_id {ndc_id}")

    def insert_breach(self, row: NdcBreachLogRow) -> None:
        self._conn.execute(
            f"INSERT INTO ledger.ndc_breach_log ({_BREACH_COLUMNS})"
            f" VALUES ({', '.join(['%s'] * 12)})",
            (
                row.breach_id,
                row.participant_id,
                row.settlement_window_id,
                row.position_account_id,
                row.attempted_debit_minor,
                row.net_position_before_minor,
                row.net_debit_cap_minor,
                row.headroom_before_minor,
                row.blocked_at,
                row.blocked_posting_entry_id,
                row.source_payment_id,
                row.schema_version,
            ),
        )

    def list_breaches(self, participant_id: str | None = None) -> list[NdcBreachLogRow]:
        if participant_id is None:
            cur = self._conn.execute(
                f"SELECT {_BREACH_COLUMNS} FROM ledger.ndc_breach_log ORDER BY blocked_at"
            )
        else:
            cur = self._conn.execute(
                f"SELECT {_BREACH_COLUMNS} FROM ledger.ndc_breach_log"
                " WHERE participant_id = %s ORDER BY blocked_at",
                (participant_id,),
            )
        return [NdcBreachLogRow(*row) for row in cur.fetchall()]

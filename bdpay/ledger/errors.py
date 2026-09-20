"""Ledger error taxonomy (spec/03 API surface; spec/04 + spec/05 additions).

Ported verbatim from bd-pay-fable-pilot/src/bdpay_pilot/ledger/errors.py
(verified pilot), extended with the settlement / reconciliation / TCSA /
PSO-position error classes for specs 04 and 05.
"""

from __future__ import annotations


class LedgerError(Exception):
    """Base class for all ledger failures."""


class LedgerImbalanceError(LedgerError):
    """SUM(debit) != SUM(credit) for a journal entry spec."""


class LedgerDuplicateError(LedgerError):
    """idempotency_key already exists (idempotent replay).

    Carries the entry_id of the previously-committed journal entry so the
    caller can treat the replay as success without re-posting (E7).
    """

    def __init__(self, idempotency_key: str, existing_entry_id: str) -> None:
        super().__init__(
            f"journal entry with idempotency_key {idempotency_key!r} already exists "
            f"as {existing_entry_id!r}"
        )
        self.idempotency_key = idempotency_key
        self.existing_entry_id = existing_entry_id


class LedgerDuplicateEventError(LedgerError):
    """An audit or outbox event with the same deterministic event_id already exists.

    Used so replay paths can treat a race-conditioned duplicate insert as success
    rather than an error.
    """

    def __init__(self, event_id: str) -> None:
        super().__init__(f"event {event_id!r} already exists")
        self.event_id = event_id


class LedgerAccountNotFoundError(LedgerError):
    """A posting references an absent or closed account."""


class LedgerAmountError(LedgerError):
    """A posting amount is not a positive integer paisa value."""


class LedgerValidationError(LedgerError):
    """Spec-shape violation (bad entry_type, < 2 postings, bad currency, ...)."""


class TrialBalanceViolationError(LedgerError):
    """Global SUM(debit) != SUM(credit) across all postings."""

    def __init__(self, total_debit_minor: int, total_credit_minor: int) -> None:
        super().__init__(
            f"trial balance violated: debit={total_debit_minor} credit={total_credit_minor} "
            f"delta={total_debit_minor - total_credit_minor}"
        )
        self.total_debit_minor = total_debit_minor
        self.total_credit_minor = total_credit_minor


class LedgerWriteGateEngagedError(LedgerError):
    """write_gate_halted is engaged: all money movement is blocked (fail closed)."""


class ChainIntegrityError(LedgerError):
    """Raised when a chain operation cannot proceed safely (fail closed)."""


# ---------------------------------------------------------------------------
# spec/04 — settlement and reconciliation
# ---------------------------------------------------------------------------


class SettlementError(LedgerError):
    """Base class for settlement-engine failures."""


class SettlementInvalidTransitionError(SettlementError):
    """Refusal-first FSM default: the requested transition is not in the table."""


class SettlementGuardError(SettlementError):
    """A transition guard evaluated false (the transition is denied)."""


class TcsaDispatchBlockedError(SettlementError):
    """The TCSA dispatch gate refused dispatch (fails closed; spec/04 FSM 1)."""

    def __init__(self, detail: str, snapshot_id: str | None, shortfall_minor: int) -> None:
        super().__init__(detail)
        self.snapshot_id = snapshot_id
        self.shortfall_minor = shortfall_minor


class FeeRuleError(SettlementError):
    """No applicable fee rule, or the computed fee violates its bounds."""


class BeftnFormatError(SettlementError):
    """A BEFTN file field cannot be encoded within its fixed-width layout."""


class ReconciliationError(LedgerError):
    """Base class for reconciliation-engine failures."""


class ReconInvalidTransitionError(ReconciliationError):
    """Refusal-first FSM default for ReconciliationRecord/Exception transitions."""


# ---------------------------------------------------------------------------
# spec/05 — TCSA monitor and PSO position accounts
# ---------------------------------------------------------------------------


class TcsaError(LedgerError):
    """Base class for TCSA monitor failures."""


class PsoModeError(LedgerError):
    """A PSO-only operation was attempted while PLATFORM_MODE != PSO (refused)."""


class NdcError(LedgerError):
    """Base class for Net Debit Cap failures."""


class NdcBreachError(NdcError):
    """A debit would breach the participant's Net Debit Cap (blocked, logged)."""

    def __init__(
        self, participant_id: str, attempted_debit_minor: int, headroom_minor: int
    ) -> None:
        super().__init__(
            f"NDC_BREACH: participant {participant_id} debit of {attempted_debit_minor} paisa "
            f"exceeds headroom {headroom_minor} paisa"
        )
        self.participant_id = participant_id
        self.attempted_debit_minor = attempted_debit_minor
        self.headroom_minor = headroom_minor


class NdcNotConfiguredError(NdcError):
    """No active Net Debit Cap exists for the participant (debit refused)."""


class NoOpenWindowError(NdcError):
    """No ACCUMULATING_POSITIONS settlement window exists (debit refused)."""


class WindowInvalidTransitionError(LedgerError):
    """Refusal-first FSM default for SettlementWindow transitions (spec/05)."""

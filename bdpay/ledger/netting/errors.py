"""Netting-engine error taxonomy (spec/19 PSO-2).

All classes extend the spec/03 ``LedgerError`` hierarchy so callers that
already handle ledger failures compose unchanged. Refusal-first: every error
here is a refusal — nothing is caught-and-continued inside the engine.
"""

from __future__ import annotations

from bdpay.ledger.errors import LedgerError, NdcError

__all__ = [
    "NdcParticipantNotActiveError",
    "NettingError",
    "NettingGuardError",
    "NettingIntegrityError",
    "NettingInvalidTransitionError",
    "UnwindRefusedError",
]


class NettingError(LedgerError):
    """Base class for netting-engine failures (spec/19 PSO-2)."""


class NettingInvalidTransitionError(NettingError):
    """Refusal-first FSM default for NettingRun transitions (spec/19 FSM 3)."""


class NettingGuardError(NettingError):
    """A netting transition guard evaluated false (the transition is denied)."""


class NettingIntegrityError(NettingError):
    """Netting integrity check failed (spec/19 algorithm step 2; fail closed).

    The window stays ``CUTOFF_REACHED``; a ``FAILED_INTEGRITY`` netting run
    row records the forensic detail. A late window beats a false one.
    """

    def __init__(self, netting_run_id: str, failure_detail: dict) -> None:
        super().__init__(
            f"NETTING_INTEGRITY_FAILURE: run {netting_run_id} failed integrity: "
            f"{failure_detail}"
        )
        self.netting_run_id = netting_run_id
        self.failure_detail = failure_detail


class UnwindRefusedError(NettingError):
    """A D-19-2 unwind request failed its guards (refused, nothing changed)."""


class NdcParticipantNotActiveError(NdcError):
    """Posting refused: the participant is not ``ACTIVE`` (spec/19 D-19 guard).

    Application-side mirror of the migration 0102 trigger guard — suspended
    participants are refused all further postings mid-window.
    """

    def __init__(self, participant_id: str, kyb_status: str) -> None:
        super().__init__(
            f"NDC_PARTICIPANT_NOT_ACTIVE: participant {participant_id} has "
            f"kyb_status={kyb_status!r}; postings refused"
        )
        self.participant_id = participant_id
        self.kyb_status = kyb_status

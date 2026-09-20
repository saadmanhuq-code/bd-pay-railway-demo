"""spec/19 PSO-1 transition tables — ParticipantOnboarding v2 + ConformanceRun.

Transcribed row by row from spec/19 §State machines 1 and 2 (the v2 table is
the D-19-1 extension of spec/08 8-FSM-2 — additive states and transitions,
nothing redefined). Refusal-first per spec/00 §8: any (state, trigger) pair
not in the table is DENIED by :class:`bdpay.kernel.fsm.TransitionTable`.

Documented divergences (SPEC_ERRATA-LANE-A-spec19.md):

- P19-3: the ConformanceRun FSM table declares no exit from ``QUEUED`` other
  than ``runner_pickup``, while the spec's failure-modes table requires a
  forged/garbled INBOUND evidence report to leave the run "``ERRORED`` before
  ``RUNNING``". The additive ``QUEUED --[evidence_hash_mismatch]--> ERRORED``
  row records that requirement; the participant's onboarding state is
  unaffected by a pickup-refused run.
- P19-4: FSM 1's ``conformance_started`` guard reads "first ConformanceRun
  queued", yet ``conformance_failed`` returns the participant to
  ``MOU_SIGNED`` and the only declared way back into ``CONFORMANCE_TESTING``
  is ``conformance_started``. Guard resolution: any run queued while the
  participant is in ``MOU_SIGNED`` fires it (first run of the current attempt
  cycle); runs queued during ``CONFORMANCE_TESTING`` do not re-fire it.
- D-19-1 legacy states: ``LICENSE_VERIFICATION_PENDING`` and
  ``SETTLEMENT_AGREEMENT_PENDING`` remain valid states with their spec/08
  OUTBOUND rows (in-flight rows are never bricked) but receive no inbound
  transitions — ``application_accepted`` now targets
  ``DOCUMENT_VERIFICATION_PENDING``.
"""

from __future__ import annotations

from bdpay.kernel.fsm import TransitionRule, TransitionTable

__all__ = [
    "CONFORMANCE_DIRECTIONS",
    "CONFORMANCE_RUN_TABLE",
    "DOCUMENT_CLASSES",
    "DOCUMENT_REVIEW_STATUSES",
    "LEGACY_STATES",
    "PARTICIPANT_V2_STATES",
    "PARTICIPANT_V2_TABLE",
]

_R = TransitionRule

#: spec/19 §API A — the closed set of onboarding document classes. The MOU
#: guard requires one VERIFIED row for every class ("all five").
DOCUMENT_CLASSES: tuple[str, ...] = (
    "BB_LICENSE",
    "BOARD_RESOLUTION",
    "SETTLEMENT_ACCOUNT_DETAILS",
    "TECHNICAL_CONTACT",
    "COMPLIANCE_CONTACT",
)

DOCUMENT_REVIEW_STATUSES: tuple[str, ...] = ("PENDING", "VERIFIED", "REJECTED", "SUPERSEDED")

CONFORMANCE_DIRECTIONS: tuple[str, ...] = ("OUTBOUND", "INBOUND")

#: D-19-1: retained spec/08 states; valid values, no new inbound transitions.
LEGACY_STATES: tuple[str, ...] = (
    "LICENSE_VERIFICATION_PENDING",
    "SETTLEMENT_AGREEMENT_PENDING",
)

PARTICIPANT_V2_STATES: tuple[str, ...] = (
    "APPLICATION_SUBMITTED",
    "DOCUMENT_VERIFICATION_PENDING",
    "LICENSE_VERIFICATION_PENDING",  # legacy, retained (D-19-1)
    "SETTLEMENT_AGREEMENT_PENDING",  # legacy, retained (D-19-1)
    "MOU_SIGNED",
    "CONFORMANCE_TESTING",
    "CONFORMANCE_PASSED",
    "NDC_ASSIGNMENT_PENDING",
    "ACTIVE",
    "SUSPENDED",
    "REJECTED",
    "TERMINATED",
)

_NON_TERMINAL = tuple(s for s in PARTICIPANT_V2_STATES if s not in ("REJECTED", "TERMINATED"))

#: spec/19 FSM 1 row ``any non-terminal --[rejected / operator + reason]--> REJECTED``;
#: the closed table needs one explicit row per non-terminal state.
_REJECTED_ROWS = tuple(
    _R(state, "rejected", "REJECTED", side_effects=("aud", "event:participant.kyb_rejected"))
    for state in _NON_TERMINAL
)

PARTICIPANT_V2_TABLE = TransitionTable(
    "ParticipantOnboardingV2",
    states=PARTICIPANT_V2_STATES,
    terminal_states=("REJECTED", "TERMINATED"),
    rules=(
        # -- v2 path (spec/19 FSM 1) --------------------------------------
        _R(
            "APPLICATION_SUBMITTED",
            "application_accepted",
            "DOCUMENT_VERIFICATION_PENDING",
            side_effects=(
                "aud:PARTICIPANT_DOCS_REQUESTED",
                "event:participant.application_submitted",
            ),
        ),
        _R(
            "DOCUMENT_VERIFICATION_PENDING",
            "document_verified",
            "DOCUMENT_VERIFICATION_PENDING",  # no state change until all five
            side_effects=(
                "aud:PARTICIPANT_DOCUMENT_VERIFIED",
                "event:participant.document_verified",
            ),
        ),
        _R(
            "DOCUMENT_VERIFICATION_PENDING",
            "document_rejected",
            "DOCUMENT_VERIFICATION_PENDING",
            side_effects=("aud",),
        ),
        _R(
            "DOCUMENT_VERIFICATION_PENDING",
            "mou_recorded",
            "MOU_SIGNED",
            side_effects=("aud:PARTICIPANT_MOU_SIGNED", "event:participant.mou_signed"),
        ),
        _R(
            "DOCUMENT_VERIFICATION_PENDING",
            "license_invalid",
            "REJECTED",
            side_effects=("aud", "event:participant.kyb_rejected"),
        ),
        _R(
            "MOU_SIGNED",
            "conformance_started",
            "CONFORMANCE_TESTING",
            side_effects=(
                "aud:PARTICIPANT_CONFORMANCE_STARTED",
                "event:participant.conformance_started",
            ),
        ),
        _R(
            "CONFORMANCE_TESTING",
            "conformance_passed",
            "CONFORMANCE_PASSED",
            side_effects=("aud", "event:participant.conformance_passed"),
        ),
        _R(
            "CONFORMANCE_TESTING",
            "conformance_failed",
            "MOU_SIGNED",
            side_effects=("aud", "event:participant.conformance_failed"),
        ),
        _R(
            "CONFORMANCE_TESTING",
            "conformance_exhausted",
            "REJECTED",
            side_effects=("aud", "event:participant.kyb_rejected"),
        ),
        _R(
            "CONFORMANCE_PASSED",
            "activation_requested",
            "NDC_ASSIGNMENT_PENDING",
            side_effects=("aud", "approval_request:participant_activation"),
        ),
        # -- spec/08 rows, unchanged --------------------------------------
        _R(
            "NDC_ASSIGNMENT_PENDING",
            "ndc_assigned",
            "ACTIVE",
            side_effects=(
                "aud",
                "event:participant.activated",
                "ledger:create participant_position + net_debit_cap accounts",
            ),
        ),
        _R("NDC_ASSIGNMENT_PENDING", "ndc_rejected", "REJECTED", side_effects=("aud",)),
        _R(
            "ACTIVE",
            "ndc_revised",
            "ACTIVE",
            side_effects=("aud", "event:participant.net_debit_cap_revised"),
        ),
        _R(
            "ACTIVE",
            "suspended",
            "SUSPENDED",
            side_effects=("aud", "event:participant.suspended"),
        ),
        _R("SUSPENDED", "reinstated", "ACTIVE", side_effects=("aud",)),
        _R(
            "ACTIVE",
            "terminated",
            "TERMINATED",
            side_effects=("aud", "event:participant.terminated"),
        ),
        # -- legacy spec/08 rows (D-19-1: outbound only; never re-entered) --
        _R(
            "LICENSE_VERIFICATION_PENDING",
            "license_verified",
            "SETTLEMENT_AGREEMENT_PENDING",
            side_effects=("aud", "event:participant.license_verified"),
        ),
        _R(
            "LICENSE_VERIFICATION_PENDING",
            "license_invalid",
            "REJECTED",
            side_effects=("aud", "event:participant.kyb_rejected"),
        ),
        _R(
            "SETTLEMENT_AGREEMENT_PENDING",
            "agreement_signed",
            "NDC_ASSIGNMENT_PENDING",
            side_effects=("aud",),
        ),
        *_REJECTED_ROWS,
    ),
)


# ---------------------------------------------------------------------------
# spec/19 FSM 2: ConformanceRun (mirrors the spec/10 CertificationRun FSM)
# ---------------------------------------------------------------------------

CONFORMANCE_RUN_STATES: tuple[str, ...] = ("QUEUED", "RUNNING", "PASSED", "FAILED", "ERRORED")

CONFORMANCE_RUN_TABLE = TransitionTable(
    "ConformanceRun",
    states=CONFORMANCE_RUN_STATES,
    terminal_states=("PASSED", "FAILED", "ERRORED"),
    rules=(
        _R(
            "QUEUED",
            "runner_pickup",
            "RUNNING",
            side_effects=("aud:CONFORMANCE_RUN_STARTED",),
        ),
        # P19-3: failure-modes row "Run ERRORED before RUNNING" (forged or
        # garbled INBOUND evidence); participant unaffected state-wise.
        _R(
            "QUEUED",
            "evidence_hash_mismatch",
            "ERRORED",
            side_effects=("aud", "event:conformance_run.completed"),
        ),
        _R(
            "RUNNING",
            "checks_passed",
            "PASSED",
            side_effects=("aud", "event:conformance_run.completed"),
        ),
        _R(
            "RUNNING",
            "check_failed",
            "FAILED",
            side_effects=("aud", "event:conformance_run.completed"),
        ),
        _R(
            "RUNNING",
            "errored",
            "ERRORED",
            side_effects=("aud", "event:conformance_run.completed"),
        ),
    ),
)

#: spec/10 timeout convention, reused by FSM 2: any single run exceeding this
#: wall-time budget is ``ERRORED``.
CONFORMANCE_RUN_TIMEOUT_SECONDS = 15 * 60

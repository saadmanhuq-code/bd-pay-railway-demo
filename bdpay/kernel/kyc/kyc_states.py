"""KycRecord + manual-review transition tables (spec/09, binding).

Transcribed from the spec/09 §9.1 transition table (T1-T26) and §9.2 manual
review FSM. Refusal-first per spec/00 §8.

Documented divergences (errata SPEC_ERRATA-LANE-A-kernel.md):

- K-3 (same class as the payment FSMs): ``ACTIVE`` and ``EXPIRED_HARD`` are
  listed "terminal" while the table declares transitions out of them; the
  engine's terminal set is {REJECTED, ARCHIVED} and the live states are
  constrained by refusal-first to exactly the declared triggers.
- K-13: spec/09 T20 moves the OLD record ``ACTIVE -> REFRESH_PENDING`` while
  the same spec's refresh invariant requires the old record to STAY ACTIVE
  until the successor activates (no coverage gap; the partial-unique-ACTIVE
  index argument depends on it). Resolved invariant-preserving:
  ``REFRESH_PENDING`` is the state of the NEW successor record at creation;
  the old record keeps its declared ``ACTIVE`` transitions
  (``refresh_overdue``, ``new_kyc_cycle_completed``). The successor enters
  the intake flow via the added ``refresh_session_started`` rule.
"""

from __future__ import annotations

from bdpay.kernel.fsm import TransitionRule, TransitionTable

__all__ = [
    "KYC_MANUAL_REVIEW_TABLE",
    "KYC_REJECTION_REASONS",
    "KYC_REVIEW_REASONS",
    "KYC_TABLE",
    "KYC_TIERS",
    "RISK_TIER_REFRESH_YEARS",
]

_R = TransitionRule

KYC_TIERS: tuple[str, ...] = ("SIMPLIFIED", "REGULAR")

#: spec/09 risk-tier refresh cadence: HIGH 1yr / MEDIUM 2yr / LOW 5yr.
RISK_TIER_REFRESH_YEARS: dict[str, int] = {"HIGH": 1, "MEDIUM": 2, "LOW": 5}

KYC_REJECTION_REASONS: tuple[str, ...] = (
    "NID_MISMATCH",
    "LIVENESS_FAILED",
    "DOCUMENT_FRAUD_SUSPECTED",
    "MINOR_NO_GUARDIAN",
    "SANCTIONS_HIT",
    "BIOMETRIC_MAX_RETRIES",
    "SESSION_TIMEOUT",
    "BIOMETRIC_TIMEOUT",
)

KYC_REVIEW_REASONS: tuple[str, ...] = (
    "PORICHOY_UNAVAILABLE",
    "MINOR_ACCOUNT_POLICY",
    "SANCTIONS_ADJACENT",
    "DOCUMENT_QUALITY_ISSUE",
    "MANUAL_ESCALATION",
    # Additive (errata OCR-1): the document-OCR provider analogue of
    # PORICHOY_UNAVAILABLE — the spec/09 non-blocking fallback applied to the
    # OCR intake path.
    "OCR_UNAVAILABLE",
)

KYC_STATES = (
    "REFRESH_PENDING",
    "SUBMITTED",
    "NID_OCR_RECEIVED",
    "BIOMETRIC_PENDING",
    "BIOMETRIC_RETRY_PENDING",
    "BIOMETRIC_PASSED",
    "PENDING_HUMAN_REVIEW",
    "ACTIVE",
    "REJECTED",
    "REFRESH_OVERDUE",
    "EXPIRED_HARD",
    "ARCHIVED",
)

KYC_TABLE = TransitionTable(
    "KycRecord",
    states=KYC_STATES,
    terminal_states=("REJECTED", "ARCHIVED"),  # errata K-3
    rules=(
        # T2/T3 — NID OCR intake (guarded on the minor check)
        _R(
            "SUBMITTED",
            "nid_ocr_received",
            "NID_OCR_RECEIVED",
            guard="adult",
            side_effects=("aud", "event:kyc_record.nid_ocr_received"),
        ),
        _R(
            "SUBMITTED",
            "nid_ocr_received",
            "PENDING_HUMAN_REVIEW",
            guard="minor",
            side_effects=("aud", "approval:MINOR_ACCOUNT_POLICY",
                          "event:kyc_record.pending_human_review"),
        ),
        # Additive (errata OCR-1): document-OCR provider unavailable during
        # the server-side OCR intake path — same non-blocking posture as T10,
        # entered from SUBMITTED because the failure happens before any NID
        # data exists. Refusal-first untouched: only this trigger is added.
        _R(
            "SUBMITTED",
            "ocr_unavailable",
            "PENDING_HUMAN_REVIEW",
            side_effects=("approval:OCR_UNAVAILABLE",
                          "event:kyc_record.pending_human_review"),
        ),
        # T4 / T6 — 48h session timeouts
        _R("SUBMITTED", "session_timeout", "REJECTED",
           side_effects=("event:kyc_record.rejected",)),
        _R("NID_OCR_RECEIVED", "session_timeout", "REJECTED",
           side_effects=("event:kyc_record.rejected",)),
        # T5 / T11 — biometric submission
        _R("NID_OCR_RECEIVED", "biometric_submitted", "BIOMETRIC_PENDING"),
        _R("BIOMETRIC_RETRY_PENDING", "biometric_submitted", "BIOMETRIC_PENDING"),
        # T7-T10 — Porichoy outcomes
        _R(
            "BIOMETRIC_PENDING",
            "porichoy_matched",
            "BIOMETRIC_PASSED",
            side_effects=("aud", "event:kyc_record.biometric_passed"),
        ),
        _R(
            "BIOMETRIC_PENDING",
            "porichoy_not_matched",
            "BIOMETRIC_RETRY_PENDING",
            guard="retry",
        ),
        _R(
            "BIOMETRIC_PENDING",
            "porichoy_not_matched",
            "REJECTED",
            guard="exhausted",
            side_effects=("event:kyc_record.rejected",),
        ),
        _R(
            "BIOMETRIC_PENDING",
            "porichoy_unavailable",
            "PENDING_HUMAN_REVIEW",
            side_effects=("approval:PORICHOY_UNAVAILABLE",
                          "event:kyc_record.pending_human_review"),
        ),
        # T12 — biometric retry window timeout
        _R("BIOMETRIC_RETRY_PENDING", "biometric_timeout", "REJECTED",
           side_effects=("event:kyc_record.rejected",)),
        # T13/T14 — tier assignment (sanctions clear)
        _R(
            "BIOMETRIC_PASSED",
            "tier_assigned",
            "ACTIVE",
            guard="simplified",
            side_effects=("event:kyc_record.tier_assigned", "event:kyc_record.activated"),
        ),
        _R(
            "BIOMETRIC_PASSED",
            "tier_assigned",
            "ACTIVE",
            guard="regular",
            side_effects=("event:kyc_record.tier_assigned", "event:kyc_record.activated"),
        ),
        # T15 — sanctions hit at tier assignment
        _R(
            "BIOMETRIC_PASSED",
            "sanctions_hit",
            "PENDING_HUMAN_REVIEW",
            side_effects=("approval:SANCTIONS_ADJACENT",
                          "event:kyc_record.pending_human_review"),
        ),
        # T16-T18 — manual review resolution (two-eyes gate)
        _R(
            "PENDING_HUMAN_REVIEW",
            "manual_review_approve_simplified",
            "ACTIVE",
            side_effects=("event:kyc_record.tier_assigned", "event:kyc_record.activated"),
        ),
        _R(
            "PENDING_HUMAN_REVIEW",
            "manual_review_approve_regular",
            "ACTIVE",
            side_effects=("event:kyc_record.tier_assigned", "event:kyc_record.activated"),
        ),
        _R(
            "PENDING_HUMAN_REVIEW",
            "manual_review_reject",
            "REJECTED",
            side_effects=("event:kyc_record.rejected",),
        ),
        # T19 — tier upgrade: no state change on this record (successor created)
        _R("ACTIVE", "tier_upgrade_requested", "ACTIVE"),
        # T20 (errata K-13): successor created in REFRESH_PENDING; this record
        # stays ACTIVE — modelled as the declared self-loop with the
        # successor-creation side effect.
        _R(
            "ACTIVE",
            "refresh_triggered",
            "ACTIVE",
            side_effects=("create:successor REFRESH_PENDING", "event:kyc_record.refresh_due"),
        ),
        # T21/T22 — refresh overdue ladder
        _R(
            "ACTIVE",
            "refresh_overdue",
            "REFRESH_OVERDUE",
            side_effects=("limits:drop to SIMPLIFIED", "event:kyc_record.refresh_overdue"),
        ),
        _R(
            "REFRESH_OVERDUE",
            "refresh_hard_expired",
            "EXPIRED_HARD",
            side_effects=("wallet:suspended", "event:kyc_record.expired_hard"),
        ),
        # T23-T25 — archived when the successor activates
        _R("ACTIVE", "new_kyc_cycle_completed", "ARCHIVED",
           side_effects=("event:kyc_record.archived",)),
        _R("REFRESH_OVERDUE", "new_kyc_cycle_completed", "ARCHIVED",
           side_effects=("event:kyc_record.archived",)),
        _R("EXPIRED_HARD", "new_kyc_cycle_completed", "ARCHIVED",
           side_effects=("event:kyc_record.archived",)),
        # T26 — retrospective sanctions sweep
        _R(
            "ACTIVE",
            "sanctions_retrospective_hit",
            "PENDING_HUMAN_REVIEW",
            side_effects=("wallet:suspended", "approval:SANCTIONS_ADJACENT",
                          "event:kyc_record.pending_human_review"),
        ),
        # errata K-13: the successor enters the intake flow
        _R("REFRESH_PENDING", "refresh_session_started", "SUBMITTED"),
    ),
)

# ---------------------------------------------------------------------------
# 9.2 manual review FSM
# ---------------------------------------------------------------------------

KYC_MANUAL_REVIEW_STATES = (
    "REVIEW_OPEN",
    "REVIEW_IN_PROGRESS",
    "REVIEW_ESCALATED",
    "REVIEW_CLOSED",
)

KYC_MANUAL_REVIEW_TABLE = TransitionTable(
    "KycManualReview",
    states=KYC_MANUAL_REVIEW_STATES,
    terminal_states=("REVIEW_CLOSED",),
    rules=(
        _R("REVIEW_OPEN", "assigned", "REVIEW_IN_PROGRESS"),
        _R("REVIEW_IN_PROGRESS", "escalated", "REVIEW_ESCALATED"),
        _R("REVIEW_IN_PROGRESS", "decision_made", "REVIEW_CLOSED"),
        _R("REVIEW_ESCALATED", "decision_made", "REVIEW_CLOSED"),
        _R("REVIEW_OPEN", "48h_alert", "REVIEW_OPEN"),  # alert only; no state change
    ),
)

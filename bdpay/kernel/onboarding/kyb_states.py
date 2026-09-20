"""Merchant KYB / PSO participant / document review transition tables (spec/08).

Transcribed row by row from spec/08 8-FSM-1 (merchant KYB), 8-FSM-2 (PSO
participant onboarding) and 8-FSM-3 (document review), including the TTL
rows as declared ``auto_expire`` / ``refresh_failed`` triggers. Refusal-first
per spec/00 §8.

Documented divergences (errata SPEC_ERRATA-LANE-A-kernel.md):

- K-3 (same class as the payment FSMs): ``ACTIVE`` is listed under
  "terminal states" while the table declares four transitions out of it;
  the engine's terminal set is {REJECTED, TERMINATED} and ACTIVE is
  constrained by refusal-first to exactly the declared triggers.
- K-10: spec/08's consumed-events table says
  ``merchant.sanctions_rescreening_required`` makes an ACTIVE merchant
  "re-enter SANCTIONS_REVIEW", which the FSM table does not declare and
  which would route an active merchant back through pre-activation states.
  The FSM table wins: a rescreen hit on an ACTIVE merchant takes the
  declared ``aml_triggered -> SUSPENDED`` path with ``payout_blocked`` set
  immediately (the spec/08 F-03 freeze posture), and CAMLCO resolves via
  ``camlco_cleared`` / ``camlco_terminated``.
"""

from __future__ import annotations

from bdpay.kernel.fsm import TransitionRule, TransitionTable

__all__ = [
    "DOCUMENT_REVIEW_TABLE",
    "DOCUMENT_TYPES",
    "MERCHANT_KYB_TABLE",
    "PARTICIPANT_TABLE",
    "REGISTRATION_TYPES",
    "REJECTION_REASON_CODES",
    "required_document_types",
]

_R = TransitionRule

REGISTRATION_TYPES: tuple[str, ...] = (
    "RJSC_PRIVATE_LIMITED",
    "RJSC_PUBLIC_LIMITED",
    "SOLE_PROPRIETORSHIP",
    "PARTNERSHIP",
    "NGO",
    "FOREIGN_COMPANY",
)

#: spec/08 §8.2 exhaustive document type taxonomy.
DOCUMENT_TYPES: tuple[str, ...] = (
    "RJSC_CERTIFICATE",
    "TRADE_LICENSE",
    "TIN_CERTIFICATE",
    "VAT_BIN_CERTIFICATE",
    "MEMORANDUM_ARTICLES",
    "PARTNERSHIP_DEED",
    "BANK_STATEMENT",
    "UBO_NID_FRONT",
    "UBO_NID_BACK",
    "UBO_SELFIE",
    "WEBSITE_POLICY_SCREENSHOT",
    "FOREIGN_INCORPORATION",
    "BOARD_RESOLUTION",
    "AUTHORIZATION_LETTER",
    "SUPPLEMENT_OTHER",
)

REJECTION_REASON_CODES: tuple[str, ...] = (
    "PROHIBITED_MCC",
    "SANCTIONS_HIT",
    "UBO_NID_UNVERIFIABLE",
    "DOCUMENT_FRAUD_SUSPECTED",
    "POLICY_VIOLATION",
    "INCOMPLETE_DOCUMENTS",
    "OTHER",
    "APPLICATION_TIMEOUT",
    "DOCUMENTS_NOT_PROVIDED",
    "AGREEMENT_NOT_SIGNED",
    "EDD_REJECTED",
    "TWO_EYES_REJECTED",
)

_RJSC_TYPES = frozenset({"RJSC_PRIVATE_LIMITED", "RJSC_PUBLIC_LIMITED"})


def required_document_types(
    registration_type: str, requested_services: tuple[str, ...] | list[str]
) -> frozenset[str]:
    """The spec/08 §8.2 "Required for" checklist for a registration type."""
    if registration_type not in REGISTRATION_TYPES:
        raise ValueError(f"unknown registration_type {registration_type!r}")
    required = {"TRADE_LICENSE", "TIN_CERTIFICATE", "BANK_STATEMENT"}
    if registration_type != "SOLE_PROPRIETORSHIP":
        required.add("RJSC_CERTIFICATE")
    if registration_type in _RJSC_TYPES:
        required.update({"MEMORANDUM_ARTICLES", "BOARD_RESOLUTION"})
    if registration_type == "PARTNERSHIP":
        required.add("PARTNERSHIP_DEED")
    if registration_type == "FOREIGN_COMPANY":
        required.add("FOREIGN_INCORPORATION")
    if "CARD_ACQUIRING" in requested_services:
        required.add("WEBSITE_POLICY_SCREENSHOT")
    return frozenset(required)


# ---------------------------------------------------------------------------
# 8-FSM-1: Merchant KYB
# ---------------------------------------------------------------------------

MERCHANT_KYB_STATES = (
    "APPLICATION_SUBMITTED",
    "DOCUMENTS_PENDING",
    "KYB_IN_PROGRESS",
    "ENHANCED_DUE_DILIGENCE",
    "PENDING_HUMAN_REVIEW",
    "SANCTIONS_REVIEW",
    "RISK_ASSESSED",
    "AGREEMENT_PENDING",
    "ACTIVE",
    "SUSPENDED",
    "KYB_REFRESH_PENDING",
    "REJECTED",
    "TERMINATED",
)

MERCHANT_KYB_TABLE = TransitionTable(
    "MerchantKYB",
    states=MERCHANT_KYB_STATES,
    terminal_states=("REJECTED", "TERMINATED"),  # ACTIVE is live (errata K-3)
    rules=(
        _R(
            "APPLICATION_SUBMITTED",
            "mcc_prohibited",
            "REJECTED",
            side_effects=("aud:kyb_rejected", "event:merchant.kyb_rejected"),
        ),
        _R(
            "APPLICATION_SUBMITTED",
            "application_accepted",
            "DOCUMENTS_PENDING",
            side_effects=("aud", "event:merchant.kyb_documents_pending"),
        ),
        _R("APPLICATION_SUBMITTED", "auto_expire", "REJECTED"),  # 30 calendar days
        _R(
            "DOCUMENTS_PENDING",
            "documents_complete",
            "KYB_IN_PROGRESS",
            side_effects=("aud", "event:merchant.kyb_in_progress"),
        ),
        _R("DOCUMENTS_PENDING", "documents_incomplete", "DOCUMENTS_PENDING"),
        _R("DOCUMENTS_PENDING", "auto_expire", "REJECTED"),  # 14 days after request
        _R(
            "KYB_IN_PROGRESS",
            "ubo_nid_mismatch",
            "DOCUMENTS_PENDING",
            side_effects=("aud", "event:merchant.ubo_nid_mismatch"),
        ),
        _R("KYB_IN_PROGRESS", "porichoy_unavailable", "PENDING_HUMAN_REVIEW"),
        _R(
            "KYB_IN_PROGRESS",
            "high_risk_mcc",
            "ENHANCED_DUE_DILIGENCE",
            side_effects=("aud", "event:merchant.edd_required", "approval:CAMLCO"),
        ),
        _R(
            "KYB_IN_PROGRESS",
            "checks_pass",
            "SANCTIONS_REVIEW",
            side_effects=("aud", "event:merchant.sanctions_review_started"),
        ),
        _R(
            "ENHANCED_DUE_DILIGENCE",
            "edd_complete",
            "SANCTIONS_REVIEW",
            side_effects=("aud", "event:merchant.sanctions_review_started"),
        ),
        _R(
            "ENHANCED_DUE_DILIGENCE",
            "edd_rejected",
            "REJECTED",
            side_effects=("aud", "event:merchant.kyb_rejected"),
        ),
        _R(
            "SANCTIONS_REVIEW",
            "sanctions_hit",
            "REJECTED",
            side_effects=("aud", "event:merchant.kyb_rejected"),
        ),
        _R(
            "SANCTIONS_REVIEW",
            "sanctions_clear",
            "RISK_ASSESSED",
            side_effects=("aud", "event:kyb_record.completed"),
        ),
        _R(
            "RISK_ASSESSED",
            "two_eyes_approved",
            "AGREEMENT_PENDING",
            side_effects=("aud", "event:merchant.agreement_pending"),
        ),
        _R(
            "RISK_ASSESSED",
            "two_eyes_rejected",
            "REJECTED",
            side_effects=("aud", "event:merchant.kyb_rejected"),
        ),
        _R(
            "AGREEMENT_PENDING",
            "agreement_signed",
            "ACTIVE",
            side_effects=(
                "aud",
                "event:merchant.activated",
                "ledger:create merchant_settlement account",
            ),
        ),
        _R("AGREEMENT_PENDING", "auto_expire", "REJECTED"),  # 14 days unsigned
        _R("PENDING_HUMAN_REVIEW", "operator_manual_verify", "KYB_IN_PROGRESS"),
        _R("PENDING_HUMAN_REVIEW", "operator_reject", "REJECTED"),
        _R(
            "ACTIVE",
            "aml_triggered",
            "SUSPENDED",
            side_effects=("aud", "event:merchant.suspended", "set:payout_blocked"),
        ),
        _R(
            "ACTIVE",
            "license_revoked",
            "TERMINATED",
            side_effects=("aud", "event:merchant.terminated", "set:payout_blocked"),
        ),
        _R(
            "ACTIVE",
            "kyb_refresh_due",
            "KYB_REFRESH_PENDING",
            side_effects=("aud", "event:merchant.kyb_refresh_due"),
        ),
        _R(
            "KYB_REFRESH_PENDING",
            "refresh_complete",
            "ACTIVE",
            side_effects=("aud", "event:merchant.kyb_refreshed"),
        ),
        _R(
            "KYB_REFRESH_PENDING",
            "refresh_failed",
            "SUSPENDED",
            side_effects=("aud", "event:merchant.suspended"),
        ),
        _R(
            "SUSPENDED",
            "camlco_cleared",
            "ACTIVE",
            side_effects=("aud", "event:merchant.unsuspended", "clear:payout_blocked"),
        ),
        _R(
            "SUSPENDED",
            "camlco_terminated",
            "TERMINATED",
            side_effects=("aud", "event:merchant.terminated"),
        ),
    ),
)

# ---------------------------------------------------------------------------
# 8-FSM-2: PSO Participant Onboarding
# ---------------------------------------------------------------------------

PARTICIPANT_STATES = (
    "APPLICATION_SUBMITTED",
    "LICENSE_VERIFICATION_PENDING",
    "SETTLEMENT_AGREEMENT_PENDING",
    "NDC_ASSIGNMENT_PENDING",
    "ACTIVE",
    "SUSPENDED",
    "REJECTED",
    "TERMINATED",
)

PARTICIPANT_TABLE = TransitionTable(
    "ParticipantOnboarding",
    states=PARTICIPANT_STATES,
    terminal_states=("REJECTED", "TERMINATED"),
    rules=(
        _R(
            "APPLICATION_SUBMITTED",
            "application_accepted",
            "LICENSE_VERIFICATION_PENDING",
            side_effects=("aud", "event:participant.application_submitted"),
        ),
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
        _R(
            "NDC_ASSIGNMENT_PENDING",
            "ndc_assigned",
            "ACTIVE",
            side_effects=(
                "aud",
                "event:participant.activated",
                "ledger:create position + net_debit_cap accounts",
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
    ),
)

# ---------------------------------------------------------------------------
# 8-FSM-3: Document review
# ---------------------------------------------------------------------------

DOCUMENT_REVIEW_STATES = ("PENDING", "OCR_COMPLETE", "ACCEPTED", "REJECTED", "SUPERSEDED")

DOCUMENT_REVIEW_TABLE = TransitionTable(
    "KybDocumentReview",
    states=DOCUMENT_REVIEW_STATES,
    terminal_states=("SUPERSEDED",),
    rules=(
        _R("PENDING", "ocr_complete", "OCR_COMPLETE"),
        _R("OCR_COMPLETE", "operator_accepted", "ACCEPTED"),
        _R("OCR_COMPLETE", "operator_rejected", "REJECTED"),
        _R("PENDING", "operator_accepted", "ACCEPTED"),  # manual review without OCR
        _R("ACCEPTED", "superseded", "SUPERSEDED"),
        _R("REJECTED", "re_uploaded", "PENDING"),
    ),
)

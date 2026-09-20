"""KYC entity records (spec/09 data model, in-process shape).

The raw NID never appears in a record: only ``nid_hash`` (one-way) and
``nid_encrypted`` (AES-256-GCM ciphertext via the injected cipher port).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from bdpay.kernel.kyc.kyc_states import (
    KYC_MANUAL_REVIEW_TABLE,
    KYC_REJECTION_REASONS,
    KYC_REVIEW_REASONS,
    KYC_TABLE,
    KYC_TIERS,
)
from bdpay.platform.errors import InvalidRequestError

__all__ = ["BiometricAttemptRecord", "CustomerRecord", "KycManualReviewRecord", "KycRecordModel"]

_RISK_TIERS = ("LOW", "MEDIUM", "HIGH")
_NID_TYPES = ("SMART", "LAMINATED", "DIGITAL")
_CHANNELS = ("SELF_SERVICE", "AGENT_ASSISTED")


@dataclass(frozen=True)
class CustomerRecord:
    """One ``customers`` row (referenced by spec/09; authored DDL, errata K-14)."""

    customer_id: str
    full_name_en: str
    created_at: datetime
    updated_at: datetime
    full_name_bn: str | None = None
    is_corporate: bool = False
    schema_version: int = 1


@dataclass(frozen=True)
class KycRecordModel:
    """One ``kyc_records`` row (spec/09 DDL, kernel-used columns)."""

    kyc_record_id: str
    customer_id: str
    created_at: datetime
    updated_at: datetime
    state: str = "SUBMITTED"
    tier_requested: str | None = None
    tier: str | None = None
    tier_assigned_at: datetime | None = None
    previous_kyc_record_id: str | None = None
    channel: str = "SELF_SERVICE"
    is_minor: bool = False
    guardian_customer_id: str | None = None
    nid_hash: str | None = None
    nid_encrypted: bytes | None = None
    nid_type: str | None = None
    nid_dob_iso: str | None = None
    name_en_norm: str | None = None
    name_bn_norm: str | None = None
    risk_tier: str = "LOW"
    refresh_due_at: datetime | None = None
    refresh_hard_deadline_at: datetime | None = None
    rejection_reason_code: str | None = None
    submitted_at: datetime | None = None
    last_biometric_at: datetime | None = None
    archived_at: datetime | None = None
    expired_at: datetime | None = None
    produced_by: str = "kyc-service@1"
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.state not in KYC_TABLE.states:
            raise InvalidRequestError(f"unknown kyc state {self.state!r}")
        for name in ("tier_requested", "tier"):
            value = getattr(self, name)
            if value is not None and value not in KYC_TIERS:
                raise InvalidRequestError(f"unknown {name} {value!r}")
        if self.channel not in _CHANNELS:
            raise InvalidRequestError(f"unknown channel {self.channel!r}")
        if self.risk_tier not in _RISK_TIERS:
            raise InvalidRequestError(f"unknown risk_tier {self.risk_tier!r}")
        if self.nid_type is not None and self.nid_type not in _NID_TYPES:
            raise InvalidRequestError(f"unknown nid_type {self.nid_type!r}")
        if self.rejection_reason_code is not None and (
            self.rejection_reason_code not in KYC_REJECTION_REASONS
        ):
            raise InvalidRequestError(
                f"unknown rejection_reason_code {self.rejection_reason_code!r}"
            )
        if self.state == "REJECTED" and self.rejection_reason_code is None:
            raise InvalidRequestError(
                "REJECTED requires a rejection_reason_code (spec/09 mandatory reason)"
            )


@dataclass(frozen=True)
class BiometricAttemptRecord:
    """One ``kyc_biometric_attempts`` row — score-free binary outcome."""

    attempt_id: str
    kyc_record_id: str
    attempt_number: int
    attempted_at: datetime
    matched: bool
    porichoy_response_hash: str
    porichoy_ref: str | None = None
    connector_result_id: str | None = None
    liveness_token_valid: bool = True
    liveness_sdk_version: str | None = None
    schema_version: int = 1

    def __post_init__(self) -> None:
        if not 1 <= self.attempt_number <= 3:
            raise InvalidRequestError("attempt_number must be in 1..3 (spec/09 retry cap)")


@dataclass(frozen=True)
class KycManualReviewRecord:
    """One ``kyc_manual_reviews`` row (spec/09 §9.2)."""

    review_id: str
    kyc_record_id: str
    review_reason: str
    created_at: datetime
    review_state: str = "REVIEW_OPEN"
    approval_request_id: str | None = None
    assigned_to_operator_id: str | None = None
    reviewer_notes: str | None = None
    decision: str | None = None
    rejection_reason_code: str | None = None
    decided_at: datetime | None = None
    decided_by_operator_id: str | None = None
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.review_state not in KYC_MANUAL_REVIEW_TABLE.states:
            raise InvalidRequestError(f"unknown review_state {self.review_state!r}")
        if self.review_reason not in KYC_REVIEW_REASONS:
            raise InvalidRequestError(f"unknown review_reason {self.review_reason!r}")
        if self.decision is not None and self.decision not in (
            "APPROVE_SIMPLIFIED",
            "APPROVE_REGULAR",
            "REJECT",
        ):
            raise InvalidRequestError(f"unknown decision {self.decision!r}")

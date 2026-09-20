"""KYB entity records (spec/08 data model, in-process shape)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from bdpay.kernel.onboarding.kyb_states import (
    DOCUMENT_REVIEW_TABLE,
    DOCUMENT_TYPES,
    MERCHANT_KYB_TABLE,
    REGISTRATION_TYPES,
)
from bdpay.platform.errors import InvalidRequestError

__all__ = [
    "ApiKeyIssuanceRecord",
    "KybDocumentRecord",
    "KybRecord",
    "MerchantRecord",
    "UboEdgeRecord",
    "UboNodeRecord",
]

_RISK_TIERS = ("LOW", "MEDIUM", "HIGH")
_NODE_TYPES = ("NATURAL_PERSON", "LEGAL_ENTITY")
_PORICHOY_STATUSES = (
    "NOT_REQUIRED",
    "PENDING",
    "VERIFIED",
    "MISMATCH",
    "UNAVAILABLE",
    "MANUALLY_VERIFIED",
)
_SANCTIONS_STATUSES = ("PENDING", "CLEAR", "HIT_UNREVIEWED", "HIT_REVIEWED", "CONFIRMED_HIT")


@dataclass(frozen=True)
class MerchantRecord:
    """One ``merchants`` row — the fast pre-flight mirror (spec/08 §10)."""

    merchant_id: str
    legal_name: str
    created_at: datetime
    updated_at: datetime
    kyb_status: str = "APPLICATION_SUBMITTED"
    kyb_record_id: str | None = None
    risk_tier: str | None = None
    payout_blocked: bool = False
    mdr_basis_points: int | None = None
    settlement_cycle_days: int | None = None
    activated_at: datetime | None = None
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.kyb_status not in MERCHANT_KYB_TABLE.states:
            raise InvalidRequestError(f"unknown kyb_status {self.kyb_status!r}")
        if self.risk_tier is not None and self.risk_tier not in _RISK_TIERS:
            raise InvalidRequestError(f"unknown risk_tier {self.risk_tier!r}")


@dataclass(frozen=True)
class KybRecord:
    """One ``kyb_records`` row (spec/08 DDL, kernel-used columns)."""

    kyb_record_id: str
    subject_type: str
    subject_id: str
    application_submitted_at: datetime
    created_at: datetime
    updated_at: datetime
    kyb_status: str = "APPLICATION_SUBMITTED"
    risk_tier: str | None = None
    legal_name: str | None = None
    legal_name_bn: str | None = None
    merchant_display_name: str | None = None
    registration_type: str | None = None
    tin_number: str | None = None
    primary_business_mcc: str | None = None
    requested_services: tuple[str, ...] = field(default_factory=tuple)
    approved_services: tuple[str, ...] = field(default_factory=tuple)
    contact_phone_e164: str | None = None
    registered_address: dict[str, Any] = field(default_factory=dict)
    bank_routing_number: str | None = None
    website_url: str | None = None
    bangla_qr_merchant_pan: str | None = None
    bangla_qr_enabled: bool = False
    mdr_basis_points: int | None = None
    settlement_cycle_days: int | None = None
    rolling_reserve_pct: int | None = None
    sanctions_cleared_at: datetime | None = None
    sanctions_screened_at: datetime | None = None
    approval_request_id: str | None = None
    kyb_submitted_at: datetime | None = None
    activated_at: datetime | None = None
    rejected_at: datetime | None = None
    rejection_reason_code: str | None = None
    rejection_notes: str | None = None
    suspended_at: datetime | None = None
    suspension_reason: str | None = None
    terminated_at: datetime | None = None
    termination_reason: str | None = None
    next_refresh_due_at: datetime | None = None
    last_refreshed_at: datetime | None = None
    refresh_interval_months: int = 24
    last_document_request_at: datetime | None = None
    agreement_requested_at: datetime | None = None
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.subject_type not in ("MERCHANT", "PARTICIPANT"):
            raise InvalidRequestError(f"unknown subject_type {self.subject_type!r}")
        if self.kyb_status not in MERCHANT_KYB_TABLE.states:
            raise InvalidRequestError(f"unknown kyb_status {self.kyb_status!r}")
        if self.risk_tier is not None and self.risk_tier not in _RISK_TIERS:
            raise InvalidRequestError(f"unknown risk_tier {self.risk_tier!r}")
        if not isinstance(self.registered_address, dict):
            raise InvalidRequestError("registered_address must be a mapping")
        if self.merchant_display_name is not None and not self.merchant_display_name.strip():
            raise InvalidRequestError("merchant_display_name cannot be blank")
        if self.bangla_qr_merchant_pan is not None:
            pan = self.bangla_qr_merchant_pan.strip()
            if not pan:
                raise InvalidRequestError("bangla_qr_merchant_pan cannot be blank")
            if not pan.isascii():
                raise InvalidRequestError("bangla_qr_merchant_pan must be ASCII")
        if self.bangla_qr_enabled and self.bangla_qr_merchant_pan is None:
            raise InvalidRequestError(
                "bangla_qr_merchant_pan is required when Bangla QR is enabled"
            )
        if self.registration_type is not None and (
            self.registration_type not in REGISTRATION_TYPES
        ):
            raise InvalidRequestError(
                f"unknown registration_type {self.registration_type!r}"
            )
        if self.settlement_cycle_days is not None and not (
            1 <= self.settlement_cycle_days <= 7
        ):
            raise InvalidRequestError("settlement_cycle_days must be in 1..7")
        if self.rolling_reserve_pct is not None and not (
            0 <= self.rolling_reserve_pct <= 2500
        ):
            raise InvalidRequestError("rolling_reserve_pct must be in 0..2500 basis points")


@dataclass(frozen=True)
class KybDocumentRecord:
    """One ``kyb_documents`` row (spec/08 DDL)."""

    document_id: str
    kyb_record_id: str
    document_type: str
    content_hash: str
    storage_pointer: str
    uploaded_at: datetime
    uploaded_by: str
    review_status: str = "PENDING"
    ocr_status: str = "PENDING"
    ubo_node_id: str | None = None
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None
    review_notes: str | None = None
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.document_type not in DOCUMENT_TYPES:
            raise InvalidRequestError(f"unknown document_type {self.document_type!r}")
        if self.review_status not in DOCUMENT_REVIEW_TABLE.states:
            raise InvalidRequestError(f"unknown review_status {self.review_status!r}")


@dataclass(frozen=True)
class UboNodeRecord:
    """One ``kyb_ubo_nodes`` row. Raw NID never appears here — hash only."""

    ubo_node_id: str
    kyb_record_id: str
    node_type: str
    full_name_en: str
    created_at: datetime
    created_by: str
    full_name_bn: str | None = None
    nid_number_hash: str | None = None
    dob: date | None = None
    nationality: str = "BD"
    is_ultimate_beneficial_owner: bool = False
    ownership_percentage_direct: Decimal | None = None
    role: str = "SHAREHOLDER"
    porichoy_status: str = "PENDING"
    porichoy_ref: str | None = None
    porichoy_verified_at: datetime | None = None
    manual_verify_by: str | None = None
    manual_verify_at: datetime | None = None
    manual_verify_note: str | None = None
    sanctions_status: str = "PENDING"
    sanctions_screened_at: datetime | None = None
    sanctions_hit_id: str | None = None
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.node_type not in _NODE_TYPES:
            raise InvalidRequestError(f"unknown node_type {self.node_type!r}")
        if self.porichoy_status not in _PORICHOY_STATUSES:
            raise InvalidRequestError(f"unknown porichoy_status {self.porichoy_status!r}")
        if self.sanctions_status not in _SANCTIONS_STATUSES:
            raise InvalidRequestError(f"unknown sanctions_status {self.sanctions_status!r}")
        if self.ownership_percentage_direct is not None:
            pct = self.ownership_percentage_direct
            if not isinstance(pct, Decimal):
                raise InvalidRequestError(
                    "ownership_percentage_direct must be Decimal (floats rejected)"
                )
            if pct < 0 or pct > 100:
                raise InvalidRequestError("ownership_percentage_direct must be in 0..100")


@dataclass(frozen=True)
class UboEdgeRecord:
    """One directed ``kyb_ubo_edges`` row (parent owns child)."""

    edge_id: str
    kyb_record_id: str
    parent_node_id: str
    child_node_id: str
    created_at: datetime
    ownership_percentage: Decimal | None = None

    def __post_init__(self) -> None:
        if self.parent_node_id == self.child_node_id:
            raise InvalidRequestError("UBO edge cannot be a self-loop")


@dataclass(frozen=True)
class ApiKeyIssuanceRecord:
    """API-key issuance record — the secret is hash-stored, never plaintext."""

    issuance_id: str
    merchant_id: str
    key_id: str
    secret_hash: str  # sha256 hex of the secret; plaintext returned once, never stored
    issued_at: datetime
    issued_by: str
    schema_version: int = 1

    def __post_init__(self) -> None:
        if len(self.secret_hash) != 64:
            raise InvalidRequestError("secret_hash must be a sha256 hex digest")

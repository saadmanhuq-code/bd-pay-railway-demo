"""Disbursement value records and validation report shapes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

__all__ = [
    "DisbursementBatch",
    "DisbursementBatchResult",
    "DisbursementItem",
    "DisbursementItemInput",
    "ValidationRow",
]


@dataclass(frozen=True, slots=True)
class DisbursementItemInput:
    """One intake row from API, CSV mapper, or internal launch consumer."""

    beneficiary_ref: str
    beneficiary_name: str
    rail: str
    amount_minor: int | str
    purpose_code: str
    item_ref: str


@dataclass(frozen=True, slots=True)
class ValidationRow:
    """Per-row validation result returned to the caller."""

    ordinal: int
    item_ref: str | None
    accepted: bool
    codes: tuple[str, ...]
    item_id: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "ordinal": self.ordinal,
            "item_ref": self.item_ref,
            "accepted": self.accepted,
            "codes": list(self.codes),
            "item_id": self.item_id,
        }


@dataclass(frozen=True, slots=True)
class DisbursementBatch:
    """DisbursementBatch entity (spec/17 VX4)."""

    batch_id: str
    merchant_id: str
    client_batch_ref: str
    item_count: int
    total_amount_minor: int
    fees_total_minor: int
    status: str
    maker_user_id: str
    approval_request_id: str | None
    reconciliation_tag: str
    created_at: datetime
    submitted_at: datetime | None = None
    approved_at: datetime | None = None
    dispatched_at: datetime | None = None
    completed_at: datetime | None = None
    cancelled_at: datetime | None = None
    schema_version: int = 1

    def to_dict(self) -> dict[str, object]:
        return {
            "batch_id": self.batch_id,
            "merchant_id": self.merchant_id,
            "client_batch_ref": self.client_batch_ref,
            "item_count": self.item_count,
            "total_amount_minor": self.total_amount_minor,
            "fees_total_minor": self.fees_total_minor,
            "status": self.status,
            "maker_user_id": self.maker_user_id,
            "approval_request_id": self.approval_request_id,
            "reconciliation_tag": self.reconciliation_tag,
            "created_at": _rfc3339(self.created_at),
            "submitted_at": _maybe_rfc3339(self.submitted_at),
            "approved_at": _maybe_rfc3339(self.approved_at),
            "dispatched_at": _maybe_rfc3339(self.dispatched_at),
            "completed_at": _maybe_rfc3339(self.completed_at),
            "cancelled_at": _maybe_rfc3339(self.cancelled_at),
        }


@dataclass(frozen=True, slots=True)
class DisbursementItem:
    """DisbursementItem entity (spec/17 VX4)."""

    item_id: str
    batch_id: str
    ordinal: int
    beneficiary_ref: str
    beneficiary_name_normalized: str
    rail: str
    amount_minor: int
    fee_minor: int
    purpose_code: str
    item_ref: str
    status: str
    rail_transaction_id: str | None = None
    settled_at: datetime | None = None
    screened_at: datetime | None = None
    screening_list_version: str | None = None
    schema_version: int = 1

    def to_dict(self) -> dict[str, object]:
        return {
            "item_id": self.item_id,
            "batch_id": self.batch_id,
            "ordinal": self.ordinal,
            "beneficiary_ref": self.beneficiary_ref,
            "beneficiary_name": self.beneficiary_name_normalized,
            "rail": self.rail,
            "amount_minor": self.amount_minor,
            "fee_minor": self.fee_minor,
            "purpose_code": self.purpose_code,
            "item_ref": self.item_ref,
            "status": self.status,
            "rail_transaction_id": self.rail_transaction_id,
            "settled_at": _maybe_rfc3339(self.settled_at),
            "screened_at": _maybe_rfc3339(self.screened_at),
            "screening_list_version": self.screening_list_version,
        }


@dataclass(frozen=True, slots=True)
class DisbursementBatchResult:
    """Batch intake result with the caller-visible validation report."""

    batch: DisbursementBatch
    validation_report: tuple[ValidationRow, ...]

    def to_dict(self) -> dict[str, object]:
        out = self.batch.to_dict()
        out["validation_report"] = [row.to_dict() for row in self.validation_report]
        return out


def _rfc3339(at: datetime) -> str:
    return at.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _maybe_rfc3339(at: datetime | None) -> str | None:
    return None if at is None else _rfc3339(at)

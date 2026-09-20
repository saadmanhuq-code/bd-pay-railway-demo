"""Kernel entity records (spec/02 data model, in-process shape).

Frozen dataclasses mirroring the spec/02 DDL columns the kernel reads and
writes. Money is integer paisa (``amount_minor``); timestamps are aware UTC
datetimes from the injected clock; IDs are content-addressed. Validation
fails closed at construction — an invalid record can never exist in memory.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime

from bdpay.kernel.payment_states import (
    ATTEMPT_TABLE,
    CAPTURE_METHODS,
    INTENT_TABLE,
    PAYMENT_METHODS,
    REFUND_REASONS,
    REFUND_TABLE,
)
from bdpay.platform.errors import InvalidRequestError

__all__ = [
    "MAX_METADATA_KEYS",
    "MAX_METADATA_VALUE_LENGTH",
    "PaymentAttemptRecord",
    "PaymentIntentRecord",
    "RefundRecord",
]

MAX_METADATA_KEYS = 10
MAX_METADATA_VALUE_LENGTH = 64


def _require_positive_minor(value: object, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidRequestError(
            f"{what} must be int paisa, got {type(value).__name__}",
            code="amount_must_be_integer",
        )
    if value <= 0:
        raise InvalidRequestError(f"{what} must be > 0 paisa", code="amount_not_positive")
    return value


def _require_aware(value: datetime, what: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise InvalidRequestError(f"{what} must be a timezone-aware datetime")
    return value


def validate_metadata(metadata: Mapping[str, str]) -> dict[str, str]:
    """spec/02: string-only KV; max 10 keys; max 64 chars per value."""
    if not isinstance(metadata, Mapping):
        raise InvalidRequestError("metadata must be a mapping", code="metadata_invalid")
    if len(metadata) > MAX_METADATA_KEYS:
        raise InvalidRequestError(
            f"metadata allows at most {MAX_METADATA_KEYS} keys", code="metadata_invalid"
        )
    out: dict[str, str] = {}
    for key, value in metadata.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise InvalidRequestError(
                "metadata keys and values must be strings", code="metadata_invalid"
            )
        if len(value) > MAX_METADATA_VALUE_LENGTH:
            raise InvalidRequestError(
                f"metadata values are capped at {MAX_METADATA_VALUE_LENGTH} chars",
                code="metadata_invalid",
            )
        out[key] = value
    return out


@dataclass(frozen=True)
class PaymentIntentRecord:
    """One ``payment_intents`` row (spec/02 DDL)."""

    payment_intent_id: str
    merchant_id: str
    amount_minor: int
    method: str
    expires_at: datetime
    created_at: datetime
    updated_at: datetime
    customer_id: str | None = None
    currency: str = "BDT"
    capture_method: str = "automatic"
    status: str = "CREATED"
    description: str | None = None
    statement_descriptor: str | None = None
    metadata: Mapping[str, str] = field(default_factory=dict)
    idempotency_key: str | None = None
    client_secret_hash: str | None = None
    latest_attempt_id: str | None = None
    succeeded_at: datetime | None = None
    delivery_confirmed_at: datetime | None = None
    cancelled_at: datetime | None = None
    reversed_at: datetime | None = None
    # spec/16 §G: the kernel's scheduled redispatch instant while the intent
    # sits in store-and-forward deferral (spec/02); None otherwise.
    deferred_until: datetime | None = None
    schema_version: int = 1

    def __post_init__(self) -> None:
        _require_positive_minor(self.amount_minor, "amount_minor")
        if self.currency != "BDT":
            raise InvalidRequestError("currency must be BDT in v1", code="currency_unsupported")
        if self.method not in PAYMENT_METHODS:
            raise InvalidRequestError(
                f"method must be one of {PAYMENT_METHODS}", code="method_unsupported"
            )
        if self.capture_method not in CAPTURE_METHODS:
            raise InvalidRequestError(
                f"capture_method must be one of {CAPTURE_METHODS}",
                code="capture_method_invalid",
            )
        if self.status not in INTENT_TABLE.states:
            raise InvalidRequestError(f"unknown intent status {self.status!r}")
        object.__setattr__(self, "metadata", validate_metadata(self.metadata))
        for name in ("expires_at", "created_at", "updated_at"):
            _require_aware(getattr(self, name), name)


@dataclass(frozen=True)
class PaymentAttemptRecord:
    """One ``payment_attempts`` row (spec/02 DDL)."""

    attempt_id: str
    payment_intent_id: str
    attempt_number: int
    connector_id: str
    instruction_id: str
    connector_ref: str
    amount_minor: int
    created_at: datetime
    updated_at: datetime
    status: str = "STARTED"
    currency: str = "BDT"
    rail_transaction_id: str | None = None
    error_code: str | None = None
    raw_response_hash: str | None = None
    poll_count: int = 0
    authorized_at: datetime | None = None
    auth_expires_at: datetime | None = None
    charged_at: datetime | None = None
    failed_at: datetime | None = None
    schema_version: int = 1

    def __post_init__(self) -> None:
        _require_positive_minor(self.amount_minor, "amount_minor")
        if not 1 <= self.attempt_number <= 10:
            raise InvalidRequestError("attempt_number must be in 1..10")
        if self.status not in ATTEMPT_TABLE.states:
            raise InvalidRequestError(f"unknown attempt status {self.status!r}")
        if self.poll_count < 0:
            raise InvalidRequestError("poll_count must be >= 0")
        for name in ("created_at", "updated_at"):
            _require_aware(getattr(self, name), name)


@dataclass(frozen=True)
class RefundRecord:
    """One ``refunds`` row (spec/02 DDL). A refund is a NEW offsetting flow;
    the original charge is never mutated."""

    refund_id: str
    payment_intent_id: str
    attempt_id: str
    amount_minor: int
    reason: str
    created_at: datetime
    updated_at: datetime
    status: str = "REFUND_INITIATED"
    currency: str = "BDT"
    connector_id: str | None = None
    connector_ref: str | None = None
    instruction_id: str | None = None
    rail_transaction_id: str | None = None
    raw_response_hash: str | None = None
    retry_count: int = 0
    metadata: Mapping[str, str] = field(default_factory=dict)
    idempotency_key: str | None = None
    succeeded_at: datetime | None = None
    failed_at: datetime | None = None
    schema_version: int = 1

    def __post_init__(self) -> None:
        _require_positive_minor(self.amount_minor, "amount_minor")
        if self.reason not in REFUND_REASONS:
            raise InvalidRequestError(
                f"reason must be one of {REFUND_REASONS}", code="refund_reason_invalid"
            )
        if self.status not in REFUND_TABLE.states:
            raise InvalidRequestError(f"unknown refund status {self.status!r}")
        if self.retry_count < 0:
            raise InvalidRequestError("retry_count must be >= 0")
        object.__setattr__(self, "metadata", validate_metadata(self.metadata))
        for name in ("created_at", "updated_at"):
            _require_aware(getattr(self, name), name)

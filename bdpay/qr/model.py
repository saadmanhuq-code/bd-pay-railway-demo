"""QR entities and content-addressed IDs — spec/13 §Entities, §Data model.

ID prefixes ``mqr``/``qrp``/``qres``/``qtest`` are registered additively by
spec/13 to the conventions §3 table. ``bdpay.platform.ids.PREFIXES`` is frozen
without them, so this module derives IDs through the same E12 primitive
(``sha256_canonical``) with the identical ``<prefix>_<hash[:24]>`` form — see
lane-B errata.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from bdpay.platform.canonical import sha256_canonical
from bdpay.platform.ids import HASH_LENGTH
from bdpay.qr.codec import QrType

__all__ = [
    "QR_ID_PREFIXES",
    "DynamicPayloadState",
    "InteropDirection",
    "MerchantQr",
    "MerchantQrState",
    "QrInteropTestCase",
    "QrPayload",
    "QrScanResolution",
    "ValidationResult",
    "qr_make_id",
]

#: spec/13 §Entities — additive prefixes (see module docstring).
QR_ID_PREFIXES: frozenset[str] = frozenset({"mqr", "qrp", "qres", "qtest"})


def qr_make_id(prefix: str, payload: dict) -> str:
    """``<prefix>_<sha256_canonical(payload)[:24]>`` for the QR prefixes."""
    if prefix not in QR_ID_PREFIXES:
        raise ValueError(f"unknown qr id prefix {prefix!r}; allowed: {sorted(QR_ID_PREFIXES)}")
    if not isinstance(payload, dict):
        raise TypeError(f"payload must be dict, got {type(payload).__name__}")
    return f"{prefix}_{sha256_canonical(payload)[:HASH_LENGTH]}"


class MerchantQrState(StrEnum):
    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"
    REVOKED = "REVOKED"  # terminal


class DynamicPayloadState(StrEnum):
    ISSUED = "ISSUED"
    SCANNED = "SCANNED"
    PAID = "PAID"  # terminal
    EXPIRED = "EXPIRED"  # terminal
    CANCELLED = "CANCELLED"  # terminal


class ValidationResult(StrEnum):
    VALID = "VALID"
    INVALID = "INVALID"


class InteropDirection(StrEnum):
    WE_ISSUE = "WE_ISSUE"
    WE_SCAN = "WE_SCAN"


@dataclass(frozen=True)
class MerchantQr:
    """A static QR issued to a merchant — long-lived artifact (spec/13)."""

    merchant_qr_id: str
    merchant_id: str
    state: MerchantQrState
    label: str
    store_id: str | None
    terminal_id: str | None
    current_payload_id: str | None
    suspend_reason: str | None
    created_at: datetime
    updated_at: datetime
    schema_version: int = 1


@dataclass(frozen=True)
class QrPayload:
    """One encoded payload; content-addressed by its string (spec/13)."""

    payload_id: str
    qr_type: QrType
    merchant_qr_id: str | None  # static
    payment_intent_id: str | None  # dynamic
    state: DynamicPayloadState  # dynamic FSM; static rows stay ISSUED
    payload_string: str
    payload_hash: str
    amount_minor: int | None  # dynamic only
    currency: str
    mcc: str
    merchant_name: str
    merchant_name_bn: str | None
    merchant_city: str
    expires_at: datetime | None  # dynamic only
    created_at: datetime
    schema_version: int = 1


@dataclass(frozen=True)
class QrScanResolution:
    """Append-only scan event — every scan recorded, refusals included."""

    resolution_id: str
    payload_hash: str
    payload_id: str | None  # set when the payload is ours
    on_us: bool
    customer_ref: str
    qr_type: str  # STATIC/DYNAMIC/UNKNOWN (parse failures may not know)
    validation_result: ValidationResult
    failure_code: str | None
    quoted_fee_minor: int | None
    payment_intent_id: str | None
    scanned_at: datetime
    schema_version: int = 1


@dataclass(frozen=True)
class QrInteropTestCase:
    """Golden corpus / counterpart-app interop matrix row (spec/13 §F)."""

    test_case_id: str
    name: str
    version: int
    direction: InteropDirection
    payload_string: str
    expected: dict  # {"valid": bool, "failure_code": ..., "fields": {...}}
    counterparty_class: str | None
    last_result: str | None  # PASS/FAIL/UNRUN
    last_run_at: datetime | None
    created_at: datetime
    schema_version: int = 1

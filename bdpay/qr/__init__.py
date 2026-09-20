"""Bangla QR package — spec/13-qr-bangla-emvco.md (EMVCo QRCPS MPM profile).

Public surface: the TLV codec (`encode_payload`/`parse_and_validate`), the
CRC-16/CCITT-FALSE reference (`crc16_ccitt_false`/`append_crc`/`verify_crc`),
the tag-54 paisa boundary, the golden corpus, the NPSB dictionary rows, and
:class:`~bdpay.qr.service.QrService`.
"""

from bdpay.qr.amount54 import parse_amount_54, render_amount_54
from bdpay.qr.codec import (
    AdditionalData,
    LanguageTemplate,
    MerchantAccountInfo,
    ParsedPayload,
    QrEncodeRequest,
    QrType,
    RouteClass,
    encode_payload,
    parse_and_validate,
    payload_hash_of,
)
from bdpay.qr.config import (
    MAX_PAYLOAD_BYTES,
    RESOLUTION_TTL_SECONDS,
    STATIC_LIMIT_MINOR,
    BanglaQrConfig,
)
from bdpay.qr.corpus import GoldenVector, golden_corpus, interop_cases
from bdpay.qr.crc import append_crc, crc16_ccitt_false, verify_crc
from bdpay.qr.errors import QR_FAILURE_CODES, QrEncodeError, QrValidationError
from bdpay.qr.model import (
    DynamicPayloadState,
    InteropDirection,
    MerchantQr,
    MerchantQrState,
    QrInteropTestCase,
    QrPayload,
    QrScanResolution,
    ValidationResult,
    qr_make_id,
)
from bdpay.qr.npsb import npsb_qr_dictionary_rows
from bdpay.qr.repository import InMemoryQrRepository, PostgresQrRepository, QrRepository
from bdpay.qr.service import QrService

__all__ = [
    "MAX_PAYLOAD_BYTES",
    "QR_FAILURE_CODES",
    "RESOLUTION_TTL_SECONDS",
    "STATIC_LIMIT_MINOR",
    "AdditionalData",
    "BanglaQrConfig",
    "DynamicPayloadState",
    "GoldenVector",
    "InMemoryQrRepository",
    "InteropDirection",
    "LanguageTemplate",
    "MerchantAccountInfo",
    "MerchantQr",
    "MerchantQrState",
    "ParsedPayload",
    "PostgresQrRepository",
    "QrEncodeError",
    "QrEncodeRequest",
    "QrInteropTestCase",
    "QrPayload",
    "QrRepository",
    "QrScanResolution",
    "QrService",
    "QrType",
    "QrValidationError",
    "RouteClass",
    "ValidationResult",
    "append_crc",
    "crc16_ccitt_false",
    "encode_payload",
    "golden_corpus",
    "interop_cases",
    "npsb_qr_dictionary_rows",
    "parse_amount_54",
    "parse_and_validate",
    "payload_hash_of",
    "qr_make_id",
    "render_amount_54",
    "verify_crc",
]

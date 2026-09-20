"""EMVCo QRCPS merchant-presented codec, Bangla QR profile — spec/13 §A/§C.

Encoder: root objects in ascending-ID order, ID 00 first, CRC (ID 63) last,
512-byte budget with the deterministic trim order (ID 64 Bengali content
first, grapheme-boundary; then ID 62 optional sub-fields).

Decoder/validator: the normative spec/13 §C algorithm, steps 1-6. Steps 7-8
(merchant lookup, resolution persistence) live in service.py. The decoder
accepts any root-object order (spec/13 §A binding) but rejects duplicates and
reserved IDs fail-closed, and requires ID 63 last.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from bdpay.platform.canonical import sha256_canonical
from bdpay.qr.amount54 import parse_amount_54, render_amount_54
from bdpay.qr.config import (
    MAX_MERCHANT_CITY_CHARS,
    MAX_MERCHANT_NAME_CHARS,
    MAX_PAYLOAD_BYTES,
    MAX_POSTAL_CODE_CHARS,
    BanglaQrConfig,
)
from bdpay.qr.crc import append_crc, verify_crc
from bdpay.qr.errors import QrEncodeError, QrValidationError
from bdpay.qr.text import truncate_graphemes
from bdpay.qr.tlv import assemble, walk, walk_template

__all__ = [
    "AdditionalData",
    "LanguageTemplate",
    "MerchantAccountInfo",
    "ParsedPayload",
    "QrEncodeRequest",
    "QrType",
    "RouteClass",
    "encode_payload",
    "parse_and_validate",
    "payload_hash_of",
]

_BDT_NUMERIC = "050"
_COUNTRY_BD = "BD"
_PAYLOAD_FORMAT = "01"
_POI_STATIC = "11"
_POI_DYNAMIC = "12"

_MCC_RE = re.compile(r"^\d{4}$", re.ASCII)
_PCT_FEE_RE = re.compile(r"^\d{2}\.\d{2}$", re.ASCII)

_RESERVED_RANGE = range(65, 80)  # 65-79 RFU for EMVCo: reject fail-closed
_CARD_TEMPLATE_RANGE = range(2, 26)  # 02-25 card-scheme templates: passthrough
_MERCHANT_TEMPLATE_RANGE = range(26, 52)  # 26-51 merchant account templates


class QrType(StrEnum):
    STATIC = "STATIC"
    DYNAMIC = "DYNAMIC"


class RouteClass(StrEnum):
    BANGLA_QR = "BANGLA_QR"
    CARD_SCHEME = "CARD_SCHEME"


@dataclass(frozen=True)
class MerchantAccountInfo:
    """Bangla QR/NPSB merchant account template content (spec/13 §A sub-TLVs)."""

    root_id: int
    guid: str
    acquirer_id: str
    merchant_id: str  # merchant PAN-equivalent (sub-ID 02), opaque to payers
    psp_sub_id: str | None = None


@dataclass(frozen=True)
class AdditionalData:
    """ID 62 sub-fields (spec/13 §A)."""

    bill_number: str | None = None  # sub 01
    mobile: str | None = None  # sub 02
    store_label: str | None = None  # sub 03
    reference_label: str | None = None  # sub 05 (pi_ id for dynamic; recon join key)
    terminal_label: str | None = None  # sub 07
    consumer_data_request: str | None = None  # sub 09


@dataclass(frozen=True)
class LanguageTemplate:
    """ID 64 merchant information language template (spec/13 §A)."""

    language_preference: str = "bn"  # sub 00
    merchant_name: str | None = None  # sub 01, UTF-8 Bengali
    merchant_city: str | None = None  # sub 02


@dataclass(frozen=True)
class QrEncodeRequest:
    qr_type: QrType
    merchant_account: MerchantAccountInfo
    mcc: str
    merchant_name: str
    merchant_city: str
    amount_minor: int | None = None  # dynamic mandatory; static MUST be None
    postal_code: str | None = None
    tip_indicator: str | None = None  # "01" prompt / "02" fixed / "03" percentage
    convenience_fee_fixed_minor: int | None = None
    convenience_fee_percentage: str | None = None  # "00.01".."99.99"
    additional: AdditionalData | None = None
    language: LanguageTemplate | None = None


@dataclass(frozen=True)
class ParsedPayload:
    """spec/13 §C ``ValidatedPayload``: a payload that passed steps 1-6."""

    payload_string: str
    payload_hash: str
    qr_type: QrType
    route_class: RouteClass
    mcc: str
    merchant_name: str
    merchant_city: str
    country_code: str
    currency: str
    amount_minor: int | None
    tip_indicator: str | None
    convenience_fee_fixed_minor: int | None
    convenience_fee_percentage: str | None
    postal_code: str | None
    additional: AdditionalData
    language: LanguageTemplate | None
    bangla_template: MerchantAccountInfo | None
    card_template_ids: tuple[int, ...]


def payload_hash_of(payload_string: str) -> str:
    """Content address of a payload string — sha256_canonical per errata E12."""
    return sha256_canonical(payload_string)


# ---------------------------------------------------------------------------
# Encoder
# ---------------------------------------------------------------------------


def _require_ascii(value: str, what: str) -> str:
    if not value.isascii():
        raise QrEncodeError(f"{what} must be ASCII/Latin in this object (Bengali goes in ID 64)")
    return value


def _merchant_account_template(info: MerchantAccountInfo) -> tuple[int, str]:
    if info.root_id not in _MERCHANT_TEMPLATE_RANGE:
        raise QrEncodeError(f"merchant account template root ID must be 26-51, got {info.root_id}")
    subs: list[tuple[int, str]] = [(0, info.guid), (1, info.acquirer_id), (2, info.merchant_id)]
    if info.psp_sub_id:
        subs.append((3, info.psp_sub_id))
    return info.root_id, assemble(subs)


def _additional_data_template(data: AdditionalData) -> str | None:
    subs: list[tuple[int, str]] = []
    for sub_id, value in (
        (1, data.bill_number),
        (2, data.mobile),
        (3, data.store_label),
        (5, data.reference_label),
        (7, data.terminal_label),
        (9, data.consumer_data_request),
    ):
        if value:
            subs.append((sub_id, value))
    return assemble(subs) if subs else None


_TEMPLATE_VALUE_BUDGET = 99  # every template value is itself a 2-digit-length object


def _language_template(language: LanguageTemplate) -> str | None:
    """Build ID 64, trimming Bengali content to the 99-byte template budget
    on grapheme boundaries (spec/13 §A: sub 01 "<= template budget")."""
    subs: list[tuple[int, str]] = [(0, language.language_preference)]
    used = 4 + len(language.language_preference.encode("utf-8"))
    if language.merchant_name:
        name = truncate_graphemes(
            language.merchant_name, _TEMPLATE_VALUE_BUDGET - used - 4, byte_budget=True
        )
        if name:
            subs.append((1, name))
            used += 4 + len(name.encode("utf-8"))
    if language.merchant_city:
        city = truncate_graphemes(
            language.merchant_city, _TEMPLATE_VALUE_BUDGET - used - 4, byte_budget=True
        )
        if city:
            subs.append((2, city))
    if len(subs) == 1:
        return None  # nothing Bengali to carry; omit the template entirely
    return assemble(subs)


def _validate_tip_objects(request: QrEncodeRequest) -> None:
    tip = request.tip_indicator
    if tip is not None and tip not in {"01", "02", "03"}:
        raise QrEncodeError(f"tip indicator must be 01/02/03, got {tip!r}")
    if (request.convenience_fee_fixed_minor is not None) != (tip == "02"):
        raise QrEncodeError(
            "convenience fee fixed (ID 56) requires tip indicator 02, and 02 requires it"
        )
    if (request.convenience_fee_percentage is not None) != (tip == "03"):
        raise QrEncodeError(
            "convenience fee percentage (ID 57) requires tip indicator 03, and 03 requires it"
        )
    pct = request.convenience_fee_percentage
    if pct is not None and (not _PCT_FEE_RE.match(pct) or not "00.01" <= pct <= "99.99"):
        raise QrEncodeError(f"ID 57 must be '00.01'..'99.99', got {pct!r}")


def _trim_to_budget(
    base_objects: list[tuple[int, str]],
    language: LanguageTemplate | None,
    additional: AdditionalData | None,
) -> str:
    """Apply the spec/13 §A budget rule with a deterministic trim order.

    Order: ID 64 Bengali city dropped, then Bengali name grapheme-truncated,
    then ID 64 dropped; then ID 62 optional sub-fields dropped in the order
    consumer-data-request, mobile, bill-number, terminal-label, store-label
    (sub 05 reference label is never trimmed — it is the dynamic recon join
    key). Still over budget afterwards is an encode error, never an invalid
    payload.
    """

    def build(lang: LanguageTemplate | None, extra: AdditionalData | None) -> str:
        objects = list(base_objects)
        if extra is not None:
            value = _additional_data_template(extra)
            if value is not None:
                objects.append((62, value))
        if lang is not None:
            value = _language_template(lang)
            if value is not None:
                objects.append((64, value))
        objects.sort(key=lambda pair: pair[0])
        return append_crc(assemble(objects))

    def fits(candidate: str) -> bool:
        return len(candidate.encode("utf-8")) <= MAX_PAYLOAD_BYTES

    payload = build(language, additional)
    if fits(payload):
        return payload

    if language is not None and language.merchant_city:
        language = LanguageTemplate(language.language_preference, language.merchant_name, None)
        payload = build(language, additional)
        if fits(payload):
            return payload
    if language is not None and language.merchant_name:
        name = language.merchant_name
        overflow = len(payload.encode("utf-8")) - MAX_PAYLOAD_BYTES
        target = max(len(name.encode("utf-8")) - overflow, 0)
        trimmed = truncate_graphemes(name, target, byte_budget=True)
        language = LanguageTemplate(language.language_preference, trimmed or None, None)
        payload = build(language, additional)
        if fits(payload):
            return payload
    if language is not None:
        language = None
        payload = build(None, additional)
        if fits(payload):
            return payload
    if additional is not None:
        for field in (
            "consumer_data_request",
            "mobile",
            "bill_number",
            "terminal_label",
            "store_label",
        ):
            if getattr(additional, field) is not None:
                additional = AdditionalData(
                    **{
                        name: (None if name == field else getattr(additional, name))
                        for name in (
                            "bill_number",
                            "mobile",
                            "store_label",
                            "reference_label",
                            "terminal_label",
                            "consumer_data_request",
                        )
                    }
                )
                payload = build(None, additional)
                if fits(payload):
                    return payload
    raise QrEncodeError(
        f"payload exceeds {MAX_PAYLOAD_BYTES} bytes even after deterministic trimming"
    )


def encode_payload(request: QrEncodeRequest) -> str:
    """Encode a Bangla QR payload string (spec/13 §A profile, CRC appended)."""
    if not _MCC_RE.match(request.mcc):
        raise QrEncodeError(f"MCC must be 4 ASCII digits, got {request.mcc!r}")
    if request.qr_type is QrType.STATIC and request.amount_minor is not None:
        raise QrEncodeError("static QR must not carry an amount (ID 54 absent)")
    if request.qr_type is QrType.DYNAMIC and request.amount_minor is None:
        raise QrEncodeError("dynamic QR requires amount_minor")
    _validate_tip_objects(request)

    name = truncate_graphemes(
        _require_ascii(request.merchant_name, "merchant name (ID 59)").strip(),
        MAX_MERCHANT_NAME_CHARS,
    )
    city = truncate_graphemes(request.merchant_city.strip(), MAX_MERCHANT_CITY_CHARS)
    if not name or not city:
        raise QrEncodeError("merchant name and city are mandatory (IDs 59/60)")

    objects: list[tuple[int, str]] = [
        (0, _PAYLOAD_FORMAT),
        (1, _POI_STATIC if request.qr_type is QrType.STATIC else _POI_DYNAMIC),
        _merchant_account_template(request.merchant_account),
        (52, request.mcc),
        (53, _BDT_NUMERIC),
        (58, _COUNTRY_BD),
        (59, name),
        (60, city),
    ]
    if request.amount_minor is not None:
        objects.append((54, render_amount_54(request.amount_minor)))
    if request.tip_indicator is not None:
        objects.append((55, request.tip_indicator))
    if request.convenience_fee_fixed_minor is not None:
        objects.append((56, render_amount_54(request.convenience_fee_fixed_minor)))
    if request.convenience_fee_percentage is not None:
        objects.append((57, request.convenience_fee_percentage))
    if request.postal_code:
        postal = request.postal_code.strip()
        if len(postal) > MAX_POSTAL_CODE_CHARS:
            raise QrEncodeError(f"postal code exceeds {MAX_POSTAL_CODE_CHARS} chars")
        objects.append((61, postal))

    return _trim_to_budget(objects, request.language, request.additional)


# ---------------------------------------------------------------------------
# Decoder / validator (spec/13 §C steps 1-6)
# ---------------------------------------------------------------------------


def _check_crc_object(payload: str, objects: list) -> None:
    last = objects[-1]
    if last.object_id != 63 or len(last.value) != 4:
        raise QrValidationError("qr_invalid_crc", "ID 63 must be the last object, 4 chars")
    if not verify_crc(payload):
        raise QrValidationError("qr_invalid_crc", "CRC mismatch")


def _parse_tip(fields: dict[int, str]) -> tuple[str | None, int | None, str | None]:
    tip = fields.get(55)
    fee_fixed = fields.get(56)
    fee_pct = fields.get(57)
    if tip is not None and tip not in {"01", "02", "03"}:
        raise QrValidationError("qr_validation_failed", "ID 55 must be 01/02/03")
    if (fee_fixed is not None) != (tip == "02"):
        raise QrValidationError("qr_validation_failed", "ID 56 requires ID 55 = 02")
    if (fee_pct is not None) != (tip == "03"):
        raise QrValidationError("qr_validation_failed", "ID 57 requires ID 55 = 03")
    fee_fixed_minor = parse_amount_54(fee_fixed) if fee_fixed is not None else None
    if fee_pct is not None and (
        not _PCT_FEE_RE.match(fee_pct) or not "00.01" <= fee_pct <= "99.99"
    ):
        raise QrValidationError("qr_validation_failed", "ID 57 must be '00.01'..'99.99'")
    return tip, fee_fixed_minor, fee_pct


def _resolve_templates(
    fields: dict[int, str], config: BanglaQrConfig
) -> tuple[RouteClass, MerchantAccountInfo | None, tuple[int, ...]]:
    """spec/13 §C step 6 — find the Bangla QR GUID among 26-51 or fall back
    to card-scheme routing. GUID match is case-insensitive."""
    card_ids = tuple(i for i in _CARD_TEMPLATE_RANGE if i in fields)
    bangla: MerchantAccountInfo | None = None
    for root_id in _MERCHANT_TEMPLATE_RANGE:
        if root_id not in fields:
            continue
        try:
            subs = walk_template(fields[root_id], context=f"template {root_id:02d}")
        except QrValidationError:
            continue  # foreign acquirer content tolerated as opaque (spec/13 §A)
        guid = subs.get(0)
        if guid is None or guid.upper() != config.scheme_guid.upper():
            continue
        acquirer_id, merchant_id = subs.get(1), subs.get(2)
        if not acquirer_id or not merchant_id:
            raise QrValidationError(
                "qr_validation_failed",
                f"Bangla QR template {root_id:02d} lacks acquirer/merchant sub-IDs",
            )
        bangla = MerchantAccountInfo(
            root_id=root_id,
            guid=guid,
            acquirer_id=acquirer_id,
            merchant_id=merchant_id,
            psp_sub_id=subs.get(3),
        )
        break
    if bangla is not None:
        return RouteClass.BANGLA_QR, bangla, card_ids
    if card_ids:
        return RouteClass.CARD_SCHEME, None, card_ids
    raise QrValidationError("qr_no_routable_template", "no Bangla QR or card-scheme template")


def _parse_additional(fields: dict[int, str]) -> AdditionalData:
    if 62 not in fields:
        return AdditionalData()
    subs = walk_template(fields[62], context="template 62")
    return AdditionalData(
        bill_number=subs.get(1),
        mobile=subs.get(2),
        store_label=subs.get(3),
        reference_label=subs.get(5),
        terminal_label=subs.get(7),
        consumer_data_request=subs.get(9),
    )


def _parse_language(fields: dict[int, str]) -> LanguageTemplate | None:
    if 64 not in fields:
        return None
    subs = walk_template(fields[64], context="template 64")
    if 0 not in subs:
        raise QrValidationError("qr_validation_failed", "ID 64 lacks language preference sub 00")
    return LanguageTemplate(
        language_preference=subs[0],
        merchant_name=subs.get(1),
        merchant_city=subs.get(2),
    )


def parse_and_validate(payload: str, config: BanglaQrConfig) -> ParsedPayload:
    """spec/13 §C steps 1-6. Raises :class:`QrValidationError` with the
    specific failure code on any refusal (refusal-first, fail-closed)."""
    if not isinstance(payload, str) or not payload:
        raise QrValidationError("qr_malformed_tlv", "payload must be a non-empty string")

    objects = walk(payload.encode("utf-8"), reject_duplicates=True, context="root")
    for obj in objects:
        if obj.object_id in _RESERVED_RANGE:
            raise QrValidationError(
                "qr_reserved_id", f"reserved ID {obj.object_id:02d} present (65-79 RFU)"
            )
    _check_crc_object(payload, objects)
    fields = {obj.object_id: obj.value for obj in objects}

    if fields.get(0) != _PAYLOAD_FORMAT:
        raise QrValidationError("qr_unsupported_version", "ID 00 must be '01'")
    poi = fields.get(1)
    if poi not in {_POI_STATIC, _POI_DYNAMIC}:
        raise QrValidationError("qr_validation_failed", "ID 01 must be '11' or '12'")
    mcc = fields.get(52)
    if mcc is None or not _MCC_RE.match(mcc):
        raise QrValidationError("qr_validation_failed", "ID 52 must be a 4-digit MCC")
    currency = fields.get(53)
    if currency is None:
        raise QrValidationError("qr_validation_failed", "ID 53 missing")
    if currency != _BDT_NUMERIC:
        raise QrValidationError("qr_wrong_currency", "ID 53 must be '050' (BDT)")
    country = fields.get(58)
    if country is None:
        raise QrValidationError("qr_validation_failed", "ID 58 missing")
    if country != _COUNTRY_BD:
        raise QrValidationError("qr_wrong_country", "ID 58 must be 'BD'")
    merchant_name = fields.get(59)
    merchant_city = fields.get(60)
    if not merchant_name or not merchant_city:
        raise QrValidationError("qr_validation_failed", "IDs 59 and 60 are mandatory")

    if poi == _POI_STATIC:
        if 54 in fields:
            raise QrValidationError("qr_static_with_amount", "static QR must not carry ID 54")
        amount_minor: int | None = None
        qr_type = QrType.STATIC
    else:
        if 54 not in fields:
            raise QrValidationError("qr_validation_failed", "ID 54 mandatory for dynamic QR")
        amount_minor = parse_amount_54(fields[54])
        qr_type = QrType.DYNAMIC

    tip, fee_fixed_minor, fee_pct = _parse_tip(fields)
    route_class, bangla, card_ids = _resolve_templates(fields, config)

    return ParsedPayload(
        payload_string=payload,
        payload_hash=payload_hash_of(payload),
        qr_type=qr_type,
        route_class=route_class,
        mcc=mcc,
        merchant_name=merchant_name,
        merchant_city=merchant_city,
        country_code=country,
        currency=currency,
        amount_minor=amount_minor,
        tip_indicator=tip,
        convenience_fee_fixed_minor=fee_fixed_minor,
        convenience_fee_percentage=fee_pct,
        postal_code=fields.get(61),
        additional=_parse_additional(fields),
        language=_parse_language(fields),
        bangla_template=bangla,
        card_template_ids=card_ids,
    )

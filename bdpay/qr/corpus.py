"""Golden payload corpus — spec/13 §F (>=12 binding vectors, fixtures).

Vectors 1-5/14/15 are produced by our encoder (or a raw assembly that any
conformant encoder could emit); 6-13 are deliberately defective payloads that
the §C validator must refuse with the exact failure code. The corpus doubles
as ``qr_interop_test_cases`` seed rows (direction WE_SCAN, SIMULATOR class).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from bdpay.qr.codec import (
    LanguageTemplate,
    MerchantAccountInfo,
    QrEncodeRequest,
    QrType,
    encode_payload,
)
from bdpay.qr.config import MAX_PAYLOAD_BYTES, BanglaQrConfig
from bdpay.qr.crc import append_crc
from bdpay.qr.errors import QrEncodeError
from bdpay.qr.model import InteropDirection, QrInteropTestCase, qr_make_id
from bdpay.qr.tlv import assemble

__all__ = ["GoldenVector", "golden_corpus", "interop_cases"]

_MERCHANT_PAN = "MPANAAAA"
_FOREIGN_ACQUIRER = "FOREIGNBANKID"
_BENGALI_NAME = "করিম স্টোর"
_BENGALI_CITY = "ঢাকা"


@dataclass(frozen=True)
class GoldenVector:
    name: str
    payload: str
    expected: dict  # {"valid": bool, "failure_code": str|None, "fields": {...}}


def _account(config: BanglaQrConfig, *, acquirer_id: str | None = None) -> MerchantAccountInfo:
    return MerchantAccountInfo(
        root_id=config.template_root_id,
        guid=config.scheme_guid,
        acquirer_id=acquirer_id or config.acquirer_id,
        merchant_id=_MERCHANT_PAN,
        psp_sub_id=config.psp_sub_id,
    )


def _static_request(config: BanglaQrConfig, **overrides: object) -> QrEncodeRequest:
    base: dict = {
        "qr_type": QrType.STATIC,
        "merchant_account": _account(config),
        "mcc": "5411",
        "merchant_name": "Karim Store",
        "merchant_city": "Dhaka",
    }
    base.update(overrides)
    return QrEncodeRequest(**base)


def _raw_static_objects(config: BanglaQrConfig) -> list[tuple[int, str]]:
    """A valid minimal static object list for hand-built defective vectors."""
    template = assemble(
        [
            (0, config.scheme_guid),
            (1, config.acquirer_id),
            (2, _MERCHANT_PAN),
            (3, config.psp_sub_id),
        ]
    )
    return [
        (0, "01"),
        (1, "11"),
        (config.template_root_id, template),
        (52, "5411"),
        (53, "050"),
        (58, "BD"),
        (59, "Karim Store"),
        (60, "Dhaka"),
    ]


def _exact_budget_static(config: BanglaQrConfig) -> str:
    """Vector 5: exactly MAX_PAYLOAD_BYTES with a full (99-byte) ID 62.

    Every data object value is capped at 99 bytes by the two-digit length,
    so reaching the 512-byte total requires unreserved-template filler
    objects (IDs 80-99 — tolerated, content ignored, retained for CRC/hash
    per spec/13 §A). This is exactly what a busy bank-app payload looks like.
    """
    full_62 = assemble(
        [
            (1, "B" * 36),  # bill number
            (2, "M" * 10),  # mobile
            (3, "S" * 12),  # store label
            (5, "R" * 9),  # reference label
            (7, "T" * 6),  # terminal label
            (9, "C" * 2),  # consumer data request
        ]
    )
    if len(full_62.encode("utf-8")) != 99:
        raise QrEncodeError("vector 5 ID 62 must be exactly 99 bytes")
    base = [
        *_raw_static_objects(config),
        (61, "1212"),
        (62, full_62),
        (64, assemble([(0, "bn"), (1, _BENGALI_NAME), (2, _BENGALI_CITY)])),
    ]
    deficit = MAX_PAYLOAD_BYTES - len(
        append_crc(assemble(sorted(base, key=lambda pair: pair[0]))).encode("utf-8")
    )
    if deficit < 5:
        raise QrEncodeError("vector 5 base payload already too close to budget")
    filler_id = 80
    objects = list(base)
    while deficit > 0:
        take = min(deficit, 103)
        if deficit - take in (1, 2, 3, 4):
            take = deficit - 5  # leave room for one final minimal object
        objects.append((filler_id, "F" * (take - 4)))
        filler_id += 1
        deficit -= take
    payload = append_crc(assemble(sorted(objects, key=lambda pair: pair[0])))
    if len(payload.encode("utf-8")) != MAX_PAYLOAD_BYTES:
        raise QrEncodeError("vector 5 construction did not land on the budget")
    return payload


def _tamper_crc(payload: str) -> str:
    last = payload[-1]
    return payload[:-1] + ("0" if last != "0" else "1")


def golden_corpus(config: BanglaQrConfig) -> tuple[GoldenVector, ...]:
    """The 15 binding vectors of spec/13 §F, in spec order."""
    minimal_static = encode_payload(_static_request(config))
    dynamic_fraction = encode_payload(
        _static_request(config, qr_type=QrType.DYNAMIC, amount_minor=125_050)
    )
    dynamic_whole = encode_payload(
        _static_request(config, qr_type=QrType.DYNAMIC, amount_minor=125_000)
    )
    bengali_static = encode_payload(
        _static_request(
            config,
            language=LanguageTemplate(merchant_name=_BENGALI_NAME, merchant_city=_BENGALI_CITY),
        )
    )
    raw = _raw_static_objects(config)

    def with_object(extra: tuple[int, str]) -> str:
        objects = sorted([*raw, extra], key=lambda pair: pair[0])
        return append_crc(assemble(objects))

    def replacing(object_id: int, value: str) -> str:
        objects = [(i, value if i == object_id else v) for i, v in raw]
        return append_crc(assemble(objects))

    dynamic_raw = [(i, "12" if i == 1 else v) for i, v in raw]
    dynamic_no_amount = append_crc(assemble(dynamic_raw))
    dynamic_bengali_amount = append_crc(
        assemble(sorted([*dynamic_raw, (54, "১২৫০")], key=lambda pair: pair[0]))
    )
    duplicate_59 = append_crc(assemble([*raw, (59, "Karim Store")]))
    foreign = encode_payload(
        QrEncodeRequest(
            qr_type=QrType.STATIC,
            merchant_account=_account(config, acquirer_id=_FOREIGN_ACQUIRER),
            mcc="5499",
            merchant_name="Rahim Mart",
            merchant_city="Chattogram",
        )
    )
    visa_only_objects = [
        (i, v) for i, v in raw if i != config.template_root_id
    ]
    visa_only = append_crc(
        assemble(sorted([*visa_only_objects, (2, assemble([(0, "VISACOGUID")]))]))
    )

    def valid(fields: dict) -> dict:
        return {"valid": True, "failure_code": None, "fields": fields}

    def invalid(code: str) -> dict:
        return {"valid": False, "failure_code": code, "fields": {}}

    return (
        GoldenVector("01_minimal_valid_static", minimal_static, valid({"qr_type": "STATIC"})),
        GoldenVector(
            "02_dynamic_fraction_amount",
            dynamic_fraction,
            valid({"qr_type": "DYNAMIC", "amount_minor": 125_050}),
        ),
        GoldenVector(
            "03_dynamic_whole_amount",
            dynamic_whole,
            valid({"qr_type": "DYNAMIC", "amount_minor": 125_000}),
        ),
        GoldenVector(
            "04_bengali_name_id64",
            bengali_static,
            valid({"qr_type": "STATIC"}),
        ),
        GoldenVector(
            "05_max_length_full_id62",
            _exact_budget_static(config),
            valid({"qr_type": "STATIC"}),
        ),
        GoldenVector(
            "06_tampered_crc", _tamper_crc(minimal_static), invalid("qr_invalid_crc")
        ),
        GoldenVector("07_wrong_currency_usd", replacing(53, "840"), invalid("qr_wrong_currency")),
        GoldenVector("08_wrong_country_in", replacing(58, "IN"), invalid("qr_wrong_country")),
        GoldenVector(
            "09_static_with_amount",
            with_object((54, "100.00")),
            invalid("qr_static_with_amount"),
        ),
        GoldenVector(
            "10_dynamic_without_amount", dynamic_no_amount, invalid("qr_validation_failed")
        ),
        GoldenVector("11_duplicate_id59", duplicate_59, invalid("qr_duplicate_id")),
        GoldenVector(
            "12_bengali_digits_in_amount",
            dynamic_bengali_amount,
            invalid("qr_bengali_digits_in_amount"),
        ),
        GoldenVector("13_reserved_id70", with_object((70, "RFU")), invalid("qr_reserved_id")),
        GoldenVector(
            "14_foreign_acquirer_offus",
            foreign,
            valid({"qr_type": "STATIC", "route_class": "BANGLA_QR"}),
        ),
        GoldenVector(
            "15_visa_only_template",
            visa_only,
            valid({"qr_type": "STATIC", "route_class": "CARD_SCHEME"}),
        ),
    )


def interop_cases(config: BanglaQrConfig, created_at: datetime) -> list[QrInteropTestCase]:
    """The corpus as qr_interop_test_cases seed rows (spec/13 §F)."""
    return [
        QrInteropTestCase(
            test_case_id=qr_make_id("qtest", {"name": vector.name, "version": 1}),
            name=vector.name,
            version=1,
            direction=InteropDirection.WE_SCAN,
            payload_string=vector.payload,
            expected=vector.expected,
            counterparty_class="SIMULATOR",
            last_result="UNRUN",
            last_run_at=None,
            created_at=created_at,
        )
        for vector in golden_corpus(config)
    ]

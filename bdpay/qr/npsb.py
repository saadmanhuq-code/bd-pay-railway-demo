"""NPSB QR ISO 8583 dictionary rows — spec/13 §D (content owned here).

The qr package implements no connector; it supplies these DE-content rows to
spec/11's ``npsb_iso8583_v28`` ``rail_field_dictionaries`` document. The exact
DE3 sub-code arrives from sponsor-bank UAT (spec/13 Open question 2) and is
therefore a parameter with the merchant-payment family default.
"""

from __future__ import annotations

from bdpay.qr.codec import MerchantAccountInfo
from bdpay.qr.errors import QrEncodeError

__all__ = ["NPSB_QR_MTI", "npsb_qr_dictionary_rows"]

NPSB_QR_MTI = "0200"

#: BB v2.8 merchant-payment family default; final sub-code is dictionary data.
_DEFAULT_PROCESSING_CODE = "260000"

_AMOUNT_DIGITS = 12  # DE4 is n12


def npsb_qr_dictionary_rows(
    *,
    amount_minor: int,
    payload_hash: str,
    template: MerchantAccountInfo,
    sender_ref: str,
    processing_code: str = _DEFAULT_PROCESSING_CODE,
) -> dict[str, str]:
    """The QR-transaction DE rows for the NPSB dictionary (spec/13 §D table).

    All values are strings (dictionary data); amounts are zero-padded n12
    paisa — integer in, exact out, no floats anywhere.
    """
    if isinstance(amount_minor, bool) or not isinstance(amount_minor, int) or amount_minor <= 0:
        raise QrEncodeError("amount_minor must be a positive int (paisa)")
    if len(processing_code) != 6 or not processing_code.isascii() or not processing_code.isdigit():
        raise QrEncodeError(f"processing code must be 6 ASCII digits, got {processing_code!r}")
    if not processing_code.startswith("26"):
        raise QrEncodeError("QR transactions use the 26 merchant-payment family (BB v2.8)")
    amount = str(amount_minor)
    if len(amount) > _AMOUNT_DIGITS:
        raise QrEncodeError(f"amount exceeds DE4 n{_AMOUNT_DIGITS}")
    template_echo = "|".join(
        part for part in (template.acquirer_id, template.merchant_id, template.psp_sub_id) if part
    )
    return {
        "mti": NPSB_QR_MTI,
        "de3": processing_code,
        "de4": amount.zfill(_AMOUNT_DIGITS),
        "de48.10": payload_hash,
        "de48.11": template_echo,
        "de49": "050",
        "de102": sender_ref,
        "de103": template.merchant_id,
    }

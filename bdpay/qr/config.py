"""Bangla QR profile configuration and binding limits — spec/13.

The BB merchant-account template root ID and scheme GUID arrive with acquirer
onboarding (spec/13 Open question 1); both are config, never code. Defaults
are the soft-launch interim values; issuer scan-to-pay and on-us acquiring do
not depend on the final BB assignment.
"""

from __future__ import annotations

from dataclasses import dataclass

from bdpay.qr.errors import QrEncodeError

__all__ = [
    "DYNAMIC_TTL_MAX_SECONDS",
    "DYNAMIC_TTL_MIN_SECONDS",
    "EXPIRY_SWEEP_SECONDS",
    "MAX_MERCHANT_CITY_CHARS",
    "MAX_MERCHANT_NAME_CHARS",
    "MAX_PAYLOAD_BYTES",
    "MAX_POSTAL_CODE_CHARS",
    "MAX_STATIC_QRS_PER_MERCHANT",
    "RESOLUTION_TTL_SECONDS",
    "STATIC_LIMIT_MINOR",
    "BanglaQrConfig",
]

#: Static QR per-transaction cap: BDT 20,200 = 2,020,000 paisa (spec/13 §C).
STATIC_LIMIT_MINOR = 2_020_000

#: Total payload budget, bytes (spec/13 §A length budget rule).
MAX_PAYLOAD_BYTES = 512

#: EMVCo primitive length rules (spec/13 §A IDs 59/60/61).
MAX_MERCHANT_NAME_CHARS = 25
MAX_MERCHANT_CITY_CHARS = 15
MAX_POSTAL_CODE_CHARS = 10

#: Dynamic QR TTL window, seconds (spec/13 POST /v1/qr-codes/dynamic).
DYNAMIC_TTL_MIN_SECONDS = 60
DYNAMIC_TTL_MAX_SECONDS = 900

#: Scan-resolution validity window enforced at /pay (spec/13 FSM §3).
RESOLUTION_TTL_SECONDS = 300

#: Dynamic-payload TTL sweep cadence (spec/13 FSM §2).
EXPIRY_SWEEP_SECONDS = 30

#: Default static-QR count limit per merchant (spec/13 issue-static errors).
MAX_STATIC_QRS_PER_MERCHANT = 50


@dataclass(frozen=True)
class BanglaQrConfig:
    """Acquirer-side scheme identity (spec/13 §A merchant account template).

    ``template_root_id`` must sit in the EMVCo merchant-account range 26-51;
    ``scheme_guid`` is shared scheme-wide (it identifies Bangla QR/NPSB, not
    us — on-us is decided by ``acquirer_id`` + merchant lookup, spec/13 §C
    step 7). GUID comparison is case-insensitive on parse.
    """

    acquirer_id: str
    psp_sub_id: str
    template_root_id: int = 26
    scheme_guid: str = "BD.BANGLAQR.NPSB"

    def __post_init__(self) -> None:
        if not 26 <= self.template_root_id <= 51:
            raise QrEncodeError(
                f"template_root_id must be in 26-51, got {self.template_root_id}"
            )
        if not self.scheme_guid or not self.acquirer_id or not self.psp_sub_id:
            raise QrEncodeError("scheme_guid, acquirer_id and psp_sub_id are required")

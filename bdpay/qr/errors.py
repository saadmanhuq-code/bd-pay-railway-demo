"""QR error taxonomy (spec/13 §API errors, §C parse/validate algorithm).

Every refusal carries a specific ``qr_*`` failure code. Parse/validate
failures are ``invalid_request`` (400) per spec/13; the static cap is
``limit_exceeded`` (422); lifecycle refusals (suspended/revoked/expired/
already-paid) are ``conflict`` (409). Codes are recorded verbatim in
``qr_scan_resolutions.failure_code`` — fraud telemetry depends on them.
"""

from __future__ import annotations

from bdpay.platform.errors import InvalidRequestError

__all__ = ["QR_FAILURE_CODES", "QrEncodeError", "QrValidationError"]

#: The closed set of issuer-side validation failure codes (spec/13 §C + FSM
#: refusals). ``qr_cancelled`` is additive: spec/13 names no code for resolving
#: a CANCELLED dynamic payload; see lane-B errata.
QR_FAILURE_CODES: frozenset[str] = frozenset(
    {
        "qr_malformed_tlv",
        "qr_duplicate_id",
        "qr_reserved_id",
        "qr_invalid_crc",
        "qr_unsupported_version",
        "qr_validation_failed",
        "qr_wrong_currency",
        "qr_wrong_country",
        "qr_static_with_amount",
        "qr_bengali_digits_in_amount",
        "qr_no_routable_template",
        "qr_merchant_suspended",
        "qr_revoked",
        "qr_expired",
        "qr_already_paid",
        "qr_cancelled",
    }
)


class QrValidationError(InvalidRequestError):
    """A scanned payload failed parse/validation (spec/13 §C) — 400.

    ``code`` is always one of :data:`QR_FAILURE_CODES`; ``detail`` carries the
    non-binding human diagnostic (never PII — payload fragments are excluded).
    """

    def __init__(self, code: str, detail: str) -> None:
        if code not in QR_FAILURE_CODES:
            raise ValueError(f"unknown qr failure code {code!r}")
        super().__init__(detail, code=code)
        self.detail = detail


class QrEncodeError(ValueError):
    """Raised when an encode request cannot produce a valid payload.

    Encode failures are programming/configuration faults on the acquirer
    side, never wire input — hence ValueError, not a BDPayError envelope.
    """

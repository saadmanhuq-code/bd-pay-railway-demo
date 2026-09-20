"""Tag-54 amount boundary — spec/13 §A ID 54 (the ONE paisa<->string boundary).

Render: integer paisa -> ASCII decimal with trailing-zero canonicalization
(whole amounts render with no fraction: ``"1250"``, not ``"1250.00"``).
Parse: ASCII-only per EMVCo; Bengali digits in the wire field are REJECTED
(``qr_bengali_digits_in_amount`` — normalization is for display inputs, not
payloads); more than 2 fraction digits rejected. No floats anywhere.
"""

from __future__ import annotations

import re

from bdpay.qr.errors import QrEncodeError, QrValidationError

__all__ = ["parse_amount_54", "render_amount_54"]

#: EMVCo ID 54 maximum length (spec/13 §A: "ASCII decimal, <=13 chars").
_MAX_AMOUNT_CHARS = 13

_AMOUNT_RE = re.compile(r"^(\d+)(?:\.(\d{1,2}))?$", re.ASCII)
_BENGALI_DIGIT_RE = re.compile(r"[০-৯]")


def render_amount_54(amount_minor: int) -> str:
    """Exact integer-paisa -> tag 54 string (spec/13 §A boundary rule)."""
    if isinstance(amount_minor, bool) or not isinstance(amount_minor, int):
        raise QrEncodeError(
            f"amount_minor must be int paisa, got {type(amount_minor).__name__} "
            "(floats are rejected everywhere)"
        )
    if amount_minor <= 0:
        raise QrEncodeError(f"dynamic amount must be positive paisa, got {amount_minor}")
    whole, frac = divmod(amount_minor, 100)
    rendered = str(whole) if frac == 0 else f"{whole}.{frac:02d}"
    if len(rendered) > _MAX_AMOUNT_CHARS:
        raise QrEncodeError(f"amount renders to {len(rendered)} chars; ID 54 max is 13")
    return rendered


def parse_amount_54(text: str) -> int:
    """Exact tag 54 string -> integer paisa (spec/13 §A parse rule)."""
    if _BENGALI_DIGIT_RE.search(text):
        raise QrValidationError(
            "qr_bengali_digits_in_amount",
            "ID 54 contains Bengali digits; EMVCo wire amounts are ASCII-only",
        )
    if len(text) > _MAX_AMOUNT_CHARS:
        raise QrValidationError(
            "qr_validation_failed", f"ID 54 is {len(text)} chars; max is 13"
        )
    match = _AMOUNT_RE.match(text)
    if match is None:
        raise QrValidationError(
            "qr_validation_failed",
            "ID 54 is not a valid ASCII decimal amount (max 2 fraction digits)",
        )
    whole, frac = match.group(1), match.group(2)
    amount_minor = int(whole) * 100 + (int(frac.ljust(2, "0")) if frac else 0)
    if amount_minor <= 0:
        raise QrValidationError("qr_validation_failed", "ID 54 amount must be positive")
    return amount_minor

"""The single sanctioned integer-paisa -> decimal-string boundary (spec/12 §A.1).

bKash/Nagad/aggregator wire formats carry BDT amounts as two-decimal-place
strings. Conversion happens HERE and only here for the MFS adapters (same
algorithm as spec/11 §C.1; no float anywhere — CERT-09). The reverse direction
(wire decimal string -> paisa) is exact or rejected: sub-paisa precision on an
inbound amount is a wire defect, never rounded.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from bdpay.platform.bangla import normalize_bengali_digits
from bdpay.platform.money import Money, MoneyError

__all__ = ["AmountBoundaryError", "decimal_string_to_paisa", "paisa_to_decimal_string"]


class AmountBoundaryError(ValueError):
    """Raised when a wire amount cannot cross the decimal boundary exactly."""


def paisa_to_decimal_string(amount_minor: int) -> str:
    """``10100`` paisa -> ``"101.00"`` — Decimal all the way, never float."""
    if isinstance(amount_minor, bool) or not isinstance(amount_minor, int):
        raise AmountBoundaryError(
            f"amount_minor must be int paisa, got {type(amount_minor).__name__}"
        )
    if amount_minor < 0:
        raise AmountBoundaryError("negative amounts never cross the MFS wire boundary")
    quantized = (Decimal(amount_minor) / Decimal(100)).quantize(Decimal("0.01"))
    return f"{quantized:f}"


def decimal_string_to_paisa(text: str) -> Money:
    """Exact inbound conversion; Bengali digits normalized first (spec/12 rule)."""
    if not isinstance(text, str):
        raise AmountBoundaryError(f"wire amount must be str, got {type(text).__name__}")
    cleaned = normalize_bengali_digits(text).strip()
    try:
        value = Decimal(cleaned)
    except InvalidOperation as exc:
        raise AmountBoundaryError(f"wire amount is not a decimal: {cleaned!r}") from exc
    try:
        return Money.from_decimal_bdt(value)
    except MoneyError as exc:
        raise AmountBoundaryError(str(exc)) from exc

"""Bengali numeric normalization and BDT amount parsing (spec 00 §7).

Bengali-locale numeric inputs are normalized to ASCII before any time or
amount parsing. ``parse_bdt_amount`` accepts the common Bengali/BD formats —
Bengali digits, lakh-style comma grouping, the Taka sign — and returns an
exact :class:`~bdpay.platform.money.Money` (sub-paisa precision rejected).
"""

from __future__ import annotations

import re
from decimal import Decimal

from bdpay.platform.money import Money

__all__ = ["AmountParseError", "normalize_bengali_digits", "parse_bdt_amount"]

#: Bengali digits U+09E6..U+09EF mapped to ASCII 0-9.
_BENGALI_TO_ASCII = str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789")

#: Currency markers stripped before numeric parsing: the Taka sign (U+09F3),
#: the Bengali word, and Latin "BDT"/"Tk"/"Taka" tokens.
_CURRENCY_TOKEN_RE = re.compile(r"৳|টাকা|(?i:\b(?:bdt|taka|tk)\b\.?)")

#: Grouping separators dropped before parsing: ASCII comma (lakh-style
#: grouping like 20,300 or 2,03,000) and the Arabic thousands separator
#: occasionally produced by BD keyboards.
_GROUPING_RE = re.compile(r"[,٬]")

_AMOUNT_RE = re.compile(r"\d+(?:\.\d+)?")


class AmountParseError(ValueError):
    """Raised when a text cannot be parsed as a BDT amount."""


def normalize_bengali_digits(text: str) -> str:
    """Map Bengali digits ০১২৩৪৫৬৭৮৯ to ASCII 0123456789, all else unchanged."""
    if not isinstance(text, str):
        raise TypeError(f"text must be str, got {type(text).__name__}")
    return text.translate(_BENGALI_TO_ASCII)


def parse_bdt_amount(text: str) -> Money:
    """Parse a human-entered BDT amount into exact integer-paisa Money.

    Handles ``"২০,৩০০.৫০"``, ``"20300.50"``, ``"৳20,300.50"``, ``"BDT 500"``.
    Raises :class:`AmountParseError` on anything that is not one non-negative
    amount; sub-paisa precision raises ``MoneyError`` via ``from_decimal_bdt``.
    """
    if not isinstance(text, str):
        raise AmountParseError(f"amount text must be str, got {type(text).__name__}")
    cleaned = normalize_bengali_digits(text)
    cleaned = _CURRENCY_TOKEN_RE.sub(" ", cleaned)
    cleaned = _GROUPING_RE.sub("", cleaned).strip()
    if not _AMOUNT_RE.fullmatch(cleaned):
        raise AmountParseError("text is not a single non-negative BDT amount")
    return Money.from_decimal_bdt(Decimal(cleaned))

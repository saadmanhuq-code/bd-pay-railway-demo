"""Card-number validation, scheme detection, and PCI truncation (spec/14 Plane A).

Bengali digits are normalized to ASCII before any validation (spec/14 intake
step 2 — ``BangladeshNumericNormalization`` lift). No PAN value is ever
constructed here as a literal: every function operates on caller-supplied
input only (scripts/check_bans.py keeps card-number-like literals out of
``bdpay/`` source).
"""

from __future__ import annotations

import unicodedata
from datetime import datetime

from bdpay.platform.bangla import normalize_bengali_digits

__all__ = [
    "detect_scheme",
    "expiry_in_past",
    "luhn_valid",
    "normalize_digits",
    "normalize_holder_name",
    "truncate_pan",
]

#: Longest-prefix scheme ranges (spec/14 intake response notes).
#: 60/65/81 ranges stay UNKNOWN pending the co-badged/domestic BIN table
#: (spec/14 open question 7) — the acquirer decides.
_RANGES: tuple[tuple[int, int, int, str], ...] = (
    # (prefix_len, low, high, scheme) — evaluated longest prefix first
    (4, 2221, 2720, "MASTERCARD"),
    (2, 51, 55, "MASTERCARD"),
    (2, 34, 34, "AMEX"),
    (2, 37, 37, "AMEX"),
    (2, 35, 35, "JCB"),
    (2, 62, 62, "UNIONPAY"),
    (1, 4, 4, "VISA"),
)


def normalize_digits(value: str) -> str:
    """Bengali->ASCII digit normalization with surrounding whitespace stripped."""
    return normalize_bengali_digits(value).strip()


def normalize_holder_name(value: str) -> str:
    """NFKC-normalize and strip control characters (Bengali script permitted)."""
    normalized = unicodedata.normalize("NFKC", value)
    return "".join(ch for ch in normalized if unicodedata.category(ch)[0] != "C").strip()


def luhn_valid(pan: str) -> bool:
    """Standard Luhn mod-10 check over an ASCII digit string."""
    if not pan.isdigit() or len(pan) < 2:
        return False
    total = 0
    for index, char in enumerate(reversed(pan)):
        digit = ord(char) - 48
        if index % 2 == 1:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


def detect_scheme(pan: str) -> str:
    """Longest-prefix match over the binding range table; unknown -> UNKNOWN."""
    for prefix_len, low, high, scheme in _RANGES:
        if len(pan) >= prefix_len:
            head = int(pan[:prefix_len])
            if low <= head <= high:
                return scheme
    return "UNKNOWN"


def truncate_pan(pan: str) -> tuple[str, str]:
    """PCI Req 3.4.1 truncation: (bin, last4).

    BIN(8)+last4 is acceptable only for PANs of 15+ digits (PCI SSC
    truncation FAQ, restated in spec/14 §Out-of-CDE); shorter PANs keep
    first 6 + last 4. Never any middle digit anywhere.
    """
    bin_len = 8 if len(pan) >= 15 else 6
    return pan[:bin_len], pan[-4:]


def expiry_in_past(month: str, year: str, now: datetime) -> bool:
    """True when the (MM, YYYY) expiry is before the current UTC month —
    a card is valid through the last day of its expiry month."""
    return (int(year), int(month)) < (now.year, now.month)

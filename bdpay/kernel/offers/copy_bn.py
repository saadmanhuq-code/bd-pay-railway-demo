"""Bilingual ReasonCode copy table (spec/18 §Eligibility — consumer-displayable).

Each :class:`~bdpay.kernel.offers.eligibility.ReasonCode` maps to a static
``(message, message_bn)`` pair. The Bengali strings are the authoritative
seed pinned by ``tests/platform/test_bn_encoding.py`` (G5 follow-up action 1,
2026-06-13) — byte-identical here so the mojibake harness pins this table
transitively. All copy is PII-free by construction (static strings only).
"""

from __future__ import annotations

from types import MappingProxyType

__all__ = ["REASON_COPY", "reason_messages"]

#: ReasonCode value -> (message, message_bn). Keys are the enum *values*
#: (UPPER_SNAKE strings) so this table has no import-time dependency cycle
#: with :mod:`bdpay.kernel.offers.eligibility`.
REASON_COPY: MappingProxyType[str, tuple[str, str]] = MappingProxyType(
    {
        "OFFER_NOT_ACTIVE": (
            "This offer is not currently active",
            "অফারটি এখন সক্রিয় নেই",
        ),
        "OUTSIDE_WINDOW": (
            "This offer does not apply at this time",
            "এই সময়ে অফারটি প্রযোজ্য নয়",
        ),
        "BEFORE_VALIDITY": (
            "This offer has not started yet",
            "অফারের মেয়াদ এখনও শুরু হয়নি",
        ),
        "AFTER_VALIDITY": (
            "This offer has expired",
            "অফারের মেয়াদ শেষ হয়ে গেছে",
        ),
        "MIN_SPEND_NOT_MET": (
            "The minimum spend for this offer was not met",
            "ন্যূনতম কেনাকাটার পরিমাণ পূরণ হয়নি",
        ),
        "METHOD_NOT_ALLOWED": (
            "This offer does not apply to this payment method",
            "এই পেমেন্ট পদ্ধতিতে অফারটি প্রযোজ্য নয়",
        ),
        "CAP_DAY_EXHAUSTED": (
            "Today's limit for this offer has been reached",
            "আজকের জন্য অফারের সীমা শেষ হয়ে গেছে",
        ),
        "CAP_TOTAL_EXHAUSTED": (
            "The total limit for this offer has been reached",
            "অফারের মোট সীমা শেষ হয়ে গেছে",
        ),
        "CAP_CUSTOMER_EXHAUSTED": (
            "You have already used this offer",
            "আপনি এই অফারটি ইতিমধ্যে ব্যবহার করেছেন",
        ),
        "MERCHANT_NOT_ACTIVE": (
            "The merchant is not currently active",
            "মার্চেন্ট এই মুহূর্তে সক্রিয় নেই",
        ),
        "BOGO_LINE_ITEMS_MISSING": (
            "BOGO offers require item details",
            "BOGO অফারের জন্য পণ্যের বিবরণ প্রয়োজন",
        ),
    }
)


def reason_messages(reason_value: str) -> tuple[str, str]:
    """Return ``(message, message_bn)`` for a ReasonCode value; KeyError if unknown."""
    return REASON_COPY[reason_value]

"""Grapheme-boundary truncation for Bengali text — spec/13 §A ID 59/64 rule.

"Truncation on UTF-8 grapheme boundary, never mid-cluster." Bengali clusters
bind via combining vowel signs (Mn/Mc/Me), the virama/hasanta (U+09CD, which
joins the following consonant into the cluster), and ZWJ/ZWNJ. A cut is legal
only at a position where the next character starts a new cluster.
"""

from __future__ import annotations

import unicodedata

__all__ = ["truncate_graphemes"]

_VIRAMA = "্"
_JOINERS = {"‌", "‍"}  # ZWNJ, ZWJ
_COMBINING_CATEGORIES = {"Mn", "Mc", "Me"}


def _starts_new_cluster(text: str, index: int) -> bool:
    """True if a cut immediately before ``text[index]`` is cluster-safe."""
    char = text[index]
    if unicodedata.category(char) in _COMBINING_CATEGORIES:
        return False
    if char in _JOINERS:
        return False
    previous = text[index - 1]
    # The virama joins the NEXT consonant into the current cluster; a joiner
    # before this char likewise glues it backwards.
    if previous == _VIRAMA or previous in _JOINERS:
        return False
    return True


def truncate_graphemes(text: str, max_units: int, *, byte_budget: bool = False) -> str:
    """Truncate ``text`` to at most ``max_units`` (chars, or UTF-8 bytes when
    ``byte_budget``), cutting only on a grapheme-cluster boundary."""
    if max_units <= 0:
        return ""

    def measure(candidate: str) -> int:
        return len(candidate.encode("utf-8")) if byte_budget else len(candidate)

    if measure(text) <= max_units:
        return text
    cut = len(text)
    while cut > 0:
        if (cut == len(text) or _starts_new_cluster(text, cut)) and measure(text[:cut]) <= (
            max_units
        ):
            return text[:cut].rstrip()
        cut -= 1
    return ""

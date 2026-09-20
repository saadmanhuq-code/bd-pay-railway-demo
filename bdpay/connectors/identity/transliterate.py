"""Bangla (Bengali script) -> Latin romanization for sanctions name matching.

Ported from dataroom-bd/src/lib/compliance/sanctions/transliterate.ts
(PORT TS->PY per PORTING-MAP; behavior preserved, test vectors carried across).

PURPOSE & HONEST SCOPE: a PRAGMATIC romanizer for fuzzy name matching, NOT a
linguistically complete or reversible transliteration system. The goal is
narrow: take a Bengali-script personal/entity name and produce a Latin-script
approximation close enough that the Jaro-Winkler / token-sort matcher can
align it against a Latin-script watchlist entry — so designated names written
in Bangla script surface their watchlist hit instead of silently clearing.

KEY INVARIANT (preserved from the source): for input containing NO Bengali
codepoints, this function is the IDENTITY — it returns the input unchanged.

WORD-INITIAL VOWEL GLIDE (COMP-04 parity): a word-initial independent ``ই``/
``ঈ`` (I/II) immediately followed by another independent vowel romanizes to
``y`` (the dominant BD romanization of the ``i+V`` onset glide), so
``ইউনুস`` -> ``yunus`` aligns with the Latin ``Yunus`` instead of false-clearing
as the distinct token ``iunus``. This is intentionally a byte-for-byte
duplicate of the rule in ``bdpay/compliance/sanctions/translit.py`` — the
connector layer is forbidden from importing ``bdpay.compliance`` (CERT-03
adapter-isolation check), so the fold/glide tables are mirrored here so the
connector's un-injected ``screen()`` fallback has NO unfixed (false-clearing)
path. Production injects the spec/07 screener (LB7), which owns matching.
"""

from __future__ import annotations

import re

__all__ = ["has_bengali", "transliterate_bangla_to_latin"]

#: Bengali Unicode block: U+0980-U+09FF.
_BENGALI_RANGE = re.compile("[ঀ-৿]")


def has_bengali(text: str) -> bool:
    """True if the string contains at least one Bengali-script codepoint."""
    return _BENGALI_RANGE.search(text) is not None


# Independent vowels (carry their own sound).
_INDEPENDENT_VOWELS = {
    "অ": "a",
    "আ": "a",  # long aa -> 'a' for matching simplicity
    "ই": "i",
    "ঈ": "i",
    "উ": "u",
    "ঊ": "u",
    "ঋ": "ri",
    "এ": "e",
    "ঐ": "oi",
    "ও": "o",
    "ঔ": "ou",
}

# Dependent vowel signs (matra) — modify the preceding consonant's inherent 'a'.
_VOWEL_SIGNS = {
    "া": "a",
    "ি": "i",
    "ী": "i",
    "ু": "u",
    "ূ": "u",
    "ৃ": "ri",
    "ে": "e",
    "ৈ": "oi",
    "ো": "o",
    "ৌ": "ou",
}

# Consonants; each carries an inherent 'a' unless overridden/suppressed.
_CONSONANTS = {
    "ক": "k",
    "খ": "kh",
    "গ": "g",
    "ঘ": "gh",
    "ঙ": "ng",
    "চ": "ch",
    "ছ": "chh",
    "জ": "j",
    "ঝ": "jh",
    "ঞ": "n",
    "ট": "t",
    "ঠ": "th",
    "ড": "d",
    "ঢ": "dh",
    "ণ": "n",
    "ত": "t",
    "থ": "th",
    "দ": "d",
    "ধ": "dh",
    "ন": "n",
    "প": "p",
    "ফ": "f",  # commonly romanized f or ph
    "ব": "b",
    "ভ": "bh",
    "ম": "m",
    "য": "y",
    "র": "r",
    "ল": "l",
    "শ": "sh",
    "ষ": "sh",
    "স": "s",
    "হ": "h",
    "ড়": "r",
    "ঢ়": "rh",
    "য়": "y",
    "ৎ": "t",  # khanda ta
}

# Word-initial ``ই``/``ঈ`` before another independent vowel glides to ``y``
# (i+V onset, e.g. ``ইউনুস`` -> ``yunus``) instead of ``i`` (COMP-04 parity).
_GLIDE_I_VOWELS = frozenset({"ই", "ঈ"})

_HASANTA = "্"  # virama — suppresses the inherent vowel
_ANUSVARA = "ং"  # -> "ng"
_CHANDRABINDU = "ঁ"  # nasalization -> dropped for matching
_VISARGA = "ঃ"  # -> "h"
_NUKTA = "়"  # combining dot below (U+09BC) -> dropped for matching

_DIGITS = {
    "০": "0",
    "১": "1",
    "২": "2",
    "৩": "3",
    "৪": "4",
    "৫": "5",
    "৬": "6",
    "৭": "7",
    "৮": "8",
    "৯": "9",
}

_WHITESPACE = re.compile(r"\s")


def transliterate_bangla_to_latin(text: str) -> str:
    """Romanize Bengali-script runs to Latin (identity on non-Bengali input).

    Consonants emit their base sound and tentatively carry an inherent 'a';
    a following vowel sign overrides it, a hasanta suppresses it (consonant
    cluster), and a word-final consonant drops it (Bengali schwa deletion —
    the source's "লাদেন" -> "laden", not "ladena").
    """
    if not has_bengali(text):
        return text

    chars = list(text)
    out: list[str] = []
    pending_inherent_a = False

    def flush_inherent_a() -> None:
        nonlocal pending_inherent_a
        if pending_inherent_a:
            out.append("a")
            pending_inherent_a = False

    def at_word_end(next_ch: str | None) -> bool:
        return next_ch is None or _WHITESPACE.match(next_ch) is not None

    def at_word_start(prev_ch: str | None) -> bool:
        return prev_ch is None or _WHITESPACE.match(prev_ch) is not None

    for index, ch in enumerate(chars):
        next_ch = chars[index + 1] if index + 1 < len(chars) else None
        prev_ch = chars[index - 1] if index > 0 else None
        if ch in _CONSONANTS:
            flush_inherent_a()
            out.append(_CONSONANTS[ch])
            pending_inherent_a = not at_word_end(next_ch)
            continue
        if ch in _VOWEL_SIGNS:
            pending_inherent_a = False
            out.append(_VOWEL_SIGNS[ch])
            continue
        if ch == _HASANTA:
            pending_inherent_a = False
            continue
        if ch == _NUKTA:
            continue
        if ch in _INDEPENDENT_VOWELS:
            flush_inherent_a()
            # Word-initial ই/ঈ before another independent vowel glides to 'y'
            # (i+V onset: ইউনুস -> yunus); does not fire before a consonant
            # (উসামা stays usama) or word-finally.
            if (
                ch in _GLIDE_I_VOWELS
                and at_word_start(prev_ch)
                and next_ch is not None
                and next_ch in _INDEPENDENT_VOWELS
            ):
                out.append("y")
            else:
                out.append(_INDEPENDENT_VOWELS[ch])
            continue
        if ch == _ANUSVARA:
            flush_inherent_a()
            out.append("ng")
            continue
        if ch == _VISARGA:
            flush_inherent_a()
            out.append("h")
            continue
        if ch == _CHANDRABINDU:
            continue
        if ch in _DIGITS:
            flush_inherent_a()
            out.append(_DIGITS[ch])
            continue
        flush_inherent_a()
        out.append(ch)

    flush_inherent_a()
    return "".join(out)

"""Bangla (Bengali script) -> Latin romanization for sanctions name matching.

Ported from ``dataroom-bd/src/lib/compliance/sanctions/transliterate.ts``
(PORT TS->PY, PORTING-MAP.md). Behaviour is faithful to the verified source:

- PRAGMATIC romanizer for fuzzy name matching, not a reversible
  transliteration system. It favours the most common Latin spellings of
  Bengali phonemes so designated names written in Bangla script surface their
  watchlist hit instead of silently clearing.
- KEY INVARIANT: for input containing NO Bengali codepoints the function is
  the IDENTITY — non-Bengali text passes through byte-for-byte unchanged.
- Inherent-vowel rule: a consonant tentatively carries an inherent ``a``; a
  following vowel sign overrides it, a hasanta suppresses it (conjunct), and
  a word-final consonant drops it (schwa deletion).
- Word-initial independent-vowel glide: a word-initial independent ``ই``/``ঈ``
  (I/II) immediately followed by another independent vowel romanizes to ``y``
  (the dominant BD romanization of the ``i+V`` onset glide), so
  ``ইউনুস`` -> ``yunus`` aligns with the Latin ``Yunus`` instead of clearing
  as the distinct token ``iunus``. Positional, like schwa deletion; it does
  NOT fire on ``ই`` before a consonant or word-finally.
"""

from __future__ import annotations

__all__ = ["has_bengali", "translit_version", "transliterate_bangla_to_latin"]

# Bengali Unicode block: U+0980-U+09FF.
_BENGALI_LO = 0x0980
_BENGALI_HI = 0x09FF

# Independent vowels (carry their own sound; word start or after a vowel).
_INDEPENDENT_VOWELS: dict[str, str] = {
    "অ": "a",  # A
    "আ": "a",  # AA (long aa -> 'a' for matching simplicity)
    "ই": "i",  # I
    "ঈ": "i",  # II
    "উ": "u",  # U
    "ঊ": "u",  # UU
    "ঋ": "ri",  # vocalic R
    "এ": "e",  # E
    "ঐ": "oi",  # AI
    "ও": "o",  # O
    "ঔ": "ou",  # AU
}

# Dependent vowel signs (matra) — override the preceding consonant's inherent a.
_VOWEL_SIGNS: dict[str, str] = {
    "া": "a",  # AA sign
    "ি": "i",  # I sign
    "ী": "i",  # II sign
    "ু": "u",  # U sign
    "ূ": "u",  # UU sign
    "ৃ": "ri",  # vocalic R sign
    "ে": "e",  # E sign
    "ৈ": "oi",  # AI sign
    "ো": "o",  # O sign
    "ৌ": "ou",  # AU sign
}

# Consonants — each carries an inherent 'a' handled in the main loop.
_CONSONANTS: dict[str, str] = {
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
    "ড়": "r",  # RRA
    "ঢ়": "rh",  # RHA
    "য়": "y",  # YYA
    "ৎ": "t",  # khanda ta
}

# Word-initial ``ই``/``ঈ`` immediately followed by another independent vowel
# glides to ``y`` (i+V onset, e.g. ``ইউনুস`` -> ``yunus``) instead of ``i``.
_GLIDE_I_VOWELS = frozenset({"ই", "ঈ"})

_HASANTA = "্"  # virama — suppresses the inherent vowel (conjunct join)
_ANUSVARA = "ং"  # -> "ng"
_CHANDRABINDU = "ঁ"  # nasalization -> drop for matching
_VISARGA = "ঃ"  # -> "h"
_NUKTA = "়"  # combining dot below -> drop (decomposed RRA/YYA forms)

_DIGITS: dict[str, str] = {
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


def has_bengali(text: str) -> bool:
    """True if the string contains at least one Bengali-script codepoint."""
    return any(_BENGALI_LO <= ord(ch) <= _BENGALI_HI for ch in text)


def translit_version() -> str:
    """Content hash of the transliteration tables (spec/07: the table ships as
    a content-addressed artifact; changing it is a new matcher minor version
    and forces a full-book rescreen). Recorded on every screening row."""
    from bdpay.platform.canonical import sha256_canonical

    return sha256_canonical(
        {
            "independent_vowels": _INDEPENDENT_VOWELS,
            "vowel_signs": _VOWEL_SIGNS,
            "consonants": _CONSONANTS,
            "digits": _DIGITS,
            "marks": {
                "hasanta": _HASANTA,
                "anusvara": _ANUSVARA,
                "chandrabindu": _CHANDRABINDU,
                "visarga": _VISARGA,
                "nukta": _NUKTA,
            },
            "glide_i_vowels": sorted(_GLIDE_I_VOWELS),
        }
    )


def transliterate_bangla_to_latin(text: str) -> str:
    """Romanize Bengali-script runs to Latin; identity on non-Bengali input.

    Walks the string codepoint by codepoint. Consonants emit their base sound
    and tentatively carry an inherent ``a``; a following vowel sign overrides
    it, a hasanta suppresses it, and a word-final consonant drops it (Bengali
    schwa deletion). Non-Bengali characters inside the run pass through.
    """
    if not isinstance(text, str):
        raise TypeError(f"text must be str, got {type(text).__name__}")
    if not has_bengali(text):  # fast path / identity guarantee
        return text

    out: list[str] = []
    pending_inherent_a = False

    def flush_inherent_a() -> None:
        nonlocal pending_inherent_a
        if pending_inherent_a:
            out.append("a")
            pending_inherent_a = False

    chars = list(text)
    length = len(chars)
    for index, ch in enumerate(chars):
        nxt = chars[index + 1] if index + 1 < length else None
        prev = chars[index - 1] if index > 0 else None
        at_word_end = nxt is None or nxt.isspace()
        at_word_start = prev is None or prev.isspace()

        consonant = _CONSONANTS.get(ch)
        if consonant is not None:
            flush_inherent_a()
            out.append(consonant)
            # Carry the inherent 'a' unless this consonant ends the word.
            pending_inherent_a = not at_word_end
            continue
        vowel_sign = _VOWEL_SIGNS.get(ch)
        if vowel_sign is not None:
            pending_inherent_a = False  # matra overrides the inherent 'a'
            out.append(vowel_sign)
            continue
        if ch == _HASANTA:
            pending_inherent_a = False  # conjunct: suppress the inherent vowel
            continue
        if ch == _NUKTA:
            continue  # base consonant already mapped; drop the diacritic
        independent = _INDEPENDENT_VOWELS.get(ch)
        if independent is not None:
            flush_inherent_a()
            # Word-initial ই/ঈ before another independent vowel glides to 'y'
            # (i+V onset: ইউনুস -> yunus). Does not fire before a consonant
            # (উসামা stays usama) or word-finally.
            if (
                ch in _GLIDE_I_VOWELS
                and at_word_start
                and nxt is not None
                and nxt in _INDEPENDENT_VOWELS
            ):
                out.append("y")
            else:
                out.append(independent)
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
            continue  # nasalization mark — drop for matching (no flush)
        digit = _DIGITS.get(ch)
        if digit is not None:
            flush_inherent_a()
            out.append(digit)
            continue
        # Any other char (space, Latin letter, punctuation, unmapped Bengali
        # grapheme): flush any pending inherent vowel, pass the char through.
        flush_inherent_a()
        out.append(ch)

    flush_inherent_a()
    return "".join(out)

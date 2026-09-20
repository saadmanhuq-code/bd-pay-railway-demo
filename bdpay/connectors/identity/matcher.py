"""Sanctions name matcher — Jaro-Winkler + token-sort over normalized names.

Ported from dataroom-bd/src/lib/compliance/sanctions/matcher.ts (PORT TS->PY
per PORTING-MAP and the lane-B brief; thresholds faithful: score >= 0.90 ->
``escalated``, >= 0.80 -> ``pending_review``, else ``cleared``). The spec/07
screener owns the platform-wide matching surface; this port lives with
``sanctions_feed_v1`` so the connector's ``screen()`` capability is fully
implemented and the integration pass can inject the spec/07 screener instead
(SPEC_ERRATA-LANE-B LB7).

Human-in-the-loop decision-support ONLY:
- NEVER auto-clears a hit (``cleared`` only when ``hit`` is False).
- NEVER claims lists that were not actually loaded.
- Empty watchlist -> raises; cannot honestly claim "screened" against nothing
  (the dataroom-bd anti-false-cleared guard, preserved verbatim).

Match scores are unitless ratios in [0, 1] — they are never money and never
enter canonical JSON as floats (stringified at any archive boundary).
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from datetime import datetime

from bdpay.connectors.identity.transliterate import transliterate_bangla_to_latin
from bdpay.platform.canonical import sha256_canonical

__all__ = [
    "DEFAULT_ESCALATE",
    "DEFAULT_REVIEW",
    "EmptyWatchlistError",
    "WatchlistEntry",
    "fold_equivalences",
    "jaro_winkler",
    "match_score",
    "normalize_name",
    "screen_names",
]

DEFAULT_ESCALATE = 0.90
DEFAULT_REVIEW = 0.80


class EmptyWatchlistError(ValueError):
    """Screening against nothing is refused (anti-false-cleared guard)."""


@dataclass(frozen=True)
class WatchlistEntry:
    """One loaded watchlist entry (normalized form precomputed)."""

    entry_id: str
    list_source: str
    raw_name: str
    normalized_name: str
    entity_type: str
    snapshot_date: str
    version: str


# -- Honorific stripping + equivalence folding (equiv_v1, spec/07 §3) -------------
#
# COMP-04 parity: these tables are an intentional byte-for-byte mirror of
# ``bdpay/compliance/sanctions/matcher.py``. The connector layer is forbidden
# from importing ``bdpay.compliance`` (CERT-03 adapter-isolation check), so the
# fold is duplicated here rather than delegated, closing the connector's
# un-injected ``screen()`` false-clear path (a ``Md.`` abbreviation / spelling
# variant of a listed name no longer scores below the review threshold). The
# matcher's ``match_score`` is ``max(token_sort, jaro_winkler, token_set)`` —
# the same fail-closed max-of-components as the compliance matcher. The
# ``token_set`` (Jaccard) term closes the subset/superset false-clear (a
# screened name that is a subset of a listed entry); adding it to a ``max`` can
# only ever RAISE a score, so it never lowers a fuzzy-neighbour case below the
# review threshold. Production injects the spec/07 screener (LB7), which owns
# matching and carries the same fold/glide; this fallback is defence-in-depth.

_HONORIFICS: frozenset[str] = frozenset(
    {
        "mr", "mrs", "ms", "dr", "haji", "hajee", "alhaj", "janab",
        "eng", "engr", "adv", "prof", "late", "lt",
    }
)

_EQUIVALENCE_GROUPS: tuple[tuple[str, ...], ...] = (
    ("muhammad", "md", "mohd", "mohammad", "mohammed", "muhammed", "muhamad",
     "mohamad", "mohammod"),
    ("abdul", "abd", "abdal", "abdel", "abdol"),
    ("uddin", "uddeen", "eddine", "addin"),
    ("hossain", "hosen", "hossen", "hussain", "hussein", "hosain", "husain"),
    ("rahman", "rahaman", "rehman", "rahmaan"),
    ("islam", "eslam"),
    ("ahmed", "ahmad", "ahmmed", "ahamed"),
    ("chowdhury", "choudhuri", "chaudhury", "chowdhuri", "chy", "chowdury"),
    ("khatun", "khatoon"),
    ("begum", "begam"),
    ("sarkar", "sarker", "shorkar"),
    ("sheikh", "shaikh", "shekh"),
)

_EQUIVALENCE_MAP: dict[str, str] = {
    alias: group[0] for group in _EQUIVALENCE_GROUPS for alias in group
}

_CONDITIONAL_LEADING_HONORIFICS: frozenset[str] = frozenset({"sheikh", "sk"})


def fold_equivalences(normalized: str) -> str:
    """Honorific stripping + equivalence folding (``equiv_v1``, spec/07 §3).

    Drops unconditional honorific tokens, folds each remaining token through its
    equivalence class (``mohammed`` -> ``muhammad``, ``shekh`` -> ``sheikh``),
    and drops a leading ``sheikh``/``sk`` only when > 2 name-bearing tokens
    remain. Idempotent.
    """
    tokens = [t for t in normalized.split(" ") if t]
    tokens = [t for t in tokens if t not in _HONORIFICS]
    folded = [_EQUIVALENCE_MAP.get(t, t) for t in tokens]
    if len(folded) > 3 and folded[0] in _CONDITIONAL_LEADING_HONORIFICS:
        folded = folded[1:]
    return " ".join(folded)


# -- Name normalization (pipeline preserved from the source) ---------------------


def normalize_name(raw: str) -> str:
    """Normalize a name for fuzzy matching.

    1. Romanize Bengali script -> Latin (identity on non-Bengali input);
       without this a Bengali-script name shares no codepoints with a Latin
       watchlist entry and scores 0 — a false "cleared".
    2. Lowercase. 3. NFD-decompose and strip Latin combining diacritics.
    4. Any non-letter/non-digit/non-space -> space (Unicode-aware, so an
       unmapped non-Latin letter is PRESERVED, never deleted to empty).
    5. Collapse whitespace and trim.
    6. Honorific stripping + equivalence-class folding (Step 3, ``equiv_v1``) —
       collapses transliteration/abbreviation variants onto the canonical token
       (``md`` -> ``muhammad``) so a listed-name variant cannot false-clear.
    """
    romanized = transliterate_bangla_to_latin(raw).lower()
    decomposed = unicodedata.normalize("NFD", romanized)
    stripped = "".join(ch for ch in decomposed if not ("̀" <= ch <= "ͯ"))
    spaced = "".join(
        ch if (ch.isalpha() or ch.isnumeric() or ch.isspace()) else " " for ch in stripped
    )
    return fold_equivalences(" ".join(spaced.split()))


# -- Jaro-Winkler (faithful port) -------------------------------------------------


def _jaro(a: str, b: str) -> float:
    if a == b:
        return 1.0
    la, lb = len(a), len(b)
    if la == 0 or lb == 0:
        return 0.0
    match_window = max(max(la, lb) // 2 - 1, 0)
    a_matched = [False] * la
    b_matched = [False] * lb
    matches = 0
    for i in range(la):
        lo = max(0, i - match_window)
        hi = min(i + match_window + 1, lb)
        for j in range(lo, hi):
            if not b_matched[j] and a[i] == b[j]:
                a_matched[i] = True
                b_matched[j] = True
                matches += 1
                break
    if matches == 0:
        return 0.0
    transpositions = 0
    k = 0
    for i in range(la):
        if not a_matched[i]:
            continue
        while not b_matched[k]:
            k += 1
        if a[i] != b[k]:
            transpositions += 1
        k += 1
    return (matches / la + matches / lb + (matches - transpositions / 2) / matches) / 3


def jaro_winkler(a: str, b: str) -> float:
    j = _jaro(a, b)
    first_mismatch = -1
    for i, ch in enumerate(a):
        if i >= len(b) or ch != b[i]:
            first_mismatch = i
            break
    prefix_len = min(4, min(len(a), len(b)) if first_mismatch == -1 else first_mismatch)
    p = 0.1
    return j + prefix_len * p * (1 - j)


def _token_sort_ratio(a: str, b: str) -> float:
    sorted_a = " ".join(sorted(a.split(" ")))
    sorted_b = " ".join(sorted(b.split(" ")))
    return jaro_winkler(sorted_a, sorted_b)


def _token_set_ratio(a: str, b: str) -> float:
    """Jaccard token-set ratio ``|A ∩ B| / |A ∪ B|`` over whitespace tokens.

    Empty-vs-empty is 1; empty-vs-nonempty is 0. Rewards subset/superset and
    reordered overlap that the character-level metrics under-credit.
    """
    tokens_a = {t for t in a.split(" ") if t}
    tokens_b = {t for t in b.split(" ") if t}
    if not tokens_a and not tokens_b:
        return 1.0
    if not tokens_a or not tokens_b:
        return 0.0
    return len(tokens_a & tokens_b) / len(tokens_a | tokens_b)


def match_score(normalized_subject: str, normalized_watchlist_entry: str) -> float:
    """``max(jaro_winkler, token_sort_ratio, token_set_ratio)`` — fail-closed.

    The ``max`` can never score below any single component, so it surfaces a
    fuzzy spelling neighbour (Jaro-Winkler), a first/last-name transposition
    (reorder-invariant token-sort), AND a subset/superset variant (token-set
    Jaccard) — the last closing the COMP-04 false-clear where a screened name
    that is a subset of a listed entry (``Uday Saddam Hussein`` vs listed
    ``Uday Saddam Hussein Al-Tikriti``) scored below the review threshold under
    the old ``max(token_sort, jaro_winkler)`` form. Over-flagging is acceptable
    on a sanctions path; false-clearing is not. Production injects the spec/07
    screener (LB7); this fallback is defence-in-depth, kept fail-closed-leaning.
    """
    return max(
        _token_sort_ratio(normalized_subject, normalized_watchlist_entry),
        jaro_winkler(normalized_subject, normalized_watchlist_entry),
        _token_set_ratio(normalized_subject, normalized_watchlist_entry),
    )


# -- Main screening function (faithful port; deterministic IDs) --------------------


def screen_names(
    subjects: list[dict],
    watchlist: list[WatchlistEntry],
    *,
    screened_at: datetime,
    escalate_threshold: float = DEFAULT_ESCALATE,
    review_threshold: float = DEFAULT_REVIEW,
) -> dict:
    """Screen ``subjects`` (``{"ref", "name"}`` dicts) against ``watchlist``.

    The screening id is content-addressed (no random ids — the source's
    nanoid is replaced per spec 00 §3). Score values inside the returned dict
    are stringified ratios so the result is canonical-JSON-safe.
    """
    if len(watchlist) == 0:
        raise EmptyWatchlistError(
            "sanctions screening requires a non-empty watchlist — "
            "cannot honestly claim screening against nothing"
        )
    loaded_sources = sorted({entry.list_source for entry in watchlist})
    loaded_versions = ";".join(sorted({entry.version for entry in watchlist}))

    results: list[dict] = []
    total_hits = 0
    escalated = 0
    for subject in subjects:
        subject_norm = normalize_name(str(subject["name"]))
        best_score = 0.0
        best_entry: WatchlistEntry | None = None
        for entry in watchlist:
            score = match_score(subject_norm, entry.normalized_name)
            if score > best_score:
                best_score = score
                best_entry = entry
        if best_score >= escalate_threshold:
            hit, disposition = True, "escalated"
            total_hits += 1
            escalated += 1
        elif best_score >= review_threshold:
            hit, disposition = True, "pending_review"
            total_hits += 1
        else:
            # Invariant: "cleared" only ever pairs with hit=False.
            hit, disposition = False, "cleared"
        results.append(
            {
                "subject_ref": str(subject.get("ref", "")),
                "subject_name": str(subject["name"]),
                "hit": hit,
                "matched_list": (best_entry.list_source if hit and best_entry else None),
                "matched_entry_id": (best_entry.entry_id if hit and best_entry else None),
                "match_score": f"{best_score:.6f}",
                "disposition": disposition,
            }
        )
    screening_id = "scr-" + sha256_canonical(
        {
            "subjects": [str(s.get("ref", "")) for s in subjects],
            "list_version": loaded_versions,
            "screened_at": screened_at,
        }
    )[:16]
    return {
        "screening_id": screening_id,
        "screened_at": screened_at,
        "sources": loaded_sources,
        "total_subjects": len(subjects),
        "hits": total_hits,
        "escalated": escalated,
        "results": results,
        "list_version": loaded_versions,
    }

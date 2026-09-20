"""Sanctions name matcher — fail-closed max over normalized-name similarities.

Re-implemented in Python from the verified ``dataroom-bd`` matcher lineage
(PORT TS->PY, PORTING-MAP.md; research 07). Behavioural invariants:

- Human-in-the-loop decision support ONLY: a hit is NEVER auto-cleared
  (``disposition == "cleared"`` only when ``hit is False``).
- Empty watchlist REFUSES (raises) — cannot honestly claim "screened"
  against nothing (the anti-false-cleared guard).
- Bengali-script names are romanized (Step 2) and folded through honorific
  stripping + equivalence classes (Step 3) before matching, so a designated
  name written in Bangla — or a transliteration/spelling variant — aligns
  with its Latin watchlist entry instead of silently clearing.
- ``match_score`` is the ``max`` of the three component similarities
  ``max(jaro_winkler, token_sort_ratio, token_set_ratio)`` over the
  canonical-normalized names. The ``max`` is the fail-CLOSED-leaning score:
  it can never fall below ANY single component, so every name variant a
  component would catch (a fuzzy spelling neighbour via ``jaro_winkler``, a
  first/last-name transposition via ``token_sort_ratio``, a subset/superset
  via ``token_set_ratio``) still surfaces. Sanctions matching must over-flag,
  never false-clear; a convex blend that weights the components below their
  ``max`` is the forbidden move (see :func:`match_score`).

COMP-04 (false-clear risk) — supersedes errata E39/E45. Two independent
false-clear gaps were closed:
  1. The faithful-port ``max(token_sort_ratio, jaro_winkler)`` OMITTED the
     token-set (Jaccard) term, so a subset/superset variant (e.g.
     ``Uday Saddam Hussein`` vs listed ``Uday Saddam Hussein Al-Tikriti``)
     could score below the review threshold and false-clear. ``token_set_ratio``
     is now a THIRD term in the ``max``, so subset/superset overlap surfaces.
  2. Step-3 equivalence folding was absent, so Bengali-script / transliterated
     spelling variants compared as distinct tokens. The Step-3 fold collapses
     them onto the canonical token (``mohammed`` → ``muhammad``, ``shekh`` →
     ``sheikh``) so the match is recovered.

A weighted convex blend (the literal spec/07 §5 ``0.50*jw + 0.30*tsr +
0.20*tset``) was tried and REJECTED: it scores BELOW the ``max`` on exactly the
subset/superset and transposition cases above, re-introducing the false-clear
it was meant to fix. BD-PAY deliberately diverges from the spec/07 §5 blend in
favour of ``max`` because over-flagging is acceptable on a sanctions path and
false-clearing is not (errata E45, revised).

BD-PAY divergences (documented): scores are ``decimal.Decimal`` (floats are
banned platform-wide and rejected by canonical_json) quantized by the caller;
IDs and timestamps are minted by the calling service through
``make_compliance_id`` and the injected Clock, never inside the matcher. DOB /
nationality corroboration adjustments (spec/07 §5 table) are applied by the
screening service, not here — the matcher scores names only.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, localcontext

from bdpay.compliance.sanctions.translit import transliterate_bangla_to_latin

__all__ = [
    "DEFAULT_ESCALATE",
    "DEFAULT_REVIEW",
    "EmptyWatchlistError",
    "ScreenedSubject",
    "SubjectScreenResult",
    "WatchlistEntry",
    "WatchlistScreenReport",
    "fold_equivalences",
    "jaro_winkler",
    "match_score",
    "normalize_name",
    "screen_subjects",
    "token_set_ratio",
    "token_sort_ratio",
]

#: Default decision thresholds, faithful to the dataroom-bd matcher.
DEFAULT_ESCALATE = Decimal("0.90")
DEFAULT_REVIEW = Decimal("0.80")

_PRECISION = 50

_ZERO = Decimal(0)
_ONE = Decimal(1)
_WINKLER_P = Decimal("0.1")


class EmptyWatchlistError(ValueError):
    """Sanctions screening requires a non-empty watchlist — fail-closed."""


# ---------------------------------------------------------------------------
# Name normalization (matcher.ts normalizeName, faithful pipeline)
# ---------------------------------------------------------------------------


def _is_combining_diacritic(ch: str) -> bool:
    # matcher.ts strips the Combining Diacritical Marks block U+0300-U+036F.
    return 0x0300 <= ord(ch) <= 0x036F


# ---------------------------------------------------------------------------
# Step 3 — honorific stripping + equivalence-class folding (equiv_v1, spec/07)
# ---------------------------------------------------------------------------

#: Honorific / particle tokens dropped before folding (spec/07 §3 stop-list).
#: Tokens applied unconditionally; ``sheikh`` and ``sk`` are conditional (only
#: when leading AND > 2 remaining tokens — handled in :func:`fold_equivalences`).
#: ``syed`` is name-bearing and intentionally KEPT. The dot-form ``md.`` is
#: already split to ``md`` by Step-1 punctuation stripping, so the bare ``md``
#: token folds to ``muhammad`` (equivalence class) rather than being dropped.
# NOTE on hyphenated spec forms: Step-1 normalization replaces ``-`` with a
# space BEFORE folding, so a spec stop-word/equivalence written with a hyphen
# (``al-haj``, ``ud-din``) can never reach this fold as a single token — it
# arrives split (``al haj``, ``ud din``). Those hyphenated forms are therefore
# intentionally omitted here (listing them would imply coverage that does not
# exist); the non-hyphenated ``alhaj``/``uddin`` forms ARE reachable and kept.
_HONORIFICS: frozenset[str] = frozenset(
    {
        "mr",
        "mrs",
        "ms",
        "dr",
        "haji",
        "hajee",
        "alhaj",
        "janab",
        "eng",
        "engr",
        "adv",
        "prof",
        "late",
        "lt",
    }
)

#: Equivalence classes (spec/07 §3 ``equiv_v1``, canonical form first). Each
#: alias maps to its canonical token; the canonical token maps to itself so the
#: fold is idempotent. Built once at import.
_EQUIVALENCE_GROUPS: tuple[tuple[str, ...], ...] = (
    ("muhammad", "md", "mohd", "mohammad", "mohammed", "muhammed", "muhamad",
     "mohamad", "mohammod"),
    ("abdul", "abd", "abdal", "abdel", "abdol"),
    ("uddin", "uddeen", "eddine", "addin"),  # 'ud-din' splits in Step-1; omitted
    ("hossain", "hosen", "hossen", "hussain", "hussein", "hosain", "husain"),
    ("rahman", "rahaman", "rehman", "rahmaan"),
    ("islam", "eslam"),
    ("ahmed", "ahmad", "ahmmed", "ahamed"),
    ("chowdhury", "choudhuri", "chaudhury", "chowdhuri", "chy", "chowdury"),
    ("khatun", "khatoon"),
    ("begum", "begam"),
    ("sarkar", "sarker", "shorkar"),
    # ``sheikh`` equivalences fold unconditionally as spellings; the *honorific*
    # drop of leading ``sheikh``/``sk`` is the conditional rule below.
    ("sheikh", "shaikh", "shekh"),
)

_EQUIVALENCE_MAP: dict[str, str] = {
    alias: group[0] for group in _EQUIVALENCE_GROUPS for alias in group
}

#: Leading honorifics dropped only when > 2 name-bearing tokens remain
#: (spec/07 §3: ``sheikh(only when leading AND >2 remaining tokens), sk``).
_CONDITIONAL_LEADING_HONORIFICS: frozenset[str] = frozenset({"sheikh", "sk"})


def fold_equivalences(normalized: str) -> str:
    """Apply spec/07 §3 honorific stripping + equivalence folding (``equiv_v1``).

    Input is the space-joined, already-romanized/diacritic-stripped token
    sequence (the output of Step 1-2). Returns the canonical normalized name:

    1. Drop unconditional honorific/particle tokens (``mr``, ``dr``, ...).
    2. Fold each remaining token through its equivalence class (``mohammed`` ->
       ``muhammad``, ``shekh`` -> ``sheikh``, ...).
    3. Drop a *leading* ``sheikh``/``sk`` honorific only when > 2 name-bearing
       tokens remain after folding (otherwise it is name-bearing and kept).

    Idempotent: folding an already-folded name is a no-op.
    """
    tokens = [t for t in normalized.split(" ") if t]
    tokens = [t for t in tokens if t not in _HONORIFICS]
    folded = [_EQUIVALENCE_MAP.get(t, t) for t in tokens]
    if (
        len(folded) > 3
        and folded[0] in _CONDITIONAL_LEADING_HONORIFICS
    ):
        # > 2 tokens REMAIN after dropping the leading honorific.
        folded = folded[1:]
    return " ".join(folded)


def normalize_name(raw: str) -> str:
    """Normalize a name to its spec/07 canonical form for fuzzy matching.

    Pipeline (spec/07 §Steps 1-3): romanize Bengali (Step 2) -> lowercase ->
    NFD-decompose and strip Latin combining diacritics -> replace any
    non-letter/digit/space with a space (Unicode-aware, so unmapped non-Latin
    letters are PRESERVED, not deleted) -> collapse whitespace -> trim ->
    honorific stripping + equivalence-class folding (Step 3, ``equiv_v1``).

    The Step-3 fold is what lets a transliterated / Bengali-script spelling
    variant of a listed name collapse onto the canonical token and match
    (COMP-04). Both list-entry names and screened names pass through here, so
    matching is always canonical-Latin vs canonical-Latin.
    """
    if not isinstance(raw, str):
        raise TypeError(f"raw must be str, got {type(raw).__name__}")
    text = transliterate_bangla_to_latin(raw).lower()
    text = unicodedata.normalize("NFD", text)
    text = "".join(ch for ch in text if not _is_combining_diacritic(ch))
    cleaned: list[str] = []
    for ch in text:
        category = unicodedata.category(ch)
        if category.startswith(("L", "N")) or ch.isspace():
            cleaned.append(ch)
        else:
            cleaned.append(" ")
    collapsed = " ".join("".join(cleaned).split())
    return fold_equivalences(collapsed)


# ---------------------------------------------------------------------------
# Jaro-Winkler (faithful port; Decimal arithmetic, no floats)
# ---------------------------------------------------------------------------


def _jaro(a: str, b: str) -> Decimal:
    if a == b:
        return _ONE
    la, lb = len(a), len(b)
    if la == 0 or lb == 0:
        return _ZERO

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
        return _ZERO

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

    with localcontext() as ctx:
        ctx.prec = _PRECISION
        m = Decimal(matches)
        return (
            m / Decimal(la) + m / Decimal(lb) + (m - Decimal(transpositions) / 2) / m
        ) / Decimal(3)


def jaro_winkler(a: str, b: str) -> Decimal:
    """Jaro-Winkler similarity (p=0.1, max prefix 4) as a Decimal in [0, 1]."""
    j = _jaro(a, b)
    # Common-prefix length, faithful to the TS findIndex form: the first index
    # where a[i] differs from b[i] (b exhausted counts as a mismatch); if a is
    # fully matched, the prefix is min(len(a), len(b)). Capped at 4.
    prefix = min(len(a), len(b))
    for i, ch in enumerate(a):
        if i >= len(b) or ch != b[i]:
            prefix = i
            break
    prefix = min(4, prefix)
    with localcontext() as ctx:
        ctx.prec = _PRECISION
        return j + Decimal(prefix) * _WINKLER_P * (_ONE - j)


def token_sort_ratio(a: str, b: str) -> Decimal:
    """Jaro-Winkler over the space-joined, code-point-sorted token sequences."""
    sorted_a = " ".join(sorted(a.split(" ")))
    sorted_b = " ".join(sorted(b.split(" ")))
    return jaro_winkler(sorted_a, sorted_b)


def token_set_ratio(a: str, b: str) -> Decimal:
    """Jaccard token-set ratio: ``|tokens(a) ∩ tokens(b)| / |tokens(a) ∪ tokens(b)|``.

    spec/07 §5 ``tset`` term. Empty-vs-empty is 1 (identical); empty-vs-nonempty
    is 0. Tokens are the whitespace-split tokens of the canonical-normalized
    names; this rewards subset/superset and reordered overlap that the
    character-level metrics under-credit.
    """
    tokens_a = {t for t in a.split(" ") if t}
    tokens_b = {t for t in b.split(" ") if t}
    if not tokens_a and not tokens_b:
        return _ONE
    if not tokens_a or not tokens_b:
        return _ZERO
    intersection = len(tokens_a & tokens_b)
    union = len(tokens_a | tokens_b)
    with localcontext() as ctx:
        ctx.prec = _PRECISION
        return Decimal(intersection) / Decimal(union)


def match_score(normalized_subject: str, normalized_watchlist_entry: str) -> Decimal:
    """Sanctions match score: ``max`` of the component similarities (fail-closed).

    Inputs are canonical-normalized names (Steps 1-3 already applied, including
    equivalence folding). Returns an unquantized Decimal in [0, 1]; the caller
    (screening service) quantizes to 4dp and applies the corroboration
    adjustments (DOB / nationality / identifier-exact, spec/07 §5 table).

    The score is ``max(jaro_winkler, token_sort_ratio, token_set_ratio)``:

    - ``jaro_winkler`` catches a fuzzy character-level spelling neighbour
      (``Osama`` vs ``Usama``).
    - ``token_sort_ratio`` is reorder-invariant, so a first/last-name
      transposition — the single most common Bangladeshi name variant
      (``Rahim Uddin`` vs the listed ``Uddin Rahim``) — scores ``1`` and HITs.
    - ``token_set_ratio`` (Jaccard over token sets) catches a subset/superset
      variant (``Uday Saddam Hussein`` vs listed ``Uday Saddam Hussein
      Al-Tikriti``) that the character metrics under-credit.

    COMP-04 fail-CLOSED guard: the ``max`` can NEVER score below any single
    component, so it can never lower a name variant below detection. This is
    deliberately stronger (more flagging) than the spec/07 §5 convex weighted
    blend, which scores *below* the ``max`` on exactly the transposition and
    subset/superset cases above and so false-clears designated parties. A higher
    score ⇒ more likely to flag ⇒ fail-closed-leaning; over-flagging is
    acceptable on a sanctions path, false-clearing is not. Both inputs are
    Decimals in [0, 1], so the result stays in [0, 1].
    """
    return max(
        jaro_winkler(normalized_subject, normalized_watchlist_entry),
        token_sort_ratio(normalized_subject, normalized_watchlist_entry),
        token_set_ratio(normalized_subject, normalized_watchlist_entry),
    )


# ---------------------------------------------------------------------------
# Screening over a watchlist (faithful port of screenOwners)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WatchlistEntry:
    """One designated entry (normalized form precomputed at ingestion)."""

    entry_id: str
    list_source: str
    raw_name: str
    normalized_name: str
    entity_type: str  # individual | entity | vessel | aircraft
    snapshot_date: str
    version: str  # opaque provenance string, persisted on every result


@dataclass(frozen=True)
class ScreenedSubject:
    """A subject (customer / merchant / UBO / external party) to screen."""

    subject_id: str
    name: str


@dataclass(frozen=True)
class SubjectScreenResult:
    """Per-subject outcome. ``disposition`` is the match-strength taxonomy."""

    subject_id: str
    subject_name: str
    hit: bool
    matched_list: str | None
    score: Decimal
    disposition: str  # escalated | pending_review | cleared
    matched_entry_id: str | None


@dataclass(frozen=True)
class WatchlistScreenReport:
    """Whole-screen outcome (sources honesty: only lists actually loaded)."""

    sources: tuple[str, ...]
    total_subjects: int
    hits: int
    escalated: int
    cleared_after_review: int
    results: tuple[SubjectScreenResult, ...]
    list_version: str
    screened_at: datetime | None


def screen_subjects(
    subjects: list[ScreenedSubject] | tuple[ScreenedSubject, ...],
    watchlist: list[WatchlistEntry] | tuple[WatchlistEntry, ...],
    *,
    escalate_threshold: Decimal = DEFAULT_ESCALATE,
    review_threshold: Decimal = DEFAULT_REVIEW,
    screened_at: datetime | None = None,
) -> WatchlistScreenReport:
    """Screen subjects against the watchlist (faithful port of screenOwners).

    Raises :class:`EmptyWatchlistError` on an empty watchlist — the
    anti-false-cleared guard, adopted verbatim (research 07).
    """
    if len(watchlist) == 0:
        raise EmptyWatchlistError(
            "sanctions screening requires a non-empty watchlist - cannot honestly "
            "claim screening against nothing"
        )
    if review_threshold > escalate_threshold:
        raise ValueError("review_threshold must not exceed escalate_threshold")

    # The ACTUALLY loaded list sources, deduplicated, insertion-ordered.
    loaded_sources: list[str] = []
    for entry in watchlist:
        if entry.list_source not in loaded_sources:
            loaded_sources.append(entry.list_source)

    results: list[SubjectScreenResult] = []
    total_hits = 0
    escalated = 0
    cleared_after_review = 0

    for subject in subjects:
        subject_norm = normalize_name(subject.name)
        best_score = _ZERO
        best_entry: WatchlistEntry | None = None
        for entry in watchlist:
            score = match_score(subject_norm, entry.normalized_name)
            if score > best_score:
                best_score = score
                best_entry = entry

        if best_score >= escalate_threshold:
            hit = True
            disposition = "escalated"
            total_hits += 1
            escalated += 1
        elif best_score >= review_threshold:
            hit = True
            disposition = "pending_review"
            total_hits += 1
        else:
            hit = False
            disposition = "cleared"

        # Invariant: NEVER auto-set cleared on a hit (cleared only when not hit).
        results.append(
            SubjectScreenResult(
                subject_id=subject.subject_id,
                subject_name=subject.name,
                hit=hit,
                matched_list=best_entry.list_source if hit and best_entry else None,
                score=best_score,
                disposition=disposition,
                matched_entry_id=best_entry.entry_id if hit and best_entry else None,
            )
        )

    loaded_versions = ";".join(sorted({entry.version for entry in watchlist}))
    return WatchlistScreenReport(
        sources=tuple(loaded_sources),
        total_subjects=len(subjects),
        hits=total_hits,
        escalated=escalated,
        cleared_after_review=cleared_after_review,
        results=tuple(results),
        list_version=loaded_versions,
        screened_at=screened_at,
    )

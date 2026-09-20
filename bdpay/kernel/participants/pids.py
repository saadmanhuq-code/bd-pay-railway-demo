"""Format-parity ID helper for the spec/19 PSO-1 additive ID prefixes.

spec/19 §Entities registers ``pdoc`` (ParticipantDocument), ``pmou``
(ParticipantMou) and ``confr`` (ConformanceRun) — none of which are in the
frozen spec/00 §3 table, so :data:`bdpay.platform.ids.PREFIXES` rejects them.
Per the E18/E28/E37 shim precedent (and spec/19's own instruction: "Until
folded into ``bdpay.platform.ids.PREFIXES``, implementations use the
format-parity shim pattern"), this module applies the identical
content-addressed scheme ``<prefix>_<sha256_canonical(payload)[:24]>`` over
the one binding E12 canonical implementation, collapsing to a plain
:func:`bdpay.platform.ids.make_id` call at the next prefix fold-in.

Recorded as errata row P19-6 in SPEC_ERRATA-LANE-A-spec19.md; pinned by
``tests/kernel/participants/test_pids.py``.
"""

from __future__ import annotations

from bdpay.platform.canonical import sha256_canonical
from bdpay.platform.ids import HASH_LENGTH, PREFIXES, IdPrefixError, make_id

__all__ = ["PARTICIPANT_PREFIXES", "make_participant_id"]

#: Additive prefixes minted by spec/19 PSO-1 (the other spec/19 prefixes —
#: ``nrun``/``nobl``/``pcert`` — belong to the PSO-2/PSO-4 lanes).
PARTICIPANT_PREFIXES: frozenset[str] = frozenset({"pdoc", "pmou", "confr"})


def make_participant_id(prefix: str, payload: dict) -> str:
    """``make_id`` with the spec/19 PSO-1 additive prefixes accepted.

    Canonical prefixes delegate straight to the platform registry; the
    additive set uses the byte-identical scheme so the IDs are
    indistinguishable in format and collapse to ``make_id`` once the
    prefixes are folded into spec/00 §3.
    """
    if prefix in PREFIXES:
        return make_id(prefix, payload)
    if prefix not in PARTICIPANT_PREFIXES:
        raise IdPrefixError(
            f"unknown id prefix {prefix!r}; allowed are the spec/00 §3 table plus "
            f"the spec/19 PSO-1 additive set {sorted(PARTICIPANT_PREFIXES)}"
        )
    if not isinstance(payload, dict):
        raise TypeError(f"payload must be dict, got {type(payload).__name__}")
    return f"{prefix}_{sha256_canonical(payload)[:HASH_LENGTH]}"

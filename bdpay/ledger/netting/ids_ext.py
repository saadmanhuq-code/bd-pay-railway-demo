"""Netting ID prefixes — format-parity extension of ``bdpay.platform.ids``.

spec/19 registers the additive prefixes ``nrun`` (NettingRun) and ``nobl``
(NettingObligation); neither is in the frozen spec/00 §3 table yet, so
``bdpay.platform.ids.make_id`` rejects them. Per the SPEC_ERRATA E18/E28/E37
shim pattern (and spec/19's own instruction to use it), this module mints IDs
with the IDENTICAL ``<prefix>_<sha256_canonical(payload)[:24]>`` scheme over
the one binding E12 canonical implementation, and collapses to a plain
``make_id`` call at the next prefix fold-in. Errata row N19-8 records the
deviation; a test pins format parity.
"""

from __future__ import annotations

from bdpay.platform.canonical import sha256_canonical
from bdpay.platform.ids import HASH_LENGTH, PREFIXES, IdPrefixError, make_id

__all__ = ["NETTING_PREFIXES", "make_netting_id"]

#: spec/19 Entities table prefixes owned by the netting workstream (PSO-2).
NETTING_PREFIXES: frozenset[str] = frozenset({"nrun", "nobl"})


def make_netting_id(prefix: str, payload: dict) -> str:
    """``make_id`` for canonical prefixes; format-parity mint for netting ones."""
    if prefix in PREFIXES:
        return make_id(prefix, payload)
    if prefix not in NETTING_PREFIXES:
        raise IdPrefixError(
            f"unknown id prefix {prefix!r}; allowed: spec/00 §3 table + "
            f"{sorted(NETTING_PREFIXES)}"
        )
    if not isinstance(payload, dict):
        raise TypeError(f"payload must be dict, got {type(payload).__name__}")
    return f"{prefix}_{sha256_canonical(payload)[:HASH_LENGTH]}"

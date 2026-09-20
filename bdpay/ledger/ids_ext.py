"""Ledger-internal ID prefixes — format-parity extension of bdpay.platform.ids.

spec/03 names three ledger-internal local-convention prefixes (``hold``,
``lckp``, ``rply``) and spec/04 adds ``frule`` (fee rules); none of them is in
the frozen spec/00 §3 table, so ``bdpay.platform.ids.make_id`` rejects them.
Same situation as E18 (lane B's ``connectors/ids_ext.py``) and PR-3 (the
notification dispatch shim): this module mints IDs with the IDENTICAL
``<prefix>_<sha256_canonical(payload)[:24]>`` scheme over E12 canonical bytes,
and collapses to a plain ``make_id`` call at the next prefix fold-in.
Errata row LA-L5 records the deviation; a test pins format parity.
"""

from __future__ import annotations

from bdpay.platform.canonical import sha256_canonical
from bdpay.platform.ids import HASH_LENGTH, PREFIXES, IdPrefixError, make_id

__all__ = ["LEDGER_INTERNAL_PREFIXES", "is_well_formed", "make_ledger_id"]

#: Ledger-internal prefixes (spec/03 data-model notes + spec/04 fee_rules).
LEDGER_INTERNAL_PREFIXES: frozenset[str] = frozenset({"hold", "lckp", "rply", "frule"})


def make_ledger_id(prefix: str, payload: dict) -> str:
    """``make_id`` for canonical prefixes; format-parity mint for internal ones."""
    if prefix in PREFIXES:
        return make_id(prefix, payload)
    if prefix not in LEDGER_INTERNAL_PREFIXES:
        raise IdPrefixError(
            f"unknown id prefix {prefix!r}; allowed: spec/00 §3 table + "
            f"{sorted(LEDGER_INTERNAL_PREFIXES)}"
        )
    if not isinstance(payload, dict):
        raise TypeError(f"payload must be dict, got {type(payload).__name__}")
    return f"{prefix}_{sha256_canonical(payload)[:HASH_LENGTH]}"


def is_well_formed(id_str: str, prefix: str) -> bool:
    """Shape check (prefix + 24 lowercase hex chars) without knowing the payload."""
    expected_len = len(prefix) + 1 + HASH_LENGTH
    if len(id_str) != expected_len or not id_str.startswith(prefix + "_"):
        return False
    suffix = id_str[len(prefix) + 1 :]
    return all(c in "0123456789abcdef" for c in suffix)

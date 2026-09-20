"""Replay-local ID minting — format-parity shim over the platform scheme.

``rply`` is already registered by ``bdpay.ledger.ids_ext`` (LA-L5). The two
additional prefixes this workstream touches are not in the frozen spec/00 §3
table nor in the ledger-internal set:

- ``dxp``  — DossierExport rows (spec/16 table; the ``PSO_DISPUTE_BUNDLE``
  dossier_type is registered by spec/19 PSO-3). The compliance lane registers
  the same prefix in ``bdpay.compliance.ids_ext``; format parity between the
  two shims is pinned by test (E-S19R-05).
- ``nrun`` — NettingRun (spec/19 PSO-2 entity, the netting lane's table).
  Replay re-derives the content-addressed ``nrun_`` id from
  ``{settlement_window_id, inputs_hash}`` as an integrity cross-check.

Identical scheme everywhere: ``<prefix>_<sha256_canonical(payload)[:24]>``
over the one binding E12 canonical implementation, collapsing to a plain
``make_id`` call at the next prefix fold-in (SPEC_ERRATA E18/E28/E37 pattern).
"""

from __future__ import annotations

from bdpay.ledger.ids_ext import LEDGER_INTERNAL_PREFIXES, make_ledger_id
from bdpay.platform.canonical import sha256_canonical
from bdpay.platform.ids import HASH_LENGTH, PREFIXES, IdPrefixError

__all__ = ["REPLAY_LOCAL_PREFIXES", "make_replay_id"]

#: Additive prefixes used by the replay workstream (spec/16 + spec/19).
REPLAY_LOCAL_PREFIXES: frozenset[str] = frozenset({"dxp", "nrun"})


def make_replay_id(prefix: str, payload: dict) -> str:
    """Mint a content-addressed id; exact format parity with ``make_id``."""
    if not isinstance(prefix, str):
        raise IdPrefixError(f"prefix must be str, got {type(prefix).__name__}")
    if prefix in PREFIXES or prefix in LEDGER_INTERNAL_PREFIXES:
        return make_ledger_id(prefix, payload)
    if prefix not in REPLAY_LOCAL_PREFIXES:
        raise IdPrefixError(
            f"unknown id prefix {prefix!r}; allowed: spec/00 §3 table + "
            f"{sorted(LEDGER_INTERNAL_PREFIXES)} + {sorted(REPLAY_LOCAL_PREFIXES)}"
        )
    if not isinstance(payload, dict):
        raise TypeError(f"payload must be dict, got {type(payload).__name__}")
    return f"{prefix}_{sha256_canonical(payload)[:HASH_LENGTH]}"

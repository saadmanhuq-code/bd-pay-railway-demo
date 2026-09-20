"""Format-parity IDs for the spec/18 G4-granted prefixes ``off``/``ored``/``octr``.

Spec/18 registers its three prefixes (G4 GRANTED, principal directive
2026-06-12) after the frozen spec/00 §3 table, so this package owns the
temporary shim per the E18 / E28 precedent: the byte-identical
content-addressed scheme ``<prefix>_<sha256_canonical(payload)[:24]>`` over
the one binding canonical-JSON implementation, collapsing to a plain
:func:`bdpay.platform.ids.make_id` call at the next prefix fold-in.

Preimages (spec/18 §Entities, binding):

- ``off_<sha>``  — ``{merchant_id, kind, created_idem_key}``
- ``ored_<sha>`` — ``{offer_id, payment_intent_id}`` (one redemption per pair)
- ``octr_<sha>`` — ``{offer_id, scope, scope_key}``
"""

from __future__ import annotations

from bdpay.platform.canonical import sha256_canonical
from bdpay.platform.ids import HASH_LENGTH, PREFIXES, IdPrefixError, make_id

__all__ = ["OFFER_PREFIXES", "make_offer_id"]

#: spec/18 additive prefixes (G4 grant) pending fold-in to spec/00 §3.
OFFER_PREFIXES: frozenset[str] = frozenset({"off", "ored", "octr"})


def make_offer_id(prefix: str, payload: dict) -> str:
    """Return ``<prefix>_<sha256_canonical(payload)[:24]>`` for spec/18 IDs."""
    if prefix in PREFIXES:
        return make_id(prefix, payload)
    if prefix not in OFFER_PREFIXES:
        raise IdPrefixError(
            f"unknown offer id prefix {prefix!r}; allowed are the spec/00 §3 table "
            f"plus the spec/18 additive set {sorted(OFFER_PREFIXES)}"
        )
    if not isinstance(payload, dict):
        raise TypeError(f"payload must be dict, got {type(payload).__name__}")
    return f"{prefix}_{sha256_canonical(payload)[:HASH_LENGTH]}"

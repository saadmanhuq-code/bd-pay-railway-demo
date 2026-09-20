"""Format-parity IDs for spec/17 subscription prefixes.

Spec/17 registers ``subs`` / ``scyc`` / ``mand`` after the frozen spec/00
prefix table, so this package owns the temporary shim. The scheme is byte-for
byte the same content-addressed format as :func:`bdpay.platform.ids.make_id`.
"""

from __future__ import annotations

from bdpay.platform.canonical import sha256_canonical
from bdpay.platform.ids import HASH_LENGTH, PREFIXES, IdPrefixError, make_id

__all__ = ["SUBSCRIPTION_PREFIXES", "make_subscription_id"]

SUBSCRIPTION_PREFIXES: frozenset[str] = frozenset({"subs", "scyc", "mand"})


def make_subscription_id(prefix: str, payload: dict) -> str:
    """Return ``<prefix>_<sha256_canonical(payload)[:24]>`` for VX3 IDs."""
    if prefix in PREFIXES:
        return make_id(prefix, payload)
    if prefix not in SUBSCRIPTION_PREFIXES:
        raise IdPrefixError(
            f"unknown subscription id prefix {prefix!r}; allowed: "
            f"{sorted(SUBSCRIPTION_PREFIXES)}"
        )
    if not isinstance(payload, dict):
        raise TypeError(f"payload must be dict, got {type(payload).__name__}")
    return f"{prefix}_{sha256_canonical(payload)[:HASH_LENGTH]}"

"""Format-parity ID helper for spec/17 disbursement prefixes."""

from __future__ import annotations

from bdpay.platform.canonical import sha256_canonical
from bdpay.platform.ids import HASH_LENGTH, PREFIXES, IdPrefixError, make_id

__all__ = ["DISBURSEMENT_PREFIXES", "make_disbursement_id"]

DISBURSEMENT_PREFIXES: frozenset[str] = frozenset(
    {
        "dbat",  # DisbursementBatch, spec/17 VX4
        "ditm",  # DisbursementItem, spec/17 VX4
    }
)


def make_disbursement_id(prefix: str, payload: dict) -> str:
    """Return ``<prefix>_<sha256_canonical(payload)[:24]>`` for spec/17 IDs."""
    if prefix in PREFIXES:
        return make_id(prefix, payload)
    if prefix not in DISBURSEMENT_PREFIXES:
        raise IdPrefixError(
            f"unknown id prefix {prefix!r}; allowed are the spec/17 disbursement "
            f"prefixes {sorted(DISBURSEMENT_PREFIXES)}"
        )
    if not isinstance(payload, dict):
        raise TypeError(f"payload must be dict, got {type(payload).__name__}")
    return f"{prefix}_{sha256_canonical(payload)[:HASH_LENGTH]}"

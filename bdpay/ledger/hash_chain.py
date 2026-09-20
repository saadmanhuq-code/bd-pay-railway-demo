"""compute_entry_hash — the binding chain-hash preimage (spec/03 source notes).

Ported from bd-pay-fable-pilot/src/bdpay_pilot/platform/hash_chain.py
(verified pilot) into bdpay/ledger per the build plan (the pilot platform
modules are not ported; chain mechanics live with the ledger).

Preimage (pipe-delimited, deterministic field order):
    "{prev_hash}|{chain_index}|{chain_domain}|{entry_type}|{payload_hash}"
    "|{payload_pointer}|{amount_minor_sum}|{producer}|{produced_at}"

payload_pointer is MANDATORY (never empty): a pointer-swap attempt is
detectable precisely because the pointer is inside the preimage.

``to_canonical_ts`` produces the E12 canonical timestamp form (UTC,
MILLISECOND precision, ``Z`` suffix) — the same string ``canonical_json``
emits for datetime values — so chain preimages and content hashes share one
timestamp wire form. The pilot used variable-precision ``isoformat()``;
errata row LA-L6 pins the E12 form as binding here.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

GENESIS_PREV_HASH = "0" * 64
CHAIN_DOMAINS = ("MONEY", "AUDIT")


class HashChainError(ValueError):
    """Raised for malformed preimage inputs (fail closed)."""


def to_canonical_ts(dt: datetime) -> str:
    """Canonical UTC ISO-8601 string, millisecond precision, Z suffix (E12)."""
    if not isinstance(dt, datetime):
        raise HashChainError(f"expected datetime, got {type(dt).__name__}")
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        raise HashChainError("naive datetime rejected: hash preimages require aware UTC")
    dt = dt.astimezone(UTC)
    return (
        f"{dt.year:04d}-{dt.month:02d}-{dt.day:02d}"
        f"T{dt.hour:02d}:{dt.minute:02d}:{dt.second:02d}"
        f".{dt.microsecond // 1000:03d}Z"
    )


def compute_entry_hash(
    prev_hash: str,
    chain_index: int,
    chain_domain: str,
    entry_type: str,
    payload_hash: str,
    payload_pointer: str,
    amount_minor_sum: int,
    producer: str,
    produced_at: str,
) -> str:
    """sha256 hexdigest of the canonical pipe-delimited preimage."""
    if chain_domain not in CHAIN_DOMAINS:
        raise HashChainError(f"chain_domain must be one of {CHAIN_DOMAINS}, got {chain_domain!r}")
    if not payload_pointer:
        raise HashChainError("payload_pointer is mandatory in the hash preimage (doctrine §9.5)")
    if not prev_hash or len(prev_hash) != 64:
        raise HashChainError("prev_hash must be a 64-char hex digest (or the genesis sentinel)")
    if chain_index < 0:
        raise HashChainError(f"chain_index must be >= 0, got {chain_index}")
    if isinstance(amount_minor_sum, bool) or amount_minor_sum < 0:
        raise HashChainError(
            f"amount_minor_sum must be a non-negative int, got {amount_minor_sum!r}"
        )
    if not produced_at.endswith("Z"):
        raise HashChainError("produced_at must be the canonical UTC ISO-8601 'Z' string")
    preimage = (
        f"{prev_hash}|{chain_index}|{chain_domain}|{entry_type}|{payload_hash}"
        f"|{payload_pointer}|{amount_minor_sum}|{producer}|{produced_at}"
    )
    return hashlib.sha256(preimage.encode("utf-8")).hexdigest()

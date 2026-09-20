"""Identity-package determinism helpers (spec/12; LB2 additive-prefix pattern).

Re-exports the spec/12 additive-prefix ``make_spec12_id`` (single E12 hash
implementation) and provides ``sha_bucket`` — the ONLY branching source the
identity-side simulators use (no randomness anywhere; same rule as the
spec/10 scenario engine's ``ref_hash`` buckets).
"""

from __future__ import annotations

import hashlib

from bdpay.connectors.mfs.spec12_ids import SPEC12_PREFIXES, make_spec12_id

__all__ = ["SPEC12_PREFIXES", "make_spec12_id", "sha_bucket"]


def sha_bucket(text: str, *, buckets: int = 10) -> int:
    """Deterministic bucket of ``sha256(text)`` — pure function, no RNG."""
    if buckets < 1:
        raise ValueError("buckets must be >= 1")
    return int(hashlib.sha256(text.encode("utf-8")).hexdigest(), 16) % buckets

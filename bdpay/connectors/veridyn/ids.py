"""Additive ``vchk`` ID prefix (spec/12 Entities; SPEC_ERRATA-LANE-B LB2 pattern).

spec/12 registers ``vchk`` (VeridynCheckRequest) "additive to conventions §3",
but the frozen ``bdpay.platform.ids.PREFIXES`` table predates spec/12 and
rejects it. The spec/12 MFS group's ``spec12_ids.py`` intentionally excludes
``vchk`` because ``veridyn_p2_compliance_v1`` was outside that group's brief
scope, so the prefix ships here with the connector that mints it — the same
LB2/LB7 mechanism: a make_id-equivalent helper built on the one canonical
hash (``sha256_canonical``, errata E12), producing the identical
``<prefix>_<sha256_canonical(payload)[:24]>`` format. A format-parity test
pins equivalence against ``make_id``. Fold into the platform PREFIXES when
lane A absorbs the spec/12 prefixes.
"""

from __future__ import annotations

from bdpay.platform.canonical import sha256_canonical
from bdpay.platform.ids import HASH_LENGTH, PREFIXES, IdPrefixError, make_id

__all__ = ["VERIDYN_PREFIXES", "make_veridyn_id"]

#: Additive prefix table from spec/12 Entities (spec/00 §3 scheme).
VERIDYN_PREFIXES: frozenset[str] = frozenset(
    {
        "vchk",  # VeridynCheckRequest
    }
)


def make_veridyn_id(prefix: str, payload: dict) -> str:
    """Return ``<prefix>_<sha256_canonical(payload)[:24]>``.

    Accepts the spec/12 ``vchk`` prefix and delegates to the frozen platform
    ``make_id`` for any prefix already in the spec/00 §3 table, so both
    families share the single E12 canonical hash implementation.
    """
    if not isinstance(prefix, str):
        raise IdPrefixError(f"prefix must be str, got {type(prefix).__name__}")
    if prefix in PREFIXES:
        return make_id(prefix, payload)
    if prefix not in VERIDYN_PREFIXES:
        raise IdPrefixError(
            f"unknown id prefix {prefix!r}; allowed are spec 00 §3 plus the "
            "spec 12 veridyn additive prefix"
        )
    if not isinstance(payload, dict):
        raise TypeError(f"payload must be dict, got {type(payload).__name__}")
    return f"{prefix}_{sha256_canonical(payload)[:HASH_LENGTH]}"

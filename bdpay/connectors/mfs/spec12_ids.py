"""Additive spec/12 ID prefixes (spec/12 Entities; SPEC_ERRATA-LANE-B LB2 pattern).

spec/12 registers prefixes ``mtok``, ``gfil``, ``sfeed``, ``ntfm``, ``ntpl``, ``mhndl``
("additive to conventions §3"), but the frozen ``bdpay.platform.ids.PREFIXES``
table predates spec/12 and rejects them. Per LB2 the lane-B connector packages
define their own additive prefix helper built on the one canonical hash
(``sha256_canonical``, errata E12), producing the identical
``<prefix>_<sha256_canonical(payload)[:24]>`` format. A format-parity test pins
equivalence against ``make_id``. Fold into the platform PREFIXES when lane A
absorbs the spec/12 prefixes.

``vchk`` (VeridynCheckRequest) is intentionally absent: the
``veridyn_p2_compliance_v1`` connector is outside the lane-B brief's spec/12
group (see LANE-B-BRIEF scope table).
"""

from __future__ import annotations

from bdpay.platform.canonical import sha256_canonical
from bdpay.platform.ids import HASH_LENGTH, PREFIXES, IdPrefixError, make_id

__all__ = ["SPEC12_PREFIXES", "make_spec12_id"]

#: Additive prefix table from spec/12 Entities (spec/00 §3 scheme).
SPEC12_PREFIXES: frozenset[str] = frozenset(
    {
        "mtok",  # MfsTokenState
        "gfil",  # GoamlFiling
        "sfeed",  # SanctionsFeedVersion
        "ntfm",  # NotificationMessage
        "ntpl",  # NotificationTemplate
        "mhndl",  # MfsPaymentHandle (CONN-02)
    }
)


def make_spec12_id(prefix: str, payload: dict) -> str:
    """Return ``<prefix>_<sha256_canonical(payload)[:24]>``.

    Accepts the spec/12 additive prefixes and delegates to the frozen platform
    ``make_id`` for any prefix already in the spec/00 §3 table, so both
    families share the single E12 canonical hash implementation.
    """
    if not isinstance(prefix, str):
        raise IdPrefixError(f"prefix must be str, got {type(prefix).__name__}")
    if prefix in PREFIXES:
        return make_id(prefix, payload)
    if prefix not in SPEC12_PREFIXES:
        raise IdPrefixError(
            f"unknown id prefix {prefix!r}; allowed are spec 00 §3 plus spec 12 additive prefixes"
        )
    if not isinstance(payload, dict):
        raise TypeError(f"payload must be dict, got {type(payload).__name__}")
    return f"{prefix}_{sha256_canonical(payload)[:HASH_LENGTH]}"

"""Format-parity ID helper for the spec/08 + spec/09 additive ID prefixes.

Specs 08 and 09 mint IDs with prefixes that are in neither the spec/00 §3
table nor the SPEC_ERRATA E18 fold-in list, so the frozen
:data:`bdpay.platform.ids.PREFIXES` registry rejects them. Per the E18 /
PR-3 precedent (lane B ``connectors/ids_ext.py``; platform
``notifications._make_dispatch_id``), this module is the kernel's shim: the
identical content-addressed scheme ``<prefix>_<sha256_canonical(payload)[:24]>``
(E12 canonical bytes), restricted to a closed prefix set, collapsing to a
plain :func:`bdpay.platform.ids.make_id` call at the next prefix fold-in.

Recorded as errata row K-2 in SPEC_ERRATA-LANE-A-kernel.md; pinned by
``tests/kernel/test_ids_ext.py``.
"""

from __future__ import annotations

from bdpay.platform.canonical import sha256_canonical
from bdpay.platform.ids import HASH_LENGTH, PREFIXES, IdPrefixError, make_id

__all__ = ["KERNEL_PREFIXES", "make_kernel_id"]

#: Additive prefixes minted by specs 02/08/09 outside the spec/00 §3 table.
KERNEL_PREFIXES: frozenset[str] = frozenset(
    {
        "cref",  # connector idempotency reference (spec/02 orchestration algorithm)
        "doc",  # KYB document (spec/08 §8.2)
        "ubon",  # UBO graph node (spec/08 §8.4)
        "uboe",  # UBO graph edge (spec/08 DDL)
        "mccr",  # MCC risk rule (spec/08 DDL)
        "agr",  # merchant agreement token id (spec/08 §8.9)
        "agrac",  # merchant agreement acceptance (spec/08 §8.9)
        "kybr",  # KYB refresh schedule row (spec/08 DDL)
        "plv",  # participant license verification (spec/08 DDL)
        "apik",  # API-key issuance record (spec/08 activation; hash-stored secret)
        "vcref",  # Porichoy verification reference (spec/08 §8.5)
        "kycdoc",  # KYC document capture (spec/09 DDL)
        "kycbio",  # KYC biometric attempt (spec/09 DDL)
        "kycrev",  # KYC manual review (spec/09 DDL)
        "kyccfg",  # KYC limit config row (spec/09 DDL)
    }
)


def make_kernel_id(prefix: str, payload: dict) -> str:
    """``make_id`` with the kernel's additive prefixes accepted.

    Canonical prefixes delegate straight to the platform registry; the
    additive set uses the byte-identical scheme so the IDs are
    indistinguishable in format and collapse to ``make_id`` once the
    prefixes are folded into spec/00 §3.
    """
    if prefix in PREFIXES:
        return make_id(prefix, payload)
    if prefix not in KERNEL_PREFIXES:
        raise IdPrefixError(
            f"unknown id prefix {prefix!r}; allowed are the spec/00 §3 table plus "
            f"the kernel additive set {sorted(KERNEL_PREFIXES)}"
        )
    if not isinstance(payload, dict):
        raise TypeError(f"payload must be dict, got {type(payload).__name__}")
    return f"{prefix}_{sha256_canonical(payload)[:HASH_LENGTH]}"

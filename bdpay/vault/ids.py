"""Additive vault-package ID prefixes (spec/14 Entities; SPEC_ERRATA-LANE-B LB2 pattern).

spec/14 extends the conventions §3 prefix table with ``tok``, ``epan``,
``vkey``, ``vcus``, ``vcer``, ``vint``, ``vaud``, ``vobx``, ``vrun`` ("all
generated via make_id(prefix, payload)"), but the frozen
``bdpay.platform.ids.PREFIXES`` table predates spec/14 and rejects them.
Per the LB2 resolution this module provides the make_id-equivalent helper
for the additive vault prefixes, built on the one canonical hash
(``sha256_canonical``, errata E12), producing the identical
``<prefix>_<sha256_canonical(payload)[:24]>`` format. A format-parity test
pins equivalence against ``make_id``. Fold into the platform PREFIXES when
lane A absorbs the spec/14 prefixes.

The vault build deliberately imports only ``bdpay.platform`` (spec/14
topology: the vault wheel never imports kernel/ledger/compliance/connectors/qr),
so this module does not reuse ``bdpay.connectors.ids_ext``.
"""

from __future__ import annotations

from bdpay.platform.canonical import sha256_canonical
from bdpay.platform.ids import HASH_LENGTH, PREFIXES, IdPrefixError, make_id

__all__ = ["VAULT_PREFIXES", "make_vault_id"]

#: Additive prefix table from spec/14 §Entities (spec/00 §3 scheme).
VAULT_PREFIXES: frozenset[str] = frozenset(
    {
        "tok",  # CardToken
        "epan",  # EncryptedPan
        "vkey",  # VaultKey
        "vcus",  # KeyCustodian
        "vcer",  # KeyCeremony
        "vint",  # CardIntakeSession
        "vaud",  # VaultAuditEntry
        "vobx",  # VaultOutboxEvent
        "vrun",  # ReencryptionRun
    }
)


def make_vault_id(prefix: str, payload: dict) -> str:
    """Return ``<prefix>_<sha256_canonical(payload)[:24]>``.

    Accepts the spec/14 additive prefixes and delegates to the frozen
    platform ``make_id`` for any prefix already in the spec/00 §3 table
    (``appr``, ``ins``, ...), so both families share the single E12
    canonical hash implementation.
    """
    if not isinstance(prefix, str):
        raise IdPrefixError(f"prefix must be str, got {type(prefix).__name__}")
    if prefix in PREFIXES:
        return make_id(prefix, payload)
    if prefix not in VAULT_PREFIXES:
        raise IdPrefixError(
            f"unknown id prefix {prefix!r}; allowed are spec 00 §3 plus spec 14 additive prefixes"
        )
    if not isinstance(payload, dict):
        raise TypeError(f"payload must be dict, got {type(payload).__name__}")
    return f"{prefix}_{sha256_canonical(payload)[:HASH_LENGTH]}"

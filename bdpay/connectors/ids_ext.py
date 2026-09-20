"""Additive connector-package ID prefixes (spec/10 Entities; SPEC_ERRATA-LANE-B LB2).

spec/10 registers prefixes ``creg``, ``cmode``, ``chlth``, ``cbrk``, ``winb``,
``simsc``, ``certr`` "same make_id scheme" (spec/00 §3), but the frozen
``bdpay.platform.ids.PREFIXES`` table predates spec/10 and rejects them.
Per LB2 this module provides the make_id-equivalent helper for the additive
prefixes, built on the one canonical hash (``sha256_canonical``, errata E12),
producing the identical ``<prefix>_<sha256_canonical(payload)[:24]>`` format.
A format-parity test pins equivalence against ``make_id``. Fold into the
platform PREFIXES when lane A absorbs the spec/10 prefixes.
"""

from __future__ import annotations

from bdpay.platform.canonical import sha256_canonical
from bdpay.platform.ids import HASH_LENGTH, PREFIXES, IdPrefixError, make_id

__all__ = ["CONNECTOR_PREFIXES", "make_connector_id"]

#: Additive prefix table from spec/10 Entities (spec/00 §3 scheme), extended
#: with the spec/11 rails Entities table ("additive to conventions §3, same
#: make_id scheme") — same LB2 mechanism, folded into platform PREFIXES later.
CONNECTOR_PREFIXES: frozenset[str] = frozenset(
    {
        "creg",  # ConnectorRegistration
        "cmode",  # ConnectorModeChange
        "chlth",  # ConnectorHealthSample
        "cbrk",  # CircuitBreakerState
        "winb",  # WebhookInbound
        "simsc",  # SimulatorScenario
        "certr",  # CertificationRun
        # spec/11 rails additions
        "iso",  # Iso8583Message
        "stan",  # StanAllocation
        "rsess",  # RailSessionState
        "saf",  # StoreAndForwardItem
        "bfile",  # BeftnFile
        "bent",  # BeftnEntry
        "bret",  # BeftnReturn
        "rtgsm",  # RtgsMessage
        "cauth",  # CardAuthRecord
        "rdict",  # RailFieldDictionary
    }
)


def make_connector_id(prefix: str, payload: dict) -> str:
    """Return ``<prefix>_<sha256_canonical(payload)[:24]>``.

    Accepts the spec/10 additive prefixes and, for convenience at the
    connector boundary, delegates to the frozen platform ``make_id`` for any
    prefix already in the spec/00 §3 table (``cres``, ``ins``, ...), so both
    families share the single E12 canonical hash implementation.
    """
    if not isinstance(prefix, str):
        raise IdPrefixError(f"prefix must be str, got {type(prefix).__name__}")
    if prefix in PREFIXES:
        return make_id(prefix, payload)
    if prefix not in CONNECTOR_PREFIXES:
        raise IdPrefixError(
            f"unknown id prefix {prefix!r}; allowed are spec 00 §3 plus spec 10 additive prefixes"
        )
    if not isinstance(payload, dict):
        raise TypeError(f"payload must be dict, got {type(payload).__name__}")
    return f"{prefix}_{sha256_canonical(payload)[:HASH_LENGTH]}"

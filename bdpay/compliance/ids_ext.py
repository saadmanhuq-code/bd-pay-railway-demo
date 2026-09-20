"""Compliance-local ID prefixes — format-parity shim over the platform scheme.

spec/06 and spec/07 mint IDs whose prefixes are not in the frozen
``bdpay.platform.ids.PREFIXES`` table (spec/00 §3): per-screen audit rows
(``scrn``), sanctions list versions (``slist``), list entries (``sent``),
PEP records (``pep``), whitelist rows (``wl``), rescreen runs (``rrun``),
monitoring rule events (``mrev``), and risk-score rows (``cscore``/``mscore``).

This is the same situation SPEC_ERRATA E18 resolved for spec/10: the additive
prefixes are registered here with exact format parity —
``<prefix>_<sha256_canonical(payload)[:24]>`` — so the shim collapses into
``bdpay.platform.ids.PREFIXES`` at integration without changing any stored ID.
See SPEC_ERRATA-LANE-A-compliance.md (E-C1) and the pinning test
``tests/compliance/test_ids_ext.py``.
"""

from __future__ import annotations

from bdpay.platform.canonical import sha256_canonical
from bdpay.platform.ids import HASH_LENGTH, PREFIXES, IdPrefixError, make_id

__all__ = ["COMPLIANCE_PREFIXES", "make_compliance_id"]

#: Additive prefixes registered by spec/06 + spec/07 (Entities / Data model).
COMPLIANCE_PREFIXES: frozenset[str] = frozenset(
    {
        "scrn",  # SanctionsScreening (spec/07 sanctions_screenings)
        "slist",  # SanctionsListVersion (spec/07)
        "sent",  # SanctionsListEntry (spec/07)
        "pep",  # PepRecord (spec/07)
        "wl",  # ScreeningWhitelistEntry (spec/07)
        "rrun",  # RescreenRun (spec/07)
        "mrev",  # MonitoringRuleEvent (spec/06 monitoring_rule_events)
        "cscore",  # customer risk score row (spec/06 customer_risk_scores)
        "mscore",  # merchant risk score row (spec/06 merchant_risk_scores)
        "dxp",  # DossierExport (spec/16 dossier_exports, E18-class additive prefix)
    }
)


def make_compliance_id(prefix: str, payload: dict) -> str:
    """``make_id`` with the compliance-spec additive prefixes accepted.

    Canonical prefixes delegate to the platform implementation unchanged;
    compliance-local prefixes use the identical content-address form.
    """
    if not isinstance(prefix, str):
        raise IdPrefixError(f"prefix must be str, got {type(prefix).__name__}")
    if prefix in PREFIXES:
        return make_id(prefix, payload)
    if prefix not in COMPLIANCE_PREFIXES:
        raise IdPrefixError(
            f"unknown id prefix {prefix!r}; allowed: spec/00 §3 table + "
            f"compliance additive prefixes {sorted(COMPLIANCE_PREFIXES)}"
        )
    if not isinstance(payload, dict):
        raise TypeError(f"payload must be dict, got {type(payload).__name__}")
    return f"{prefix}_{sha256_canonical(payload)[:HASH_LENGTH]}"

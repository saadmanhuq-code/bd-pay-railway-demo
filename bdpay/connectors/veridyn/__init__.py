"""spec/12 §H compliance sidecar connector (``veridyn_p2_compliance_v1``).

Advisory/evidence tier ONLY — the synchronous payment path can never reach
this package: no ``PaymentConnector`` surface, no payment capability in the
registration, no runner adapter entry, and CERT-V1 pins that kernel/ledger/
compliance-rules modules never import it.
"""

from bdpay.connectors.veridyn.ids import VERIDYN_PREFIXES, make_veridyn_id
from bdpay.connectors.veridyn.p2 import (
    CONNECTOR_ID,
    VeridynCallerError,
    VeridynCheckRow,
    VeridynFactBundleError,
    VeridynP2ComplianceConnector,
    VeridynTierError,
    VeridynUnavailableError,
    check_row_summary,
    usable_as_evidence,
)
from bdpay.connectors.veridyn.registration import (
    FIREWALL_CAPABILITIES,
    VERIDYN_PROTOCOL,
    VeridynFirewallError,
    assert_advisory_firewall,
    cert_v1_import_violations,
    record_health_sample,
    register_veridyn_p2,
)

__all__ = [
    "CONNECTOR_ID",
    "FIREWALL_CAPABILITIES",
    "VERIDYN_PREFIXES",
    "VERIDYN_PROTOCOL",
    "VeridynCallerError",
    "VeridynCheckRow",
    "VeridynFactBundleError",
    "VeridynFirewallError",
    "VeridynP2ComplianceConnector",
    "VeridynTierError",
    "VeridynUnavailableError",
    "assert_advisory_firewall",
    "cert_v1_import_violations",
    "check_row_summary",
    "make_veridyn_id",
    "record_health_sample",
    "register_veridyn_p2",
    "usable_as_evidence",
]

"""Deterministic per-rail simulators (spec/11 §F) + MFS/identity hosts (spec/12 §K).

Each simulator is a fake RAIL HOST, not a fake adapter: it speaks the rail's
real wire format (packed ISO 8583 / fixed-width BEFTN files / ISO 20022 XML /
acquirer REST dicts / MFS PGW JSON+RSA surfaces / payShield host-command
frames), so the production adapter code runs unmodified in SIMULATOR mode
with only the transport swapped. No randomness anywhere; all branching
derives from explicit scenario hints or sha256 buckets; time comes from the
injected Clock.
"""

from __future__ import annotations

from bdpay.connectors.simulators.identity_goaml_sim import (
    ScriptedGoamlPortal,
    SimulatedGoamlPortal,
)
from bdpay.connectors.simulators.identity_hsm_sim import (
    ScriptedPayshieldHost,
    SoftHsmSession,
)
from bdpay.connectors.simulators.identity_porichoy_sim import PorichoySimulatorGateway
from bdpay.connectors.simulators.identity_sanctions_sim import (
    SanctionsFeedSimulatorSource,
    build_un_consolidated_xml,
    fixture_entries,
)
from bdpay.connectors.simulators.mfs_bkash_sim import (
    BkashSimulatorGateway,
    build_bkash_spec12_scenarios,
    generate_rsa_keypair_pem,
)
from bdpay.connectors.simulators.mfs_nagad_sim import NagadSimulatorGateway
from bdpay.connectors.simulators.mfs_rocket_sim import (
    DownAggregatorTransport,
    RocketAggregatorSimulatorGateway,
)
from bdpay.connectors.simulators.mfs_sms_sim import (
    DeterministicSmsGateway,
    SmsAggregatorSimulatorHost,
)
from bdpay.connectors.simulators.veridyn_p2_sim import (
    SimulatedP2Endpoint,
    SimulatedP2Sidecar,
    build_signed_envelope_response,
)

__all__ = [
    "BkashSimulatorGateway",
    "DeterministicSmsGateway",
    "DownAggregatorTransport",
    "NagadSimulatorGateway",
    "PorichoySimulatorGateway",
    "RocketAggregatorSimulatorGateway",
    "SanctionsFeedSimulatorSource",
    "ScriptedGoamlPortal",
    "ScriptedPayshieldHost",
    "SimulatedGoamlPortal",
    "SimulatedP2Endpoint",
    "SimulatedP2Sidecar",
    "SmsAggregatorSimulatorHost",
    "SoftHsmSession",
    "build_bkash_spec12_scenarios",
    "build_signed_envelope_response",
    "build_un_consolidated_xml",
    "fixture_entries",
    "generate_rsa_keypair_pem",
]

# The rail-host simulators (NPSB / BEFTN / RTGS / card) belong to the rails
# group, which is mid-build in a concurrent session on this branch: its
# checkpoint imports modules it has not pushed yet. Guarded imports keep this
# package importable for the MFS/identity group now and export the rails
# simulators verbatim the moment their build lands; nothing here changes
# rails-group behavior.
try:
    from bdpay.connectors.simulators.beftn_sim import BeftnSimulatorBank
    from bdpay.connectors.simulators.card_sim import CardSimulatorAcquirer, SimVaultProxy
    from bdpay.connectors.simulators.npsb_sim import NpsbSimulatorRail
    from bdpay.connectors.simulators.rtgs_sim import RtgsSimulatorRail
except ImportError:  # rails-group modules not yet complete on this branch
    _RAIL_SIMS_AVAILABLE = False
else:
    _RAIL_SIMS_AVAILABLE = True
    __all__ += [
        "BeftnSimulatorBank",
        "CardSimulatorAcquirer",
        "NpsbSimulatorRail",
        "RtgsSimulatorRail",
        "SimVaultProxy",
    ]

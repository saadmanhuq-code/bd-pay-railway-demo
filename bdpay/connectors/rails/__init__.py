"""Rail connectors (spec/11): NPSB, BEFTN, RTGS, card acquiring.

Four adapters against the frozen SDK (spec/00 §10): ``npsb_iso8583_v28``
(PaymentConnector + ConnectorWebhookHandler), ``beftn_batch_v2``
(SettlementFileConnector), ``rtgs_iso20022_v1`` (PaymentConnector),
``card_acquirer_v1`` (PaymentConnector + ConnectorWebhookHandler). Every
adapter is mode-agnostic: the wire behavior lives behind a transport port and
the SIMULATOR / SANDBOX / PRODUCTION modes differ ONLY in which transport the
factory wires in (deterministic in-process rail vs. real wire code).
Connectors never write business DB tables, never import bdpay.kernel/ledger,
always echo instruction_id + connector_ref, always archive raw responses via
``sha256_canonical`` (errata E12).
"""

from __future__ import annotations

from bdpay.connectors.rails.registration import (
    RAIL_CONNECTOR_IDS,
    rail_registration_config,
    register_rail_connectors,
)

__all__ = [
    "RAIL_CONNECTOR_IDS",
    "rail_registration_config",
    "register_rail_connectors",
]

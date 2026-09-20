"""Registry registration documents for the four rail connectors (spec/11 §A).

Centralizes the spec-facing registration facts — connector id, protocol,
capabilities, supported methods — so wiring code registers the rails with one
call. Timeout/retry budgets are NOT repeated here: ``ConnectorRegistry``
already pins the spec/10 budget defaults per connector id (``DEFAULT_BUDGETS``)
and ``register`` falls back to them when no override is supplied.
"""

from __future__ import annotations

from bdpay.connectors.registry import (
    ConnectorMode,
    ConnectorRegistration,
    ConnectorRegistry,
)

__all__ = [
    "RAIL_CONNECTOR_IDS",
    "rail_registration_config",
    "register_rail_connectors",
]

RAIL_CONNECTOR_IDS: tuple[str, ...] = (
    "npsb_iso8583_v28",
    "beftn_batch_v2",
    "rtgs_iso20022_v1",
    "card_acquirer_v1",
)

_RAIL_CONFIGS: dict[str, dict] = {
    "npsb_iso8583_v28": {
        "display_name": "NPSB IBFT (ISO 8583 v28)",
        "protocol": "PAYMENT",
        "capabilities": (
            "submit",
            "query_status",
            "reverse",
            "health_check",
            "webhook",
        ),
        "supported_methods": ("NPSB_IBFT", "BANGLA_QR"),
    },
    "beftn_batch_v2": {
        "display_name": "BEFTN batch (sponsor-bank SFTP v2)",
        "protocol": "SETTLEMENT_FILE",
        "capabilities": ("batch", "recon_file", "health_check"),
        "supported_methods": (),
    },
    "rtgs_iso20022_v1": {
        "display_name": "BD-RTGS (ISO 20022 v1)",
        "protocol": "PAYMENT",
        "capabilities": (
            "submit",
            "query_status",
            "reverse",
            "health_check",
            "webhook",
        ),
        "supported_methods": ("RTGS_CREDIT",),
    },
    "card_acquirer_v1": {
        "display_name": "Card acquiring host v1",
        "protocol": "PAYMENT",
        "capabilities": (
            "submit",
            "query_status",
            "reverse",
            "health_check",
            "webhook",
        ),
        "supported_methods": ("CARD",),
    },
}


def rail_registration_config(connector_id: str) -> dict:
    """The registration document for one rail connector id (a fresh copy)."""
    try:
        config = _RAIL_CONFIGS[connector_id]
    except KeyError:
        raise ValueError(f"{connector_id!r} is not a rail connector id") from None
    return {
        "display_name": config["display_name"],
        "protocol": config["protocol"],
        "capabilities": tuple(config["capabilities"]),
        "supported_methods": tuple(config["supported_methods"]),
    }


def register_rail_connectors(
    registry: ConnectorRegistry,
    *,
    mode: ConnectorMode = ConnectorMode.SIMULATOR,
) -> dict[str, ConnectorRegistration]:
    """Register all four rails in ``registry``; budgets come from spec/10 defaults."""
    registered: dict[str, ConnectorRegistration] = {}
    for connector_id in RAIL_CONNECTOR_IDS:
        config = rail_registration_config(connector_id)
        registered[connector_id] = registry.register(
            connector_id,
            display_name=config["display_name"],
            protocol=config["protocol"],
            capabilities=config["capabilities"],
            supported_methods=config["supported_methods"],
            active_mode=mode,
        )
    return registered

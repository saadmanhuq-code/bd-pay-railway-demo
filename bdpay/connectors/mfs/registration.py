"""Registry registration for the spec/12 build group (8 connectors).

The lane-B brief assigns this group ``bkash_pgw_v2``, ``nagad_pgw_v33``,
``rocket_aggregator_v1``, ``porichoy_ekyc_v1``, ``goaml_reporter_v1``,
``sanctions_feed_v1``, ``hsm_thales_v1`` and ``sms_otp_v1`` (spec/12 also
lists ``upay_rest_v1`` / ``email_smtp_v1`` / ``veridyn_p2_compliance_v1`` —
those are outside this group's brief scope). Budgets come from the binding
spec/10 table already encoded in ``bdpay.connectors.registry.DEFAULT_BUDGETS``.
"""

from __future__ import annotations

from bdpay.connectors.registry import ConnectorRegistration, ConnectorRegistry

__all__ = ["SPEC12_CONNECTOR_SPECS", "register_spec12_connectors"]

#: connector_id -> (display_name, protocol, capabilities, supported_methods)
SPEC12_CONNECTOR_SPECS: dict[str, tuple[str, str, tuple[str, ...], tuple[str, ...]]] = {
    "bkash_pgw_v2": (
        "bKash Tokenized Checkout PGW",
        "PAYMENT",
        ("submit", "query_status", "reverse", "health_check", "webhook"),
        ("BKASH",),
    ),
    "nagad_pgw_v33": (
        "Nagad PGW API v3.3",
        "PAYMENT",
        ("submit", "query_status", "reverse", "health_check", "webhook"),
        ("NAGAD",),
    ),
    "rocket_aggregator_v1": (
        "Rocket via licensed PSO aggregator",
        "PAYMENT",
        ("submit", "query_status", "reverse", "health_check", "webhook"),
        ("ROCKET",),
    ),
    "porichoy_ekyc_v1": (
        "Porichoy NID gateway",
        "IDENTITY",
        ("verify_nid", "health_check"),
        (),
    ),
    "goaml_reporter_v1": (
        "BFIU goAML portal reporter",
        "AML_FILING",
        ("file_str", "file_ctr"),
        (),
    ),
    "sanctions_feed_v1": (
        "UN Consolidated + BFIU domestic sanction feeds",
        "SANCTIONS",
        ("screen", "feed_ingest"),
        (),
    ),
    "hsm_thales_v1": (
        "payShield-class HSM boundary",
        "HSM",
        ("generate_mac", "verify_mac", "translate_pin_block", "zmk_transport", "health_check"),
        (),
    ),
    "sms_otp_v1": (
        "SMS aggregator notification delivery",
        "NOTIFICATION",
        ("send", "dlr_webhook", "health_check"),
        (),
    ),
}


def register_spec12_connectors(
    registry: ConnectorRegistry, config_by_id: dict[str, dict]
) -> list[ConnectorRegistration]:
    """Register the eight spec/12 connectors from deployment config.

    ``config_by_id`` maps connector_id -> config document with the mandatory
    ``mode`` key (``simulator | sandbox | live | disabled | manual_local``);
    protocol/capabilities default from the binding catalogue above.
    """
    registrations: list[ConnectorRegistration] = []
    for connector_id, (display, protocol, capabilities, methods) in sorted(
        SPEC12_CONNECTOR_SPECS.items()
    ):
        config = dict(config_by_id.get(connector_id, {}))
        config.setdefault("display_name", display)
        config.setdefault("protocol", protocol)
        config.setdefault("capabilities", capabilities)
        config.setdefault("supported_methods", methods)
        registrations.append(registry.register_from_config(connector_id, config))
    return registrations

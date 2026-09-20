"""Registration + the BINDING advisory firewall for ``veridyn_p2_compliance_v1``.

spec/12 §H tiering rule, enforced structurally (never as policy):

1. **Registry capability level** — the registration's protocol is
   ``COMPLIANCE_SIDECAR``; its capability set is exactly
   ``("governance_check", "health_check")``; ``supported_methods`` is empty.
   :func:`register_veridyn_p2` refuses fail-closed any config document that
   tries to grant a payment capability, a payment method, or a payment
   protocol — and :func:`assert_advisory_firewall` re-checks the invariant on
   every boot/sweep, so a drifted registry row is caught, not served.
2. **Dispatch level** — the adapter is NEVER entered into a
   ``ConnectorRunner`` adapter table (the kernel's only path to a connector).
   Health flows through :func:`record_health_sample` instead, which probes
   the adapter directly and appends the ``chlth`` sample via
   ``ConnectorRegistry.record_health`` — telemetry without dispatchability.
3. **Import level (CERT-V1)** — :func:`cert_v1_import_violations` AST-scans
   the kernel/ledger/compliance packages for any import of
   ``bdpay.connectors.veridyn``; per spec/12 §H only ``compliance.case_support``
   and ops modules may consume the adapter, so those are the sole exemptions.
"""

from __future__ import annotations

import ast
from pathlib import Path

from bdpay.connectors.registry import (
    ConnectorMode,
    ConnectorRegistration,
    ConnectorRegistry,
    mode_from_config,
)
from bdpay.connectors.veridyn.p2 import CONNECTOR_ID
from bdpay.platform.clock import Clock
from bdpay.platform.errors import ConflictError, InvalidRequestError

__all__ = [
    "FIREWALL_CAPABILITIES",
    "PAYMENT_CAPABILITIES",
    "VERIDYN_PROTOCOL",
    "VeridynFirewallError",
    "assert_advisory_firewall",
    "cert_v1_import_violations",
    "record_health_sample",
    "register_veridyn_p2",
]

VERIDYN_PROTOCOL = "COMPLIANCE_SIDECAR"

#: The ONLY capabilities this connector may ever carry (spec/12 §H surface).
FIREWALL_CAPABILITIES: tuple[str, ...] = ("governance_check", "health_check")

#: Capability names that put a connector on the kernel's payment path.
PAYMENT_CAPABILITIES: frozenset[str] = frozenset(
    {"submit", "query_status", "reverse", "webhook", "manual_result"}
)

#: Default credential refs swept by the registry's fail-closed credential
#: sweep in the credentialed modes. Names only — never values.
_DEFAULT_CREDENTIAL_REFS = ["env:VERIDYN_P2_URL", "env:VERIDYN_P2_CLIENT_TOKEN"]

#: CERT-V1 scan roots: the packages that must never import this adapter.
_CERT_V1_SCAN_PACKAGES = ("kernel", "ledger", "compliance")

#: spec/12 §H: the adapter is importable only from compliance.case_support
#: (and ops modules, which live outside ``bdpay``).
_CERT_V1_EXEMPT_MODULE = "case_support"

_VERIDYN_IMPORT_ROOT = "bdpay.connectors.veridyn"


class VeridynFirewallError(ConflictError):
    """The advisory/evidence firewall would be breached (refused fail-closed)."""

    default_code = "veridyn_payment_path_forbidden"


def assert_advisory_firewall(registration: ConnectorRegistration) -> None:
    """Re-checkable invariant: the registration cannot reach the payment path."""
    if registration.connector_id != CONNECTOR_ID:
        raise VeridynFirewallError(
            f"firewall assertion is for {CONNECTOR_ID}, got {registration.connector_id!r}"
        )
    if registration.protocol != VERIDYN_PROTOCOL:
        raise VeridynFirewallError(
            f"protocol must be {VERIDYN_PROTOCOL}; {registration.protocol!r} could "
            "place the sidecar on a payment surface"
        )
    granted_payment = PAYMENT_CAPABILITIES.intersection(registration.capabilities)
    if granted_payment:
        raise VeridynFirewallError(
            "payment capabilities are forbidden for the advisory sidecar: "
            + ", ".join(sorted(granted_payment))
        )
    if registration.supported_methods:
        raise VeridynFirewallError(
            "supported_methods must be empty; a method binding would make the "
            "sidecar routable by the kernel"
        )


def register_veridyn_p2(
    registry: ConnectorRegistry, config: dict
) -> ConnectorRegistration:
    """Register the connector from deployment config; ``mode`` is mandatory.

    Mode strings are the deployment vocabulary (``simulator | sandbox | live
    | disabled``). ``manual_local`` is refused — an advisory sidecar has no
    manual contingency (spec/12 §H failure posture: outage means the case
    workbench shows "advisory unavailable", never an operator-recorded
    verdict). Any attempt to grant payment capabilities/methods/protocol is
    refused BEFORE the registry row exists.
    """
    if "mode" not in config:
        raise InvalidRequestError(
            f"connector config for {CONNECTOR_ID} is missing 'mode'",
            code="invalid_connector_mode",
        )
    mode = mode_from_config(config["mode"])
    if mode is ConnectorMode.MANUAL_LOCAL:
        raise VeridynFirewallError(
            "manual_local is not a legal mode for the advisory sidecar",
        )
    requested_capabilities = tuple(config.get("capabilities", FIREWALL_CAPABILITIES))
    if set(requested_capabilities) - set(FIREWALL_CAPABILITIES):
        raise VeridynFirewallError(
            "config tried to grant capabilities outside the sidecar surface: "
            + ", ".join(sorted(set(requested_capabilities) - set(FIREWALL_CAPABILITIES)))
        )
    if tuple(config.get("supported_methods", ())):
        raise VeridynFirewallError(
            "config tried to bind payment methods to the advisory sidecar"
        )
    if str(config.get("protocol", VERIDYN_PROTOCOL)) != VERIDYN_PROTOCOL:
        raise VeridynFirewallError(
            f"config tried to register the sidecar under protocol "
            f"{config.get('protocol')!r}"
        )
    inner_config = dict(config.get("config") or {})
    inner_config.setdefault(
        "credential_refs_by_mode",
        {
            ConnectorMode.SANDBOX.value: list(_DEFAULT_CREDENTIAL_REFS),
            ConnectorMode.PRODUCTION.value: list(_DEFAULT_CREDENTIAL_REFS),
        },
    )
    registration = registry.register(
        CONNECTOR_ID,
        display_name=str(config.get("display_name", "Veridyn P2 compliance sidecar")),
        protocol=VERIDYN_PROTOCOL,
        capabilities=FIREWALL_CAPABILITIES,
        supported_methods=(),
        adapter_version=str(config.get("adapter_version", "1.0.0")),
        active_mode=mode,
        config=inner_config,
        config_schema=config.get("config_schema"),
        timeout_ms=config.get("timeout_ms"),
        retry_budget=config.get("retry_budget"),
    )
    assert_advisory_firewall(registration)
    return registration


async def record_health_sample(
    registry: ConnectorRegistry, adapter, *, clock: Clock
) -> bool:
    """One health sweep step OUTSIDE the runner (chlth wiring, spec/10 shape).

    The adapter is probed directly and the sample lands through
    ``ConnectorRegistry.record_health`` (which mints the ``chlth`` sample row
    and emits ``connector_registration.health_changed`` on a delta). The
    adapter is deliberately never placed in a runner adapter table, so the
    kernel's dispatch path has nothing to find.
    """
    registration = registry.get(CONNECTOR_ID)
    assert_advisory_firewall(registration)
    if registration.active_mode is ConnectorMode.DISABLED or not registration.enabled:
        registry.record_health(
            CONNECTOR_ID, healthy=False, detail_code="connector_disabled"
        )
        return False
    started = clock.now()
    detail_code: str | None = None
    try:
        healthy = bool(await adapter.health_check())
        if not healthy:
            detail_code = "unhealthy_response"
    except Exception:
        healthy = False
        detail_code = "transport_error"
    latency_ms = int((clock.now() - started).total_seconds() * 1000)
    registry.record_health(
        CONNECTOR_ID, healthy=healthy, latency_ms=latency_ms, detail_code=detail_code
    )
    return healthy


def cert_v1_import_violations(bdpay_root: Path | None = None) -> list[str]:
    """CERT-V1: modules in kernel/ledger/compliance importing the adapter.

    Returns ``<relative path>: <imported name>`` strings; an empty list is
    the PASS verdict. ``compliance/case_support*`` is the single permitted
    importer (spec/12 §H); everything else in the scanned packages — the
    kernel preflight/orchestrator above all — must have no route to this
    adapter at import level.
    """
    root = (
        bdpay_root
        if bdpay_root is not None
        else Path(__file__).resolve().parents[2]
    )
    violations: list[str] = []
    for package in _CERT_V1_SCAN_PACKAGES:
        base = root / package
        if not base.exists():
            continue
        for path in sorted(base.rglob("*.py")):
            if _CERT_V1_EXEMPT_MODULE in path.stem:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                names: list[str] = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module]
                for name in names:
                    if name == _VERIDYN_IMPORT_ROOT or name.startswith(
                        _VERIDYN_IMPORT_ROOT + "."
                    ):
                        violations.append(f"{path.relative_to(root)}: {name}")
    return violations

"""SIMULATOR certification environments for the spec/12 payment adapters.

Mirrors ``bdpay.connectors.certification.build_simulator_environment`` but the
adapter under test is the REAL spec/12 adapter in SIMULATOR mode (CERT-03/09
AST-scan the adapter's own module) and the webhook handler is the adapter's
query-confirmed IPN handler — so certification exercises this group's code,
not the generic simulator wrapper. The non-payment connectors (identity /
AML filing / sanctions / HSM / notification) have protocol-shaped certification
checks pinned in their test suites instead (SPEC_ERRATA-LANE-B LB9 — the
spec/10 harness is PaymentInstruction-shaped by construction).
"""

from __future__ import annotations

from datetime import datetime

from bdpay.connectors.breaker import CircuitBreaker
from bdpay.connectors.certification import SimulatorCertEnvironment
from bdpay.connectors.mfs.bkash import BkashIpnWebhookHandler, BkashPgwConnector
from bdpay.connectors.mfs.ipn import QueryConfirmedIpnHandler
from bdpay.connectors.mfs.nagad import NagadPgwConnector
from bdpay.connectors.mfs.rocket import RocketAggregatorConnector
from bdpay.connectors.ports import AcceptAllKernelHandoff, InMemoryObjectStore
from bdpay.connectors.registry import ConnectorMode, ConnectorRegistry
from bdpay.connectors.runner import ConnectorRunner
from bdpay.connectors.scenarios import build_baseline_scenarios, build_extended_scenarios
from bdpay.connectors.simulator import ScenarioEngine
from bdpay.connectors.stores import InMemoryConnectorResultStore
from bdpay.connectors.webhooks import WebhookPipeline
from bdpay.platform.clock import SteppingClock

__all__ = ["MFS_PAYMENT_ADAPTERS", "build_mfs_simulator_environment"]

MFS_PAYMENT_ADAPTERS = {
    "bkash_pgw_v2": (BkashPgwConnector, ("BKASH",)),
    "nagad_pgw_v33": (NagadPgwConnector, ("NAGAD",)),
    "rocket_aggregator_v1": (RocketAggregatorConnector, ("ROCKET",)),
}


async def _instant_sleep(_seconds: float) -> None:
    return None


def build_mfs_simulator_environment(
    connector_id: str,
    *,
    start: datetime,
    timeout_ms: int = 150,
    signing_key: bytes = b"sim-cert-verify-key",
    adapter_version: str = "1.0.0",
) -> SimulatorCertEnvironment:
    """Deterministic SIMULATOR bench with the real spec/12 adapter under test."""
    adapter_cls, methods = MFS_PAYMENT_ADAPTERS[connector_id]
    clock = SteppingClock(start)
    object_store = InMemoryObjectStore()
    result_store = InMemoryConnectorResultStore()
    scenarios = build_baseline_scenarios(connector_id, created_at=clock.now())
    scenarios += build_extended_scenarios(connector_id, created_at=clock.now())
    engine = ScenarioEngine(
        connector_id,
        scenarios,
        clock=clock,
        signing_key=signing_key,
        object_store=object_store,
        sleep=_instant_sleep,
    )
    adapter = adapter_cls(
        mode=ConnectorMode.SIMULATOR,
        clock=clock,
        engine=engine,
        object_store=object_store,
    )
    registry = ConnectorRegistry(clock=clock)
    registry.register(
        connector_id,
        display_name=f"{connector_id} (spec/12 simulator cert)",
        protocol="PAYMENT",
        capabilities=("submit", "query_status", "reverse", "health_check", "webhook"),
        supported_methods=methods,
        adapter_version=adapter_version,
        active_mode=ConnectorMode.SIMULATOR,
        timeout_ms=timeout_ms,
        retry_budget={"submit_retries": 0, "poll_attempts": 3, "poll_interval_ms": 5_000},
    )
    breaker = CircuitBreaker(clock=clock)
    runner = ConnectorRunner(
        registry=registry,
        breaker=breaker,
        clock=clock,
        result_store=result_store,
        object_store=object_store,
        sleep=_instant_sleep,
    )
    runner.register_adapter(connector_id, ConnectorMode.SIMULATOR, adapter)
    kernel = AcceptAllKernelHandoff()
    if connector_id == "bkash_pgw_v2":
        handler = BkashIpnWebhookHandler(
            signing_key, clock=clock, truth_for=adapter.truth_for
        )
    else:
        handler = QueryConfirmedIpnHandler(
            connector_id, signing_key, clock=clock, truth_for=adapter.truth_for
        )
    pipeline = WebhookPipeline(
        clock=clock,
        kernel=kernel,
        handlers={connector_id: handler},
        result_store=result_store,
        object_store=object_store,
    )
    engine.bind_callback_sink(pipeline.ingest)
    return SimulatorCertEnvironment(
        connector_id=connector_id,
        clock=clock,
        registry=registry,
        breaker=breaker,
        runner=runner,
        engine=engine,
        adapter=adapter,
        pipeline=pipeline,
        handler=handler,
        kernel=kernel,
        object_store=object_store,
        result_store=result_store,
        signing_key=signing_key,
    )

"""Gated gateway composition for CERT WebhookPipeline on inbound MFS IPNs.

Default production path remains verify + webhook_sink -> connector_inbound_events.
When Settings.gateway_webhook_pipeline is True, POST /v1/webhooks/{id} runs through
WebhookPipeline against the runner's webhook_inbound_store (DUPLICATE-terminal,
persist-before-verify fail-closed) and dual-writes accepted results into the
existing webhook_sink so the money-path drain is not half-broken.

The gate defaults OFF. Enabling it changes HTTP oracle posture for bad
signatures / parse failures to the CERT constant {"received": true} (no 401/400
oracle) while still refusing delivery to the sink.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import TYPE_CHECKING

from bdpay.connectors.stores import InMemoryWebhookInboundStore
from bdpay.connectors.webhooks import WebhookPipeline

if TYPE_CHECKING:
    from bdpay.connectors.runner import ConnectorRunner
    from bdpay.connectors.sdk import ConnectorResult
    from bdpay.gateway.ports import WebhookHandlerRegistry, WebhookSinkPort
    from bdpay.platform.clock import Clock

__all__ = [
    "WebhookSinkKernelHandoff",
    "bind_webhook_connector_id",
    "build_gateway_webhook_pipeline",
    "map_connector_mode_at_call",
]

_connector_id_ctx: ContextVar[str | None] = ContextVar(
    "bdpay_gateway_webhook_pipeline_connector_id", default=None
)


def bind_webhook_connector_id(connector_id: str | None):
    """Bind connector_id for the current request's sink dual-write handoff."""
    return _connector_id_ctx.set(connector_id)


def map_connector_mode_at_call(connector_mode: str) -> str:
    """Map Settings.connector_mode to WebhookPipeline mode_at_call labels."""
    normalized = (connector_mode or "simulator").strip().lower()
    if normalized == "live":
        return "PRODUCTION"
    if normalized == "sandbox":
        return "SANDBOX"
    return "SIMULATOR"


class WebhookSinkKernelHandoff:
    """KernelHandoff that dual-writes accepted pipeline results to webhook_sink.

    Preserves the connector_inbound_events drain path while the pipeline owns
    webhook_inbound durability / DUPLICATE-terminal / fail-closed verify.
    """

    def __init__(self, sink: WebhookSinkPort) -> None:
        self._sink = sink

    async def handle_connector_result(self, result: ConnectorResult) -> bool:
        connector_id = _connector_id_ctx.get()
        if connector_id is None:
            # Refuse handoff rather than invent a connector_id (fail-closed).
            return False
        self._sink.deliver(connector_id, result)
        return True


def build_gateway_webhook_pipeline(
    *,
    clock: Clock,
    webhook_registry: WebhookHandlerRegistry,
    webhook_sink: WebhookSinkPort,
    connector_runner: ConnectorRunner,
    connector_mode: str,
) -> tuple[WebhookPipeline, WebhookSinkKernelHandoff]:
    """Compose CERT WebhookPipeline onto the runner's durable inbound store."""
    inbound = getattr(connector_runner, "webhook_inbound_store", None)
    if inbound is None:
        inbound = InMemoryWebhookInboundStore()
        # Make the store observable for wiring asserts / shared-backing tests.
        connector_runner.webhook_inbound_store = inbound  # noqa: SLF001

    handlers = {
        connector_id: handler
        for connector_id in webhook_registry.connector_ids()
        if (handler := webhook_registry.get(connector_id)) is not None
    }
    handoff = WebhookSinkKernelHandoff(webhook_sink)
    pipeline = WebhookPipeline(
        clock=clock,
        kernel=handoff,
        handlers=handlers,
        inbound_store=inbound,
        result_store=connector_runner.result_store,
        mode_at_call=map_connector_mode_at_call(connector_mode),
    )
    return pipeline, handoff

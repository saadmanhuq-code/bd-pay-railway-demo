"""Gateway -> kernel ports + the inbound connector-webhook registry.

The gateway reaches ALL business logic through these Protocols (plus the
platform-owned ports in :mod:`bdpay.platform.interfaces` — imported, never
redefined). The kernel package provides the implementations at composition
time; gateway tests use recording fakes. Methods return plain JSON-shaped
mappings (the gateway is a thin edge: it validates, authorizes, meters, and
forwards) and raise the :mod:`bdpay.platform.errors` hierarchy for refusals.

List methods return ``(items, next_cursor)`` for the spec/00 §4 cursor
pagination contract. ``clock`` parameters follow the platform interface
convention: a port method that needs the current time takes the injected
clock — never a wall-clock read.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:  # annotation-only; no runtime coupling to other packages
    from bdpay.connectors.sdk import ConnectorResult, ConnectorWebhookHandler
    from bdpay.platform.clock import Clock

__all__ = [
    "CustomerOnboardingPort",
    "DossierExportPort",
    "MerchantOnboardingPort",
    "PaymentIntentPort",
    "QrPort",
    "RefundPort",
    "WebhookHandlerRegistry",
    "WebhookSinkPort",
]

Json = Mapping[str, object]
Page = tuple[Sequence[Json], "str | None"]


@runtime_checkable
class PaymentIntentPort(Protocol):
    """The kernel payment-orchestrator surface the gateway forwards to."""

    def create_intent(
        self, payload: Json, *, merchant_id: str, idempotency_key: str, clock: Clock
    ) -> Json: ...

    def get_intent(self, payment_intent_id: str) -> Json | None: ...

    def list_intents(
        self, *, merchant_id: str | None, filters: Json, limit: int, cursor: str | None
    ) -> Page: ...

    def list_intents_for_customer(
        self, *, customer_id: str, filters: Json, limit: int, cursor: str | None
    ) -> Page: ...

    def confirm_intent(
        self, payment_intent_id: str, payload: Json, *, actor: str, clock: Clock
    ) -> Json: ...

    def capture_intent(
        self, payment_intent_id: str, *, amount_minor: int, clock: Clock
    ) -> Json: ...

    def cancel_intent(
        self, payment_intent_id: str, *, cancellation_reason: str, clock: Clock
    ) -> Json: ...

    def list_attempts(
        self, payment_intent_id: str, *, limit: int, cursor: str | None
    ) -> Page: ...


@runtime_checkable
class RefundPort(Protocol):
    """Refund initiation and queries (kernel-owned lifecycle, spec/02)."""

    def create_refund(
        self, payload: Json, *, merchant_id: str | None, idempotency_key: str, clock: Clock
    ) -> Json: ...

    def get_refund(self, refund_id: str) -> Json | None: ...

    def list_refunds(
        self, *, merchant_id: str | None, filters: Json, limit: int, cursor: str | None
    ) -> Page: ...

    def list_refunds_for_customer(
        self, *, customer_id: str, filters: Json, limit: int, cursor: str | None
    ) -> Page: ...


@runtime_checkable
class MerchantOnboardingPort(Protocol):
    """Merchant KYB onboarding surface (spec/08 via kernel)."""

    def submit_application(self, payload: Json, *, idempotency_key: str, clock: Clock) -> Json: ...

    def get_merchant(self, merchant_id: str) -> Json | None: ...

    def list_merchants(self, *, filters: Json, limit: int, cursor: str | None) -> Page: ...

    def list_diner_merchants(self, *, filters: Json, limit: int, cursor: str | None) -> Page: ...


@runtime_checkable
class CustomerOnboardingPort(Protocol):
    """Customer eKYC submission surface (spec/09 via kernel).

    ``submit_customer`` receives the raw eKYC payload exactly once, in
    process; the gateway never logs or stores it (PII redaction applies to
    every log write on the way through).
    """

    def submit_customer(
        self, payload: Json, *, merchant_id: str | None, idempotency_key: str, clock: Clock
    ) -> Json: ...

    def get_customer(self, customer_id: str) -> Json | None: ...

    def get_balance(self, customer_id: str) -> Json | None: ...


@runtime_checkable
class DossierExportPort(Protocol):
    """Operator-only dossier-export surface (spec/16 §API A, compliance-owned).

    Implemented by ``bdpay.compliance.dossier.DossierExportService``; the
    gateway stays a thin edge — queue returns the (possibly pre-existing,
    content-addressed) record, ``download`` returns ``(zip_bytes, sha256_hex)``
    per the spec/15 deterministic file+hash contract and raises NotFound until
    the export is COMPLETED.
    """

    def queue(
        self, *, dossier_type: str, input_snapshot: Json, requested_by: str
    ) -> object: ...

    def get(self, dossier_export_id: str) -> object | None: ...

    def list(
        self, *, limit: int = 50, cursor: str | None = None
    ) -> tuple[Sequence[object], str | None]: ...

    def download(self, dossier_export_id: str) -> tuple[bytes, str]: ...


@runtime_checkable
class QrPort(Protocol):
    """The spec/13 Bangla QR surface (acquirer + issuer + operator).

    Implemented by ``bdpay.qr.service.QrService``. Result objects are the qr
    package's frozen dataclasses (StaticQrIssueResult, ResolveResult,
    MerchantQr, RenderAsset, ...); the gateway shapes the wire view.
    """

    def issue_static(
        self,
        merchant_id: str,
        *,
        label: str,
        store_id: str | None = None,
        terminal_id: str | None = None,
    ) -> object: ...

    def issue_dynamic(self, payment_intent_id: str, *, ttl_seconds: int) -> object: ...

    def get_merchant_qr(self, merchant_qr_id: str) -> object: ...

    def list_merchant_qrs(self, merchant_id: str) -> Sequence[object]: ...

    def render_asset(self, merchant_qr_id: str, kind: str) -> object: ...

    def suspend(self, merchant_qr_id: str, *, reason: str) -> object: ...

    def reactivate(self, merchant_qr_id: str) -> object: ...

    def revoke(self, merchant_qr_id: str, *, reason: str) -> object: ...

    def resolve(self, payload: str, *, customer_ref: str) -> object: ...

    def pay(self, resolution_id: str, *, amount_minor: int | None = None) -> object: ...

    def run_interop(self, *, counterparty_class: str | None = None) -> object: ...

    def list_interop_cases(self, counterparty_class: str | None) -> Sequence[object]: ...

    def dashboard(self) -> object: ...


@runtime_checkable
class WebhookSinkPort(Protocol):
    """Where a verified inbound connector event goes (kernel intake)."""

    def deliver(self, connector_id: str, result: ConnectorResult) -> None: ...


class WebhookHandlerRegistry:
    """Closed registry of :class:`ConnectorWebhookHandler` implementations.

    Fail-closed by construction: an unknown ``connector_id`` resolves to
    ``None`` and the route refuses; double registration is an error.
    """

    def __init__(self) -> None:
        self._handlers: dict[str, ConnectorWebhookHandler] = {}

    def register(self, handler: ConnectorWebhookHandler) -> None:
        connector_id = handler.connector_id
        if not connector_id:
            raise ValueError("handler.connector_id must be non-empty")
        if connector_id in self._handlers:
            raise ValueError(f"webhook handler {connector_id!r} already registered")
        self._handlers[connector_id] = handler

    def get(self, connector_id: str) -> ConnectorWebhookHandler | None:
        return self._handlers.get(connector_id)

    def connector_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._handlers))

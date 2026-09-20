"""Gateway event fan-out for resilient payment and QR notification audiences.

The month-one events are ALREADY registered in spec/02 Appendix A; this
consumer extends their audiences additively (no renames, no new types). The
QR paid binding is the spec/13 transaction-notification audience:

==========================  =============================================
``settlement_batch.held``    merchant webhooks (opt-in; per-merchant slice,
                             ``payout_expected_by: null``)
``settlement_batch.released``merchant webhooks (same fan-out; recomputed
                             ``payout_expected_by``)
``payment_intent.deferred``  merchant webhook + customer ``payment_deferred``
                             notification (``scheduled_for`` in Asia/Dhaka)
``refund.failed``            customer ``refund_failed`` notification (the
                             5-business-day operational resolution path)
``dispute.opened``           merchant webhook (opt-in) + customer
                             ``dispute_opened`` notification
``dispute.resolved``         merchant webhook (opt-in) + customer
                             ``dispute_resolved`` notification
``webhook.delivery_exhausted`` merchant EMAIL ``webhook_exhausted`` (the
                             webhook channel itself is dead)
``qr_payload.paid``          QR customer ``payment_succeeded`` SMS + merchant
                             ``payment_received`` SMS (spec/13 §E.3)
==========================  =============================================

Wiring: the composition root subscribes :meth:`ResilienceEventFanout.handlers`
on the in-process event bus; the outbox worker drains rows into the bus; this
consumer creates webhook deliveries (sent by
:class:`~bdpay.gateway.webhooks_out.WebhookDeliveryService.process_due`) and
enqueues notifications through the existing platform NotificationService.
Everything is idempotent on ``event_id`` (bus-level dedupe + content-addressed
notification idempotency keys + ``(webhook_id, event_id)`` delivery dedupe).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from bdpay.gateway.webhooks_out import WebhookDeliveryService
from bdpay.platform.notifications import NotificationService
from bdpay.platform.scheduler import DHAKA_TZ

__all__ = [
    "BatchMerchantSlice",
    "IntentNotificationContext",
    "IntentPartiesPort",
    "ResilienceEventFanout",
    "SettlementBatchReadPort",
]

_LOG = logging.getLogger("bdpay.gateway.event_fanout")

#: The closed set of event types this consumer handles.
FANOUT_EVENT_TYPES: tuple[str, ...] = (
    "settlement_batch.held",
    "settlement_batch.released",
    "payment_intent.deferred",
    "qr_payload.paid",
    "refund.failed",
    "dispute.opened",
    "dispute.resolved",
    "webhook.delivery_exhausted",
)


@dataclass(frozen=True)
class IntentNotificationContext:
    """Notification facts resolved from the payment-intent read model."""

    merchant_id: str
    customer_id: str | None
    amount_minor: int | None = None
    currency: str = "BDT"
    merchant_display_name: str | None = None


@runtime_checkable
class IntentPartiesPort(Protocol):
    """Resolve the merchant/customer behind a payment intent (kernel read)."""

    def intent_parties(self, payment_intent_id: str) -> tuple[str, str | None] | None:
        """``(merchant_id, customer_id_or_None)`` or ``None`` when unknown."""
        ...


class BatchMerchantSlice:
    """One merchant's slice of a settlement batch (spec/16 fan-out payload)."""

    __slots__ = ("merchant_id", "instruction_count", "net_payout_minor", "payout_expected_by")

    def __init__(
        self,
        merchant_id: str,
        instruction_count: int,
        net_payout_minor: int,
        payout_expected_by: datetime | None = None,
    ) -> None:
        if isinstance(net_payout_minor, bool) or not isinstance(net_payout_minor, int):
            raise ValueError("net_payout_minor must be int paisa")
        self.merchant_id = merchant_id
        self.instruction_count = int(instruction_count)
        self.net_payout_minor = net_payout_minor
        self.payout_expected_by = payout_expected_by


@runtime_checkable
class SettlementBatchReadPort(Protocol):
    """Per-merchant batch breakdown (join over settlement_instructions)."""

    def batch_breakdown(self, batch_id: str) -> list[BatchMerchantSlice]: ...


def _rfc3339(at: datetime) -> str:
    return at.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_rfc3339(raw: object) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _dhaka_display(at: datetime) -> str:
    """Asia/Dhaka rendering for customer messages (spec/16: Dhaka time)."""
    return at.astimezone(DHAKA_TZ).strftime("%Y-%m-%d %H:%M")


class ResilienceEventFanout:
    """The LR-5 consumer. One instance handles all six event types."""

    def __init__(
        self,
        *,
        webhooks: WebhookDeliveryService,
        notifications: NotificationService,
        intents: IntentPartiesPort,
        settlement: SettlementBatchReadPort | None = None,
        customer_language: str = "bn",
        logger: logging.Logger | None = None,
    ) -> None:
        self._webhooks = webhooks
        self._notifications = notifications
        self._intents = intents
        self._settlement = settlement
        self._customer_language = customer_language
        self._log = logger or _LOG

    # -- bus wiring -------------------------------------------------------------

    def handlers(self) -> dict[str, Callable[[Mapping[str, object]], None]]:
        """``{event_type: handler}`` for bus subscription."""
        return {
            "settlement_batch.held": self.on_settlement_batch_held,
            "settlement_batch.released": self.on_settlement_batch_released,
            "payment_intent.deferred": self.on_payment_intent_deferred,
            "qr_payload.paid": self.on_qr_payload_paid,
            "refund.failed": self.on_refund_failed,
            "dispute.opened": self.on_dispute_opened,
            "dispute.resolved": self.on_dispute_resolved,
            "webhook.delivery_exhausted": self.on_webhook_delivery_exhausted,
        }

    # -- helpers ------------------------------------------------------------------

    @staticmethod
    def _payload(envelope: Mapping[str, object]) -> Mapping[str, object]:
        payload = envelope.get("payload")
        return payload if isinstance(payload, Mapping) else {}

    @staticmethod
    def _event_id(envelope: Mapping[str, object]) -> str:
        return str(envelope.get("event_id", ""))

    @staticmethod
    def _payment_intent_id(payload: Mapping[str, object], envelope: Mapping[str, object]) -> str:
        return str(payload.get("payment_intent_id") or envelope.get("subject_id") or "")

    def _parties_from_payload(
        self, payload: Mapping[str, object], envelope: Mapping[str, object]
    ) -> tuple[str, str | None] | None:
        intent_id = self._payment_intent_id(payload, envelope)
        if not intent_id:
            return None
        context = self._intent_context(intent_id, payload)
        return None if context is None else (context.merchant_id, context.customer_id)

    def _intent_context(
        self, intent_id: str, payload: Mapping[str, object]
    ) -> IntentNotificationContext | None:
        getter = getattr(self._intents, "intent_notification_context", None)
        if callable(getter):
            context = getter(intent_id)
            if context is not None:
                return context
        parties = self._intents.intent_parties(intent_id)
        if parties is None:
            self._log.warning("fanout: unknown payment intent %s", intent_id)
            return None
        amount_raw = payload.get("amount_minor")
        amount_minor = (
            amount_raw
            if isinstance(amount_raw, int) and not isinstance(amount_raw, bool) and amount_raw > 0
            else None
        )
        return IntentNotificationContext(
            merchant_id=parties[0],
            customer_id=parties[1],
            amount_minor=amount_minor,
        )

    @staticmethod
    def _amount_bdt(amount_minor: int) -> str:
        return f"{amount_minor // 100}.{amount_minor % 100:02d}"

    def _notify_customer(
        self,
        *,
        customer_id: str,
        template_id: str,
        template_vars: dict[str, str],
        event_id: str,
    ) -> None:
        self._notifications.dispatch(
            recipient_type="customer",
            recipient_id=customer_id,
            channel="sms",
            template_id=template_id,
            template_vars=template_vars,
            idempotency_key=f"{event_id}:{template_id}:{customer_id}",
            language=self._customer_language,
        )

    # -- settlement batch held / released (merchant webhooks, opt-in) -------------

    def _fan_out_batch(
        self,
        envelope: Mapping[str, object],
        *,
        include_expected_by: bool,
    ) -> None:
        if self._settlement is None:
            self._log.warning(
                "fanout: settlement read port not wired; %s not fanned out",
                envelope.get("type"),
            )
            return
        payload = self._payload(envelope)
        batch_id = str(payload.get("batch_id") or envelope.get("subject_id") or "")
        rail = str(payload.get("rail", ""))
        event_id = self._event_id(envelope)
        for batch_slice in self._settlement.batch_breakdown(batch_id):
            expected: str | None = None
            if include_expected_by and batch_slice.payout_expected_by is not None:
                expected = _rfc3339(batch_slice.payout_expected_by)
            self._webhooks.dispatch_event(
                merchant_id=batch_slice.merchant_id,
                event_id=event_id,
                event_type=str(envelope.get("type", "")),
                payload={
                    "batch_id": batch_id,
                    "rail": rail,
                    "instruction_count": batch_slice.instruction_count,
                    "net_payout_minor": batch_slice.net_payout_minor,
                    # HELD: null by definition (spec/16); RELEASED: recomputed.
                    "payout_expected_by": expected,
                },
            )

    def on_settlement_batch_held(self, envelope: Mapping[str, object]) -> None:
        self._fan_out_batch(envelope, include_expected_by=False)

    def on_settlement_batch_released(self, envelope: Mapping[str, object]) -> None:
        self._fan_out_batch(envelope, include_expected_by=True)

    # -- payment_intent.deferred ----------------------------------------------------

    def on_payment_intent_deferred(self, envelope: Mapping[str, object]) -> None:
        payload = self._payload(envelope)
        event_id = self._event_id(envelope)
        parties = self._parties_from_payload(payload, envelope)
        if parties is None:
            return
        merchant_id, customer_id = parties
        deferred_until_raw = payload.get("deferred_until")
        self._webhooks.dispatch_event(
            merchant_id=merchant_id,
            event_id=event_id,
            event_type="payment_intent.deferred",
            payload={
                "payment_intent_id": str(
                    payload.get("payment_intent_id") or envelope.get("subject_id") or ""
                ),
                "rail": str(payload.get("rail", "")),
                "deferred_until": deferred_until_raw,
            },
        )
        deferred_until = _parse_rfc3339(deferred_until_raw)
        if customer_id and deferred_until is not None:
            self._notify_customer(
                customer_id=customer_id,
                template_id="payment_deferred",
                template_vars={"scheduled_for": _dhaka_display(deferred_until)},
                event_id=event_id,
            )

    # -- qr_payload.paid ------------------------------------------------------------

    def on_qr_payload_paid(self, envelope: Mapping[str, object]) -> None:
        payload = self._payload(envelope)
        event_id = self._event_id(envelope)
        intent_id = self._payment_intent_id(payload, envelope)
        if not intent_id:
            self._log.warning("fanout: qr_payload.paid without payment_intent_id")
            return
        context = self._intent_context(intent_id, payload)
        if context is None:
            return
        if context.amount_minor is None:
            self._log.warning("fanout: qr_payload.paid without amount context for %s", intent_id)
            return
        amount_bdt = self._amount_bdt(context.amount_minor)
        merchant_name = context.merchant_display_name or context.merchant_id
        if context.customer_id:
            self._notify_customer(
                customer_id=context.customer_id,
                template_id="payment_succeeded",
                template_vars={"amount_bdt": amount_bdt, "merchant_name": merchant_name},
                event_id=event_id,
            )
        self._notifications.dispatch(
            recipient_type="merchant",
            recipient_id=context.merchant_id,
            channel="sms",
            template_id="payment_received",
            template_vars={"amount_bdt": amount_bdt},
            idempotency_key=f"{event_id}:payment_received:{context.merchant_id}",
            language=self._customer_language,
        )

    # -- refund.failed ------------------------------------------------------------

    def on_refund_failed(self, envelope: Mapping[str, object]) -> None:
        payload = self._payload(envelope)
        event_id = self._event_id(envelope)
        parties = self._parties_from_payload(payload, envelope)
        if parties is None:
            return
        merchant_id, customer_id = parties
        # spec/02 registry already lists gateway webhooks as a refund.failed
        # consumer (spec/01 §I catalogue row); the merchant webhook rides the
        # same machinery. The ADDED LR-5 audience is the customer message.
        self._webhooks.dispatch_event(
            merchant_id=merchant_id,
            event_id=event_id,
            event_type="refund.failed",
            payload={
                "refund_id": str(payload.get("refund_id", "")),
                "payment_intent_id": str(
                    payload.get("payment_intent_id") or envelope.get("subject_id") or ""
                ),
                "error_code": str(payload.get("error_code", "")),
            },
        )
        if customer_id:
            self._notify_customer(
                customer_id=customer_id,
                template_id="refund_failed",
                template_vars={},
                event_id=event_id,
            )

    # -- disputes ---------------------------------------------------------------------

    def _on_dispute(self, envelope: Mapping[str, object], *, resolved: bool) -> None:
        payload = self._payload(envelope)
        event_id = self._event_id(envelope)
        parties = self._parties_from_payload(payload, envelope)
        if parties is None:
            return
        merchant_id, customer_id = parties
        webhook_payload: dict[str, object] = {
            "dispute_id": str(payload.get("dispute_id", "")),
            "payment_intent_id": str(payload.get("payment_intent_id", "")),
            "state": str(payload.get("state", "")),
            "amount_minor": payload.get("amount_minor"),
        }
        self._webhooks.dispatch_event(
            merchant_id=merchant_id,
            event_id=event_id,
            event_type=str(envelope.get("type", "")),
            payload=webhook_payload,
        )
        if not customer_id:
            return
        if resolved:
            self._notify_customer(
                customer_id=customer_id,
                template_id="dispute_resolved",
                template_vars={"resolution": str(payload.get("state", "RESOLVED"))},
                event_id=event_id,
            )
        else:
            self._notify_customer(
                customer_id=customer_id,
                template_id="dispute_opened",
                template_vars={},
                event_id=event_id,
            )

    def on_dispute_opened(self, envelope: Mapping[str, object]) -> None:
        self._on_dispute(envelope, resolved=False)

    def on_dispute_resolved(self, envelope: Mapping[str, object]) -> None:
        self._on_dispute(envelope, resolved=True)

    # -- webhook.delivery_exhausted (merchant EMAIL) -----------------------------------

    def on_webhook_delivery_exhausted(self, envelope: Mapping[str, object]) -> None:
        payload = self._payload(envelope)
        merchant_id = str(payload.get("merchant_id", ""))
        if not merchant_id:
            self._log.warning(
                "fanout: delivery_exhausted without merchant_id (event %s)",
                envelope.get("event_id"),
            )
            return
        self._notifications.dispatch(
            recipient_type="merchant",
            recipient_id=merchant_id,
            channel="email",  # the webhook channel itself is dead (spec/16)
            template_id="webhook_exhausted",
            template_vars={
                "event_type": str(payload.get("event_type", "")),
                "attempt_count": str(payload.get("attempt_count", "")),
            },
            idempotency_key=f"{self._event_id(envelope)}:webhook_exhausted:{merchant_id}",
            language="en",  # spec/16: merchant email is en
        )

"""In-process event bus — the composition root's :class:`EventPublisher`.

The outbox is the ONLY producer path (spec/00 §5); the :class:`OutboxWorker`
drains rows and publishes envelopes through an injected publisher. In the
single-deployable composition (and every unit/e2e test) that publisher is this
bus: it fans each envelope out to the consumers subscribed to its event type.
The Redpanda producer slots into the same :class:`EventPublisher` protocol when
the broker is deployed — consumers do not change.

Consumer contract (spec/00 §5): idempotent on ``event_id``. The bus enforces
it structurally by wrapping every handler in
:class:`~bdpay.platform.outbox.IdempotentConsumer`, so a redelivered envelope
(at-least-once outbox semantics) is a recorded no-op per handler.

A handler exception propagates out of :meth:`publish` AFTER the remaining
handlers have run — the outbox worker then releases the claim and the event
retries (attempt-capped, POISON at the spec/02 cap). Handlers that already
processed the envelope dedupe the redelivery, so one failing consumer never
blocks the others and never double-fires them.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping

from bdpay.platform.outbox import IdempotentConsumer, OutboxError

__all__ = ["InProcessEventBus"]

Handler = Callable[[Mapping[str, object]], None]

#: Subscribe-to-everything key (ops mirrors, recording test consumers).
WILDCARD = "*"


class InProcessEventBus:
    """Routes outbox envelopes to subscribed consumers by event ``type``."""

    def __init__(self, *, logger: logging.Logger | None = None) -> None:
        self._handlers: dict[str, list[tuple[str, IdempotentConsumer]]] = {}
        self._log = logger or logging.getLogger("bdpay.platform.eventbus")

    def subscribe(self, event_type: str, handler: Handler, *, name: str | None = None) -> None:
        """Register ``handler`` for ``event_type`` (or ``"*"`` for all).

        The handler is wrapped in :class:`IdempotentConsumer`; duplicate
        ``event_id`` deliveries are recorded no-ops per subscription.
        """
        if not isinstance(event_type, str) or not event_type:
            raise OutboxError("event_type must be a non-empty string")
        if not callable(handler):
            raise OutboxError("handler must be callable")
        label = name or getattr(handler, "__qualname__", repr(handler))
        self._handlers.setdefault(event_type, []).append(
            (label, IdempotentConsumer(handler))
        )

    def subscriptions(self) -> dict[str, tuple[str, ...]]:
        """Registered handler labels per event type (introspection/tests)."""
        return {
            event_type: tuple(label for label, _ in handlers)
            for event_type, handlers in self._handlers.items()
        }

    # -- EventPublisher ------------------------------------------------------

    def publish(self, topic: str, envelope: Mapping[str, object]) -> None:
        """Fan the envelope out to every matching subscription.

        Every handler runs even when an earlier one raises; the first error is
        re-raised at the end so the outbox worker retries the event (handlers
        that succeeded dedupe the redelivery on ``event_id``).
        """
        event_type = envelope.get("type")
        if not isinstance(event_type, str) or not event_type:
            raise OutboxError("envelope is missing a string 'type'")
        matched = list(self._handlers.get(event_type, ()))
        matched.extend(self._handlers.get(WILDCARD, ()))
        first_error: Exception | None = None
        for label, consumer in matched:
            try:
                consumer.handle(envelope)
            except Exception as exc:  # noqa: BLE001 - per-consumer isolation
                self._log.warning(
                    "consumer %s failed for %s on %s: %s: %s",
                    label,
                    envelope.get("event_id"),
                    topic,
                    type(exc).__name__,
                    exc,
                )
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error

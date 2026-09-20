"""Durable inbound connector webhook sink."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bdpay.connectors.sdk import ConnectorResult
    from bdpay.platform.clock import Clock

__all__ = ["InMemoryWebhookSink", "PostgresWebhookSink"]


def _make_event_id(connector_id: str, connector_ref: str, raw_response_hash: str) -> str:
    payload = json.dumps(
        {
            "connector_id": connector_id,
            "connector_ref": connector_ref,
            "raw_response_hash": raw_response_hash,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return f"cie_{hashlib.sha256(payload).hexdigest()}"


class PostgresWebhookSink:
    """Persist verified inbound connector results before the HTTP handler returns."""

    def __init__(self, connection_factory: Callable[[], Any], *, clock: Clock) -> None:
        self._connect = connection_factory
        self._clock = clock

    def deliver(self, connector_id: str, result: ConnectorResult) -> None:
        event_id = _make_event_id(
            connector_id,
            result.connector_ref,
            result.raw_response_hash,
        )
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO connector_inbound_events (
                    event_id,
                    connector_id,
                    connector_ref,
                    instruction_id,
                    status_value,
                    rail_transaction_id,
                    responded_at,
                    error_code,
                    raw_response_hash,
                    processing_status,
                    received_at,
                    schema_version
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'RECEIVED', %s, 1)
                ON CONFLICT (connector_id, connector_ref, raw_response_hash) DO NOTHING
                """,
                (
                    event_id,
                    connector_id,
                    result.connector_ref,
                    result.instruction_id,
                    result.status.value,
                    result.rail_transaction_id,
                    result.responded_at,
                    result.error_code,
                    result.raw_response_hash,
                    self._clock.now(),
                ),
            )


class InMemoryWebhookSink:
    """Deterministic test sink with the same idempotency key."""

    def __init__(self) -> None:
        self._seen: set[str] = set()
        self.events: list[tuple[str, ConnectorResult]] = []

    def deliver(self, connector_id: str, result: ConnectorResult) -> None:
        event_id = _make_event_id(
            connector_id,
            result.connector_ref,
            result.raw_response_hash,
        )
        if event_id in self._seen:
            return
        self._seen.add(event_id)
        self.events.append((connector_id, result))

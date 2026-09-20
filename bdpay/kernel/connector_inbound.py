"""Durable connector inbound event draining."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from bdpay.connectors.sdk import ConnectorResult, ConnectorStatus

if TYPE_CHECKING:
    from bdpay.platform.clock import Clock

__all__ = [
    "ConnectorInboundEvent",
    "PostgresConnectorInboundEventStore",
]


@dataclass(frozen=True)
class ConnectorInboundEvent:
    event_id: str
    connector_id: str
    connector_ref: str
    instruction_id: str
    status_value: str
    rail_transaction_id: str | None
    responded_at: str | None
    error_code: str | None
    raw_response_hash: str

    def to_result(self) -> ConnectorResult:
        return ConnectorResult(
            instruction_id=self.instruction_id,
            connector_ref=self.connector_ref,
            status=ConnectorStatus(self.status_value),
            rail_transaction_id=self.rail_transaction_id,
            responded_at=self.responded_at,
            error_code=self.error_code,
            raw_response_hash=self.raw_response_hash,
        )


class _InboundApplier(Protocol):
    async def apply_inbound_connector_result(
        self, connector_id: str, result: ConnectorResult, *, conn: Any
    ) -> str: ...


class PostgresConnectorInboundEventStore:
    """Claim and apply verified connector callbacks from ``connector_inbound_events``."""

    def __init__(self, connection_factory, *, clock: Clock) -> None:
        self._connect = connection_factory
        self._clock = clock

    @staticmethod
    def _row_to_event(row: dict[str, Any]) -> ConnectorInboundEvent:
        return ConnectorInboundEvent(
            event_id=row["event_id"],
            connector_id=row["connector_id"],
            connector_ref=row["connector_ref"],
            instruction_id=row["instruction_id"],
            status_value=row["status_value"],
            rail_transaction_id=row["rail_transaction_id"],
            responded_at=row["responded_at"],
            error_code=row["error_code"],
            raw_response_hash=row["raw_response_hash"],
        )

    def _claim_received(self, conn: Any, *, limit: int) -> list[ConnectorInboundEvent]:
        from psycopg.rows import dict_row

        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT
                    event_id,
                    connector_id,
                    connector_ref,
                    instruction_id,
                    status_value,
                    rail_transaction_id,
                    responded_at,
                    error_code,
                    raw_response_hash
                FROM connector_inbound_events
                WHERE processing_status = 'RECEIVED'
                ORDER BY received_at, event_id
                LIMIT %s
                FOR UPDATE SKIP LOCKED
                """,
                (limit,),
            )
            return [self._row_to_event(dict(row)) for row in cur.fetchall()]

    def _mark(self, conn: Any, event_id: str, status: str) -> None:
        if status not in {"DISPATCHED", "SKIPPED"}:
            raise ValueError(f"invalid connector inbound processing status {status!r}")
        conn.execute(
            """
            UPDATE connector_inbound_events
            SET processing_status = %s, dispatched_at = %s
            WHERE event_id = %s AND processing_status = 'RECEIVED'
            """,
            (status, self._clock.now(), event_id),
        )

    async def drain_once(self, applier: _InboundApplier, *, limit: int = 100) -> dict[str, int]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        counts = {"received": 0, "dispatched": 0, "skipped": 0}
        with self._connect() as conn:
            with conn.transaction():
                events = self._claim_received(conn, limit=limit)
                counts["received"] = len(events)
                for event in events:
                    result = event.to_result()
                    status = await applier.apply_inbound_connector_result(
                        event.connector_id, result, conn=conn
                    )
                    self._mark(conn, event.event_id, status)
                    if status == "DISPATCHED":
                        counts["dispatched"] += 1
                    else:
                        counts["skipped"] += 1
        return counts

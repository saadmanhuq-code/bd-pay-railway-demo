"""Per-connector idempotency store (spec/10 SDK invariant: idempotent on
``connector_ref``).

Keyed by ``(connector_id, connector_ref, call_kind)``; a replay returns the
recorded :class:`~bdpay.connectors.sdk.ConnectorResult` unchanged, exercising
the consumer's dedupe exactly as the simulator DSL requires. Pattern lineage:
the INSERT-OR-IGNORE dedupe writer in dse-profit-engine storage/store.py
(ADAPT PATTERN — first write wins, replays are pure reads).

Write-ahead dispatch intents: ``begin`` persists a :class:`DispatchIntent`
for the ``connector_ref`` BEFORE the runner touches the rail, so a crash
mid-dispatch leaves a durable breadcrumb that an attempt with that ref may
have reached the rail (recovery must ``query_status`` first, never blind
resubmit). ``begin`` is itself idempotent — the first intent wins.

CONN-03 / BP-G4b: Postgres impl (migration ``0162_mfs_idempotency_stores``)
mirrors NPSB ``PostgresStanStore`` / MFS ``PostgresMfsHandleStore``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from bdpay.connectors.sdk import ConnectorResult, ConnectorStatus
from bdpay.platform.clock import Clock, SystemClock

__all__ = [
    "DispatchIntent",
    "IdempotencyStore",
    "InMemoryIdempotencyStore",
    "PostgresIdempotencyStore",
]

ConnectionFactory = Callable[[], Any]


@dataclass(frozen=True)
class DispatchIntent:
    """Pre-dispatch record: this ref may have reached the rail."""

    connector_id: str
    connector_ref: str
    call_kind: str
    instruction_id: str
    recorded_at: datetime


@runtime_checkable
class IdempotencyStore(Protocol):
    """Recorded-result repository keyed (connector_id, connector_ref, call_kind)."""

    def get(
        self, connector_id: str, connector_ref: str, call_kind: str
    ) -> ConnectorResult | None: ...

    def put(
        self, connector_id: str, connector_ref: str, call_kind: str, result: ConnectorResult
    ) -> ConnectorResult: ...

    def begin(
        self,
        connector_id: str,
        connector_ref: str,
        call_kind: str,
        *,
        instruction_id: str,
        recorded_at: datetime,
    ) -> DispatchIntent: ...

    def intent(
        self, connector_id: str, connector_ref: str, call_kind: str
    ) -> DispatchIntent | None: ...


class InMemoryIdempotencyStore:
    """Deterministic in-memory idempotency store; first write wins.

    Pass shared ``recorded`` / ``intents`` dicts to simulate process restart
    against the same backing (unit drills without Postgres).
    """

    def __init__(
        self,
        recorded: dict[tuple[str, str, str], ConnectorResult] | None = None,
        intents: dict[tuple[str, str, str], DispatchIntent] | None = None,
    ) -> None:
        self._recorded: dict[tuple[str, str, str], ConnectorResult] = (
            recorded if recorded is not None else {}
        )
        self._intents: dict[tuple[str, str, str], DispatchIntent] = (
            intents if intents is not None else {}
        )

    def get(self, connector_id: str, connector_ref: str, call_kind: str) -> ConnectorResult | None:
        return self._recorded.get((connector_id, connector_ref, call_kind))

    def put(
        self, connector_id: str, connector_ref: str, call_kind: str, result: ConnectorResult
    ) -> ConnectorResult:
        key = (connector_id, connector_ref, call_kind)
        existing = self._recorded.get(key)
        if existing is not None:
            return existing
        self._recorded[key] = result
        return result

    def begin(
        self,
        connector_id: str,
        connector_ref: str,
        call_kind: str,
        *,
        instruction_id: str,
        recorded_at: datetime,
    ) -> DispatchIntent:
        key = (connector_id, connector_ref, call_kind)
        existing = self._intents.get(key)
        if existing is not None:
            return existing
        intent = DispatchIntent(
            connector_id=connector_id,
            connector_ref=connector_ref,
            call_kind=call_kind,
            instruction_id=instruction_id,
            recorded_at=recorded_at,
        )
        self._intents[key] = intent
        return intent

    def intent(
        self, connector_id: str, connector_ref: str, call_kind: str
    ) -> DispatchIntent | None:
        return self._intents.get((connector_id, connector_ref, call_kind))


class PostgresIdempotencyStore:
    """Postgres-backed connector idempotency (migration ``0162_mfs_idempotency_stores``)."""

    def __init__(
        self, connection_factory: ConnectionFactory, *, clock: Clock | None = None
    ) -> None:
        self._connect = connection_factory
        self._clock = clock if clock is not None else SystemClock()

    @staticmethod
    def _to_result(row: Any) -> ConnectorResult:
        return ConnectorResult(
            instruction_id=row["instruction_id"],
            connector_ref=row["connector_ref"],
            status=ConnectorStatus(row["status"]),
            rail_transaction_id=row["rail_transaction_id"],
            responded_at=row["responded_at"],
            error_code=row["error_code"],
            raw_response_hash=row["raw_response_hash"],
        )

    @staticmethod
    def _to_intent(row: Any) -> DispatchIntent:
        return DispatchIntent(
            connector_id=row["connector_id"],
            connector_ref=row["connector_ref"],
            call_kind=row["call_kind"],
            instruction_id=row["instruction_id"],
            recorded_at=row["recorded_at"],
        )

    def get(self, connector_id: str, connector_ref: str, call_kind: str) -> ConnectorResult | None:
        from psycopg.rows import dict_row

        with self._connect() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """
                    SELECT instruction_id, connector_ref, status, rail_transaction_id,
                           responded_at, error_code, raw_response_hash
                    FROM connector_idempotency_results
                    WHERE connector_id = %s AND connector_ref = %s AND call_kind = %s
                    """,
                    (connector_id, connector_ref, call_kind),
                )
                row = cur.fetchone()
        return None if row is None else self._to_result(row)

    def put(
        self, connector_id: str, connector_ref: str, call_kind: str, result: ConnectorResult
    ) -> ConnectorResult:
        from psycopg.rows import dict_row

        recorded_at = self._clock.now()
        with self._connect() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """
                    INSERT INTO connector_idempotency_results (
                        connector_id, connector_ref, call_kind, instruction_id,
                        status, rail_transaction_id, responded_at, error_code,
                        raw_response_hash, recorded_at, schema_version
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 1)
                    ON CONFLICT (connector_id, connector_ref, call_kind) DO NOTHING
                    RETURNING instruction_id, connector_ref, status, rail_transaction_id,
                              responded_at, error_code, raw_response_hash
                    """,
                    (
                        connector_id,
                        connector_ref,
                        call_kind,
                        result.instruction_id,
                        result.status.value,
                        result.rail_transaction_id,
                        result.responded_at,
                        result.error_code,
                        result.raw_response_hash,
                        recorded_at,
                    ),
                )
                row = cur.fetchone()
                if row is None:
                    cur.execute(
                        """
                        SELECT instruction_id, connector_ref, status, rail_transaction_id,
                               responded_at, error_code, raw_response_hash
                        FROM connector_idempotency_results
                        WHERE connector_id = %s AND connector_ref = %s AND call_kind = %s
                        """,
                        (connector_id, connector_ref, call_kind),
                    )
                    row = cur.fetchone()
            conn.commit()
        assert row is not None
        return self._to_result(row)

    def begin(
        self,
        connector_id: str,
        connector_ref: str,
        call_kind: str,
        *,
        instruction_id: str,
        recorded_at: datetime,
    ) -> DispatchIntent:
        from psycopg.rows import dict_row

        with self._connect() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """
                    INSERT INTO connector_dispatch_intents (
                        connector_id, connector_ref, call_kind, instruction_id,
                        recorded_at, schema_version
                    ) VALUES (%s, %s, %s, %s, %s, 1)
                    ON CONFLICT (connector_id, connector_ref, call_kind) DO NOTHING
                    RETURNING connector_id, connector_ref, call_kind, instruction_id, recorded_at
                    """,
                    (connector_id, connector_ref, call_kind, instruction_id, recorded_at),
                )
                row = cur.fetchone()
                if row is None:
                    cur.execute(
                        """
                        SELECT connector_id, connector_ref, call_kind, instruction_id, recorded_at
                        FROM connector_dispatch_intents
                        WHERE connector_id = %s AND connector_ref = %s AND call_kind = %s
                        """,
                        (connector_id, connector_ref, call_kind),
                    )
                    row = cur.fetchone()
            conn.commit()
        assert row is not None
        return self._to_intent(row)

    def intent(
        self, connector_id: str, connector_ref: str, call_kind: str
    ) -> DispatchIntent | None:
        from psycopg.rows import dict_row

        with self._connect() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """
                    SELECT connector_id, connector_ref, call_kind, instruction_id, recorded_at
                    FROM connector_dispatch_intents
                    WHERE connector_id = %s AND connector_ref = %s AND call_kind = %s
                    """,
                    (connector_id, connector_ref, call_kind),
                )
                row = cur.fetchone()
        return None if row is None else self._to_intent(row)

"""ReplayRecord storage — ``ledger.ledger_replay_records`` (migration 0103).

Append-only output discipline (spec/03): replay results are never
overwritten; terminal rows are immutable. State advances only through the
ReplayRequest FSM (:mod:`bdpay.ledger.replay.service`); both stores refuse
terminal mutation, and the Postgres pickup serializes claimants via
``pg_advisory_xact_lock`` over the rply-id hash (SPEC_ERRATA E2 pattern, as
in ``bdpay/ledger/pg_store.py::lock_chain_tip``).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable

from bdpay.ledger.replay.errors import ReplayInvalidTransitionError, ReplayNotFoundError
from bdpay.ledger.replay.types import ReplayRecord
from bdpay.platform.canonical import canonical_json

__all__ = [
    "InMemoryReplayRecordStore",
    "PostgresReplayRecordStore",
    "ReplayRecordStore",
]


@runtime_checkable
class ReplayRecordStore(Protocol):
    """Storage contract for ledger_replay_records."""

    def insert(self, record: ReplayRecord) -> None: ...

    def get(self, replay_id: str) -> ReplayRecord | None: ...

    def begin_run(self, replay_id: str, started_at: Any) -> ReplayRecord | None:
        """Atomically claim QUEUED -> RUNNING; None when not claimable."""
        ...

    def save(self, record: ReplayRecord) -> None:
        """Persist a RUNNING-row advance; terminal rows are immutable."""
        ...

    def list_for_reference(self, reference_id: str) -> list[ReplayRecord]: ...


class InMemoryReplayRecordStore:
    """Deterministic in-memory store; mirrors the Postgres semantics."""

    def __init__(self) -> None:
        self._rows: dict[str, ReplayRecord] = {}
        self._order: list[str] = []
        self._claimed: set[str] = set()

    def insert(self, record: ReplayRecord) -> None:
        if record.replay_id in self._rows:
            raise ReplayInvalidTransitionError(
                f"replay record {record.replay_id} already exists (append-only)"
            )
        self._rows[record.replay_id] = record
        self._order.append(record.replay_id)

    def get(self, replay_id: str) -> ReplayRecord | None:
        return self._rows.get(replay_id)

    def begin_run(self, replay_id: str, started_at: Any) -> ReplayRecord | None:
        record = self._rows.get(replay_id)
        if record is None or record.status != "QUEUED" or replay_id in self._claimed:
            return None
        self._claimed.add(replay_id)
        import dataclasses

        updated = dataclasses.replace(record, status="RUNNING", started_at=started_at)
        self._rows[replay_id] = updated
        return updated

    def save(self, record: ReplayRecord) -> None:
        existing = self._rows.get(record.replay_id)
        if existing is None:
            raise ReplayNotFoundError(f"unknown replay record {record.replay_id}")
        if existing.is_terminal:
            raise ReplayInvalidTransitionError(
                f"replay record {record.replay_id} is terminal ({existing.status}); "
                "stored rows are NEVER overwritten"
            )
        self._rows[record.replay_id] = record

    def list_for_reference(self, reference_id: str) -> list[ReplayRecord]:
        return [
            self._rows[rid] for rid in self._order if self._rows[rid].reference_id == reference_id
        ]


_COLUMNS = (
    "replay_id, reference_id, reference_type, replay_mode, status, requested_by,"
    " requested_at, started_at, completed_at, result_summary, result_pointer, schema_version"
)


class PostgresReplayRecordStore:
    """``ledger.ledger_replay_records`` via psycopg3 (migration 0103)."""

    def __init__(self, connection_factory: Callable[[], Any]) -> None:
        self._connect = connection_factory

    @staticmethod
    def _jsonb(payload: Any) -> Any:
        if payload is None:
            return None
        from psycopg.types.json import Jsonb

        return Jsonb(dict(payload), dumps=lambda o: canonical_json(o).decode("utf-8"))

    @staticmethod
    def _to_record(row: tuple[Any, ...]) -> ReplayRecord:
        return ReplayRecord(*row)

    def insert(self, record: ReplayRecord) -> None:
        with self._connect() as conn:
            conn.execute(
                f"INSERT INTO ledger.ledger_replay_records ({_COLUMNS})"
                f" VALUES ({', '.join(['%s'] * 12)})",
                (
                    record.replay_id,
                    record.reference_id,
                    record.reference_type,
                    record.replay_mode,
                    record.status,
                    record.requested_by,
                    record.requested_at,
                    record.started_at,
                    record.completed_at,
                    self._jsonb(record.result_summary),
                    record.result_pointer,
                    record.schema_version,
                ),
            )
            conn.commit()

    def get(self, replay_id: str) -> ReplayRecord | None:
        with self._connect() as conn:
            cur = conn.execute(
                f"SELECT {_COLUMNS} FROM ledger.ledger_replay_records WHERE replay_id = %s",
                (replay_id,),
            )
            row = cur.fetchone()
        return self._to_record(row) if row else None

    def begin_run(self, replay_id: str, started_at: Any) -> ReplayRecord | None:
        with self._connect() as conn:
            with conn.transaction():
                # E2 pattern: serialize claimants on the rply id hash.
                conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (f"bdpay.ledger_replay_records.{replay_id}",),
                )
                cur = conn.execute(
                    "UPDATE ledger.ledger_replay_records"
                    " SET status = 'RUNNING', started_at = %s"
                    " WHERE replay_id = %s AND status = 'QUEUED'",
                    (started_at, replay_id),
                )
                if cur.rowcount != 1:
                    return None
        return self.get(replay_id)

    def save(self, record: ReplayRecord) -> None:
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE ledger.ledger_replay_records"
                " SET status = %s, completed_at = %s, result_summary = %s, result_pointer = %s"
                " WHERE replay_id = %s AND status NOT IN ('COMPLETED','FAILED')",
                (
                    record.status,
                    record.completed_at,
                    self._jsonb(record.result_summary),
                    record.result_pointer,
                    record.replay_id,
                ),
            )
            updated = cur.rowcount
            conn.commit()
        if updated == 0:
            existing = self.get(record.replay_id)
            if existing is None:
                raise ReplayNotFoundError(f"unknown replay record {record.replay_id}")
            raise ReplayInvalidTransitionError(
                f"replay record {record.replay_id} is terminal ({existing.status}); "
                "stored rows are NEVER overwritten"
            )

    def list_for_reference(self, reference_id: str) -> list[ReplayRecord]:
        with self._connect() as conn:
            cur = conn.execute(
                f"SELECT {_COLUMNS} FROM ledger.ledger_replay_records"
                " WHERE reference_id = %s ORDER BY requested_at ASC, replay_id ASC",
                (reference_id,),
            )
            rows = cur.fetchall()
        return [self._to_record(row) for row in rows]

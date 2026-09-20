"""Postgres ReconStore (psycopg3, sync) — matches 0013_reconciliation.sql."""

from __future__ import annotations

from typing import Any

import psycopg

from bdpay.ledger.errors import ReconciliationError
from bdpay.ledger.reconciliation.types import (
    ReconciliationExceptionRow,
    ReconciliationRecordRow,
)

_RECORD_COLUMNS = (
    "record_id, batch_id, rail, rail_transaction_id, rail_amount_minor, rail_direction, "
    "rail_effective_date, rail_merchant_ref, raw_line_hash, parse_failed, parse_error, "
    "match_status, match_level, matched_instruction_id, delta_minor, ingested_at, matched_at, "
    "produced_by, schema_version"
)

_EXCEPTION_COLUMNS = (
    "exception_id, record_id, resolution_tier, reason_code, rail_amount_minor, "
    "internal_amount_minor, delta_minor, status, assigned_to, raised_at, due_by, resolved_at, "
    "closed_at, resolution_action, adjustment_amount_minor, journal_entry_id, "
    "approval_request_id, notes, supporting_ref, resolved_by, aml_alert_id, produced_by, "
    "schema_version"
)

_RECORD_MUTABLE = frozenset(
    {"match_status", "match_level", "matched_instruction_id", "delta_minor", "matched_at"}
)

_EXCEPTION_MUTABLE = frozenset(
    {
        "status",
        "assigned_to",
        "resolved_at",
        "closed_at",
        "resolution_action",
        "adjustment_amount_minor",
        "journal_entry_id",
        "approval_request_id",
        "notes",
        "supporting_ref",
        "resolved_by",
        "aml_alert_id",
        "resolution_tier",  # spec/PT-H: tier-upgrade path (AUTO_TIMING_GAP → FINANCE_QUEUE)
    }
)


def _record_from(row: tuple[Any, ...]) -> ReconciliationRecordRow:
    return ReconciliationRecordRow(*row)


def _exception_from(row: tuple[Any, ...]) -> ReconciliationExceptionRow:
    return ReconciliationExceptionRow(*row)


class PostgresReconStore:
    """Sync psycopg3 store over an autocommit connection (ledger schema)."""

    def __init__(self, conn: psycopg.Connection[tuple[Any, ...]]) -> None:
        if not conn.autocommit:
            raise ValueError("PostgresReconStore requires an autocommit connection")
        self._conn = conn

    @classmethod
    def connect(cls, dsn: str, *, role: str | None = None) -> PostgresReconStore:
        conn = psycopg.connect(dsn, autocommit=True, options="-c TimeZone=UTC")
        if role is not None:
            conn.execute(psycopg.sql.SQL("SET ROLE {}").format(psycopg.sql.Identifier(role)))
        return cls(conn)

    def close(self) -> None:
        self._conn.close()

    # --- records ---

    def insert_record(self, row: ReconciliationRecordRow) -> None:
        self._conn.execute(
            f"INSERT INTO ledger.reconciliation_records ({_RECORD_COLUMNS})"
            f" VALUES ({', '.join(['%s'] * 19)})",
            (
                row.record_id,
                row.batch_id,
                row.rail,
                row.rail_transaction_id,
                row.rail_amount_minor,
                row.rail_direction,
                row.rail_effective_date,
                row.rail_merchant_ref,
                row.raw_line_hash,
                row.parse_failed,
                row.parse_error,
                row.match_status,
                row.match_level,
                row.matched_instruction_id,
                row.delta_minor,
                row.ingested_at,
                row.matched_at,
                row.produced_by,
                row.schema_version,
            ),
        )

    def get_record(self, record_id: str) -> ReconciliationRecordRow | None:
        cur = self._conn.execute(
            f"SELECT {_RECORD_COLUMNS} FROM ledger.reconciliation_records WHERE record_id = %s",
            (record_id,),
        )
        row = cur.fetchone()
        return _record_from(row) if row else None

    def update_record(self, record_id: str, **changes: object) -> ReconciliationRecordRow:
        self._update(
            "reconciliation_records", "record_id", record_id, _RECORD_MUTABLE, changes
        )
        updated = self.get_record(record_id)
        assert updated is not None
        return updated

    def list_records(
        self, batch_id: str | None = None, match_status: str | None = None
    ) -> list[ReconciliationRecordRow]:
        clauses: list[str] = []
        params: list[Any] = []
        if batch_id is not None:
            clauses.append("batch_id = %s")
            params.append(batch_id)
        if match_status is not None:
            clauses.append("match_status = %s")
            params.append(match_status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        cur = self._conn.execute(
            f"SELECT {_RECORD_COLUMNS} FROM ledger.reconciliation_records{where}"
            " ORDER BY ingested_at, record_id",
            params,
        )
        return [_record_from(row) for row in cur.fetchall()]

    # --- exceptions ---

    def insert_exception(self, row: ReconciliationExceptionRow) -> None:
        self._conn.execute(
            f"INSERT INTO ledger.reconciliation_exceptions ({_EXCEPTION_COLUMNS})"
            f" VALUES ({', '.join(['%s'] * 23)})",
            (
                row.exception_id,
                row.record_id,
                row.resolution_tier,
                row.reason_code,
                row.rail_amount_minor,
                row.internal_amount_minor,
                row.delta_minor,
                row.status,
                row.assigned_to,
                row.raised_at,
                row.due_by,
                row.resolved_at,
                row.closed_at,
                row.resolution_action,
                row.adjustment_amount_minor,
                row.journal_entry_id,
                row.approval_request_id,
                row.notes,
                row.supporting_ref,
                row.resolved_by,
                row.aml_alert_id,
                row.produced_by,
                row.schema_version,
            ),
        )

    def get_exception(self, exception_id: str) -> ReconciliationExceptionRow | None:
        cur = self._conn.execute(
            f"SELECT {_EXCEPTION_COLUMNS} FROM ledger.reconciliation_exceptions"
            " WHERE exception_id = %s",
            (exception_id,),
        )
        row = cur.fetchone()
        return _exception_from(row) if row else None

    def update_exception(
        self, exception_id: str, **changes: object
    ) -> ReconciliationExceptionRow:
        self._update(
            "reconciliation_exceptions",
            "exception_id",
            exception_id,
            _EXCEPTION_MUTABLE,
            changes,
        )
        updated = self.get_exception(exception_id)
        assert updated is not None
        return updated

    def list_exceptions(
        self, status: str | None = None, resolution_tier: str | None = None
    ) -> list[ReconciliationExceptionRow]:
        clauses: list[str] = []
        params: list[Any] = []
        if status is not None:
            clauses.append("status = %s")
            params.append(status)
        if resolution_tier is not None:
            clauses.append("resolution_tier = %s")
            params.append(resolution_tier)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        cur = self._conn.execute(
            f"SELECT {_EXCEPTION_COLUMNS} FROM ledger.reconciliation_exceptions{where}"
            " ORDER BY raised_at, exception_id",
            params,
        )
        return [_exception_from(row) for row in cur.fetchall()]

    def find_exception_for_record(
        self, record_id: str
    ) -> ReconciliationExceptionRow | None:
        cur = self._conn.execute(
            f"SELECT {_EXCEPTION_COLUMNS} FROM ledger.reconciliation_exceptions"
            " WHERE record_id = %s AND status IN ('OPEN','IN_REVIEW','ESCALATED')"
            " ORDER BY raised_at LIMIT 1",
            (record_id,),
        )
        row = cur.fetchone()
        return _exception_from(row) if row else None

    # --- helpers ---

    def _update(
        self,
        table: str,
        key_column: str,
        key: str,
        allowed: frozenset[str],
        changes: dict[str, object],
    ) -> None:
        unknown = set(changes) - allowed
        if unknown:
            raise ReconciliationError(f"non-updatable column(s) {sorted(unknown)}")
        if not changes:
            return
        columns = sorted(changes)
        sets = ", ".join(f"{column} = %s" for column in columns)
        params: list[Any] = [changes[column] for column in columns]
        params.append(key)
        cur = self._conn.execute(
            f"UPDATE ledger.{table} SET {sets} WHERE {key_column} = %s",  # noqa: S608
            params,
        )
        if cur.rowcount != 1:
            raise ReconciliationError(f"unknown {table} key {key}")

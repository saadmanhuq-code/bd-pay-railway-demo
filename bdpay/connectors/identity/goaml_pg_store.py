"""Postgres-backed durable filing queue for ``GoamlReporterConnector``."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from bdpay.connectors.identity.goaml import GoamlFilingRow
from bdpay.platform.errors import ConflictError, NotFoundError

__all__ = ["PostgresGoamlFilingStore"]

_COLUMN_NAMES = (
    "filing_id",
    "report_type",
    "report_ref",
    "attempt_no",
    "state",
    "xml_payload_hash",
    "xml_payload_pointer",
    "goaml_submission_ref",
    "portal_reject_reason_hash",
    "retry_count",
    "next_retry_at",
    "submitted_at",
    "acked_at",
    "created_at",
    "updated_at",
    "schema_version",
)
_COLUMNS = ", ".join(_COLUMN_NAMES)
_RETURNING_COLUMNS = ", ".join(f"g.{name}" for name in _COLUMN_NAMES)

_MUTABLE_SET = (
    "state = %s, "
    "goaml_submission_ref = %s, "
    "portal_reject_reason_hash = %s, "
    "retry_count = %s, "
    "next_retry_at = %s, "
    "submitted_at = %s, "
    "acked_at = %s, "
    "updated_at = %s"
)


class PostgresGoamlFilingStore:
    """psycopg3 implementation of the goAML filing queue store port."""

    durable = True

    def __init__(self, connection_factory: Callable[[], Any]) -> None:
        self._connect = connection_factory

    def _execute(self, sql: str, params: tuple) -> int:
        with self._connect() as conn:
            cur = conn.execute(sql, params)
            rowcount = cur.rowcount
            conn.commit()
            return rowcount

    def _fetchone(self, sql: str, params: tuple) -> dict | None:
        from psycopg.rows import dict_row

        with self._connect() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(sql, params)
                return cur.fetchone()

    def _fetchall(self, sql: str, params: tuple) -> list[dict]:
        from psycopg.rows import dict_row

        with self._connect() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(sql, params)
                return cur.fetchall()

    @staticmethod
    def _to_row(db: dict) -> GoamlFilingRow:
        return GoamlFilingRow(
            filing_id=db["filing_id"],
            report_type=db["report_type"],
            report_ref=db["report_ref"],
            attempt_no=db["attempt_no"],
            xml_payload_hash=db["xml_payload_hash"],
            xml_payload_pointer=db["xml_payload_pointer"],
            state=db["state"],
            goaml_submission_ref=db["goaml_submission_ref"],
            portal_reject_reason_hash=db["portal_reject_reason_hash"],
            retry_count=db["retry_count"],
            next_retry_at=_aware(db["next_retry_at"]),
            submitted_at=_aware(db["submitted_at"]),
            acked_at=_aware(db["acked_at"]),
            created_at=_aware(db["created_at"]),
            updated_at=_aware(db["updated_at"]),
            schema_version=int(db["schema_version"]),
        )

    def enqueue(self, row: GoamlFilingRow) -> None:
        from psycopg.errors import UniqueViolation

        try:
            self._execute(
                f"INSERT INTO goaml_filings ({_COLUMNS}) VALUES "
                "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    row.filing_id,
                    row.report_type,
                    row.report_ref,
                    row.attempt_no,
                    row.state,
                    row.xml_payload_hash,
                    row.xml_payload_pointer,
                    row.goaml_submission_ref,
                    row.portal_reject_reason_hash,
                    row.retry_count,
                    row.next_retry_at,
                    row.submitted_at,
                    row.acked_at,
                    row.created_at,
                    row.updated_at,
                    row.schema_version,
                ),
            )
        except UniqueViolation as exc:
            raise ConflictError(
                f"goaml filing {row.filing_id!r} already exists",
                code="duplicate_filing",
            ) from exc

    def save(self, row: GoamlFilingRow) -> None:
        updated = self._execute(
            f"UPDATE goaml_filings SET {_MUTABLE_SET} WHERE filing_id = %s",
            (
                row.state,
                row.goaml_submission_ref,
                row.portal_reject_reason_hash,
                row.retry_count,
                row.next_retry_at,
                row.submitted_at,
                row.acked_at,
                row.updated_at,
                row.filing_id,
            ),
        )
        if updated != 1:
            raise NotFoundError(
                f"goaml filing {row.filing_id!r} not found",
                code="filing_not_found",
            )

    def get(self, filing_id: str) -> GoamlFilingRow | None:
        db = self._fetchone(
            f"SELECT {_COLUMNS} FROM goaml_filings WHERE filing_id = %s",
            (filing_id,),
        )
        return None if db is None else self._to_row(db)

    def rows(self) -> list[GoamlFilingRow]:
        rows = self._fetchall(f"SELECT {_COLUMNS} FROM goaml_filings ORDER BY seq", ())
        return [self._to_row(row) for row in rows]

    def latest_for(self, report_ref: str) -> GoamlFilingRow | None:
        db = self._fetchone(
            f"SELECT {_COLUMNS} FROM goaml_filings "
            "WHERE report_ref = %s ORDER BY seq DESC LIMIT 1",
            (report_ref,),
        )
        return None if db is None else self._to_row(db)

    def list_due(self, *, now: datetime) -> list[GoamlFilingRow]:
        rows = self._fetchall(
            f"SELECT {_COLUMNS} FROM goaml_filings "
            "WHERE state = 'QUEUED' AND (next_retry_at IS NULL OR next_retry_at <= %s) "
            "ORDER BY seq",
            (now,),
        )
        return [self._to_row(row) for row in rows]

    def claim_due(
        self, *, now: datetime, lease_until: datetime, limit: int = 100
    ) -> list[GoamlFilingRow]:
        if isinstance(limit, bool) or limit < 1:
            raise ValueError("limit must be a positive integer")
        from psycopg.rows import dict_row

        with self._connect() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    f"""
                    WITH due AS (
                        SELECT filing_id
                        FROM goaml_filings
                        WHERE (
                            state = 'QUEUED'
                            AND (next_retry_at IS NULL OR next_retry_at <= %(now)s)
                        ) OR (
                            state = 'SUBMITTING'
                            AND (next_retry_at IS NULL OR next_retry_at <= %(now)s)
                        )
                        ORDER BY seq
                        FOR UPDATE SKIP LOCKED
                        LIMIT %(limit)s
                    )
                    UPDATE goaml_filings AS g
                    SET state = 'SUBMITTING',
                        next_retry_at = %(lease_until)s,
                        updated_at = %(now)s
                    FROM due
                    WHERE g.filing_id = due.filing_id
                    RETURNING {_RETURNING_COLUMNS}
                    """,
                    {"now": now, "lease_until": lease_until, "limit": limit},
                )
                rows = cur.fetchall()
            conn.commit()
        return [self._to_row(row) for row in rows]


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None or dt.tzinfo is not None:
        return dt
    return dt.replace(tzinfo=UTC)

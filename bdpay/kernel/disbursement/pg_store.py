"""Postgres storage for spec/17 disbursement batches and items."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Any

from bdpay.kernel.disbursement.models import DisbursementBatch, DisbursementItem
from bdpay.platform.errors import ConflictError, NotFoundError

__all__ = ["PostgresDisbursementStore"]

_BATCH_COLUMNS = (
    "batch_id, merchant_id, client_batch_ref, item_count, total_amount_minor, "
    "fees_total_minor, status, maker_user_id, approval_request_id, "
    "reconciliation_tag, created_at, submitted_at, approved_at, dispatched_at, "
    "completed_at, cancelled_at, schema_version"
)

_ITEM_COLUMNS = (
    "item_id, batch_id, ordinal, beneficiary_ref, beneficiary_name_normalized, rail, "
    "amount_minor, fee_minor, purpose_code, item_ref, status, rail_transaction_id, "
    "settled_at, screened_at, screening_list_version, schema_version"
)


class PostgresDisbursementStore:
    """psycopg3 store matching ``db/migrations/0120_disbursement_batches.sql``."""

    def __init__(self, connection_factory: Callable[[], Any]) -> None:
        self._connect = connection_factory

    def _execute(self, conn: Any | None, sql: str, params: tuple | list[object]) -> None:
        if conn is not None:
            conn.execute(sql, params)
            return
        with self._connect() as owned:
            owned.execute(sql, params)
            owned.commit()

    def _executemany(
        self,
        conn: Any | None,
        sql: str,
        params: list[tuple[object, ...]],
    ) -> None:
        if conn is not None:
            with conn.cursor() as cur:
                cur.executemany(sql, params)
            return
        with self._connect() as owned:
            with owned.cursor() as cur:
                cur.executemany(sql, params)
            owned.commit()

    def _fetchone(
        self, conn: Any | None, sql: str, params: tuple | list[object]
    ) -> Mapping[str, Any] | None:
        from psycopg.rows import dict_row

        if conn is not None:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(sql, params)
                return cur.fetchone()
        with self._connect() as owned:
            with owned.cursor(row_factory=dict_row) as cur:
                cur.execute(sql, params)
                return cur.fetchone()

    def _fetchall(
        self, conn: Any | None, sql: str, params: tuple | list[object]
    ) -> list[Mapping[str, Any]]:
        from psycopg.rows import dict_row

        if conn is not None:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(sql, params)
                return cur.fetchall()
        with self._connect() as owned:
            with owned.cursor(row_factory=dict_row) as cur:
                cur.execute(sql, params)
                return cur.fetchall()

    @staticmethod
    def _batch(row: Mapping[str, Any]) -> DisbursementBatch:
        return DisbursementBatch(
            batch_id=row["batch_id"],
            merchant_id=row["merchant_id"],
            client_batch_ref=row["client_batch_ref"],
            item_count=row["item_count"],
            total_amount_minor=row["total_amount_minor"],
            fees_total_minor=row["fees_total_minor"],
            status=row["status"],
            maker_user_id=row["maker_user_id"],
            approval_request_id=row["approval_request_id"],
            reconciliation_tag=row["reconciliation_tag"],
            created_at=row["created_at"],
            submitted_at=row["submitted_at"],
            approved_at=row["approved_at"],
            dispatched_at=row["dispatched_at"],
            completed_at=row["completed_at"],
            cancelled_at=row["cancelled_at"],
            schema_version=row["schema_version"],
        )

    @staticmethod
    def _item(row: Mapping[str, Any]) -> DisbursementItem:
        return DisbursementItem(
            item_id=row["item_id"],
            batch_id=row["batch_id"],
            ordinal=row["ordinal"],
            beneficiary_ref=row["beneficiary_ref"],
            beneficiary_name_normalized=row["beneficiary_name_normalized"],
            rail=row["rail"],
            amount_minor=row["amount_minor"],
            fee_minor=row["fee_minor"],
            purpose_code=row["purpose_code"],
            item_ref=row["item_ref"],
            status=row["status"],
            rail_transaction_id=row["rail_transaction_id"],
            settled_at=row["settled_at"],
            screened_at=row["screened_at"],
            screening_list_version=row["screening_list_version"],
            schema_version=row["schema_version"],
        )

    def insert_batch(self, batch: DisbursementBatch, *, conn: Any | None = None) -> None:
        import psycopg

        sql = (
            f"INSERT INTO disbursement_batches ({_BATCH_COLUMNS}) VALUES "
            "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
        )
        params = (
            batch.batch_id,
            batch.merchant_id,
            batch.client_batch_ref,
            batch.item_count,
            batch.total_amount_minor,
            batch.fees_total_minor,
            batch.status,
            batch.maker_user_id,
            batch.approval_request_id,
            batch.reconciliation_tag,
            batch.created_at,
            batch.submitted_at,
            batch.approved_at,
            batch.dispatched_at,
            batch.completed_at,
            batch.cancelled_at,
            batch.schema_version,
        )
        try:
            self._execute(conn, sql, params)
        except psycopg.errors.UniqueViolation as exc:
            if conn is not None:
                raise ConflictError(
                    "disbursement batch already exists", code="batch_exists"
                ) from exc
            raise ConflictError(
                "disbursement batch already exists", code="batch_exists"
            ) from exc

    def update_batch(
        self,
        batch_id: str,
        *,
        conn: Any | None = None,
        expected_status: str | None = None,
        **changes: object,
    ) -> DisbursementBatch:
        if not changes:
            batch = self.get_batch(batch_id, conn=conn)
            if batch is None:
                raise NotFoundError("disbursement batch not found", code="batch_not_found")
            return batch
        assignments = ", ".join(f"{name} = %s" for name in changes)
        params: list[object] = [*changes.values(), batch_id]
        sql = (
            f"UPDATE disbursement_batches SET {assignments} "  # noqa: S608
            f"WHERE batch_id = %s"
        )
        if expected_status is not None:
            sql += " AND status = %s"
            params.append(expected_status)
        sql += f" RETURNING {_BATCH_COLUMNS}"
        row = self._fetchone(conn, sql, params)
        if row is None:
            if expected_status is not None:
                raise ConflictError("stale batch transition", code="stale_state_transition")
            raise NotFoundError("disbursement batch not found", code="batch_not_found")
        return self._batch(row)

    def get_batch(
        self, batch_id: str, *, conn: Any | None = None, lock: bool = False
    ) -> DisbursementBatch | None:
        sql = f"SELECT {_BATCH_COLUMNS} FROM disbursement_batches WHERE batch_id = %s"
        if lock and conn is not None:
            sql += " FOR UPDATE"
        row = self._fetchone(conn, sql, (batch_id,))
        return None if row is None else self._batch(row)

    def find_batch_by_client_ref(
        self, merchant_id: str, client_batch_ref: str, *, conn: Any | None = None
    ) -> DisbursementBatch | None:
        row = self._fetchone(
            conn,
            f"SELECT {_BATCH_COLUMNS} FROM disbursement_batches "
            "WHERE merchant_id = %s AND client_batch_ref = %s",
            (merchant_id, client_batch_ref),
        )
        return None if row is None else self._batch(row)

    def insert_items(
        self, items: Iterable[DisbursementItem], *, conn: Any | None = None
    ) -> None:
        items_tuple = tuple(items)
        if not items_tuple:
            return
        sql = (
            f"INSERT INTO disbursement_items ({_ITEM_COLUMNS}) VALUES "
            "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
        )
        params = [
            (
                item.item_id,
                item.batch_id,
                item.ordinal,
                item.beneficiary_ref,
                item.beneficiary_name_normalized,
                item.rail,
                item.amount_minor,
                item.fee_minor,
                item.purpose_code,
                item.item_ref,
                item.status,
                item.rail_transaction_id,
                item.settled_at,
                item.screened_at,
                item.screening_list_version,
                item.schema_version,
            )
            for item in items_tuple
        ]
        self._executemany(conn, sql, params)

    def update_item(
        self,
        item_id: str,
        *,
        conn: Any | None = None,
        expected_status: str | None = None,
        **changes: object,
    ) -> DisbursementItem:
        if not changes:
            item = self.get_item(item_id, conn=conn)
            if item is None:
                raise NotFoundError("disbursement item not found", code="item_not_found")
            return item
        assignments = ", ".join(f"{name} = %s" for name in changes)
        params: list[object] = [*changes.values(), item_id]
        sql = (
            f"UPDATE disbursement_items SET {assignments} "  # noqa: S608
            f"WHERE item_id = %s"
        )
        if expected_status is not None:
            sql += " AND status = %s"
            params.append(expected_status)
        sql += f" RETURNING {_ITEM_COLUMNS}"
        row = self._fetchone(conn, sql, params)
        if row is None:
            if expected_status is not None:
                raise ConflictError("stale item transition", code="stale_state_transition")
            raise NotFoundError("disbursement item not found", code="item_not_found")
        return self._item(row)

    def claim_item_for_dispatch(
        self, item_id: str, *, conn: Any | None = None
    ) -> DisbursementItem | None:
        row = self._fetchone(
            conn,
            f"UPDATE disbursement_items SET status = 'DISPATCHING' "  # noqa: S608
            f"WHERE item_id = %s AND status = 'QUEUED' RETURNING {_ITEM_COLUMNS}",
            (item_id,),
        )
        return None if row is None else self._item(row)

    def claim_item_returned(
        self, item_id: str, *, conn: Any | None = None
    ) -> DisbursementItem | None:
        row = self._fetchone(
            conn,
            f"UPDATE disbursement_items SET status = 'RETURNED' "  # noqa: S608
            f"WHERE item_id = %s AND status IN ('DISPATCHED', 'DISPATCHING') "
            f"RETURNING {_ITEM_COLUMNS}",
            (item_id,),
        )
        return None if row is None else self._item(row)

    def get_item(
        self, item_id: str, *, conn: Any | None = None
    ) -> DisbursementItem | None:
        row = self._fetchone(
            conn,
            f"SELECT {_ITEM_COLUMNS} FROM disbursement_items WHERE item_id = %s",
            (item_id,),
        )
        return None if row is None else self._item(row)

    def list_items(
        self, batch_id: str, *, status: str | None = None, conn: Any | None = None
    ) -> tuple[DisbursementItem, ...]:
        sql = f"SELECT {_ITEM_COLUMNS} FROM disbursement_items WHERE batch_id = %s"
        params: list[object] = [batch_id]
        if status is not None:
            sql += " AND status = %s"
            params.append(status)
        sql += " ORDER BY ordinal"
        rows = self._fetchall(conn, sql, params)
        return tuple(self._item(row) for row in rows)

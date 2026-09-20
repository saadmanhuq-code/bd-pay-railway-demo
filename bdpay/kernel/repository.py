"""Payment storage — repository protocol, in-memory and Postgres stores.

Storage pattern per IMPLEMENTATION.md: a repository Protocol with TWO
implementations — deterministic in-memory (unit tests, no infrastructure)
and psycopg3 matching ``db/migrations/0032..0034`` exactly (tests marked
``@pytest.mark.integration``).

The Postgres store keeps every statement inside the caller's transaction
when a ``conn`` is supplied, composing with the spec/00 §8 atomic
journal+audit+outbox rule.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any, Protocol, runtime_checkable

from bdpay.kernel.models import PaymentAttemptRecord, PaymentIntentRecord, RefundRecord
from bdpay.kernel.payment_states import INTENT_TABLE
from bdpay.platform.errors import ConflictError, NotFoundError

__all__ = [
    "InMemoryPaymentStore",
    "PaymentStore",
    "PostgresPaymentStore",
]

#: Intent states swept by the TTL task (spec/02 TTL policy).
#: REQUIRES_CAPTURE is included because ``expires_at`` is repointed to
#: ``auth_expires_at`` (default 7d) when the intent enters REQUIRES_CAPTURE,
#: so the single sweep query picks it up at the correct auth-hold deadline
#: — not the original 30-min payment TTL.
TTL_SWEEP_STATES = (
    "CREATED",
    "REQUIRES_PAYMENT_METHOD",
    "REQUIRES_CONFIRMATION",
    "PRE_FLIGHT",
    "REQUIRES_ACTION",
    "PROCESSING",
    "REQUIRES_CAPTURE",
)


@runtime_checkable
class PaymentStore(Protocol):
    """Storage contract for intents, attempts and refunds."""

    # -- intents -------------------------------------------------------------

    def insert_intent(self, record: PaymentIntentRecord, *, conn: Any | None = None) -> None: ...

    def get_intent(
        self, payment_intent_id: str, *, conn: Any | None = None
    ) -> PaymentIntentRecord | None: ...

    def lock_intent(
        self, payment_intent_id: str, *, conn: Any | None = None
    ) -> PaymentIntentRecord | None: ...

    def update_intent(
        self,
        record: PaymentIntentRecord,
        *,
        conn: Any | None = None,
        expected_status: str | None = None,
    ) -> None: ...

    def find_intent_by_idempotency_key(
        self, merchant_id: str, idempotency_key: str, *, conn: Any | None = None
    ) -> PaymentIntentRecord | None: ...

    def list_expired_intents(
        self, *, now: datetime, conn: Any | None = None
    ) -> list[PaymentIntentRecord]: ...

    # -- attempts ------------------------------------------------------------

    def insert_attempt(self, record: PaymentAttemptRecord, *, conn: Any | None = None) -> None: ...

    def get_attempt(
        self, attempt_id: str, *, conn: Any | None = None
    ) -> PaymentAttemptRecord | None: ...

    def find_attempt_by_connector_ref(
        self, connector_id: str, connector_ref: str, *, conn: Any | None = None
    ) -> PaymentAttemptRecord | None: ...

    def update_attempt(
        self,
        record: PaymentAttemptRecord,
        *,
        conn: Any | None = None,
        expected_status: str | None = None,
    ) -> None: ...

    def list_attempts(
        self, payment_intent_id: str, *, conn: Any | None = None
    ) -> list[PaymentAttemptRecord]: ...

    # -- refunds ---------------------------------------------------------------

    def insert_refund(self, record: RefundRecord, *, conn: Any | None = None) -> None: ...

    def get_refund(self, refund_id: str, *, conn: Any | None = None) -> RefundRecord | None: ...

    def find_refund_by_idempotency_key(
        self, payment_intent_id: str, idempotency_key: str, *, conn: Any | None = None
    ) -> RefundRecord | None: ...

    def find_refund_by_connector_ref(
        self, connector_id: str, connector_ref: str, *, conn: Any | None = None
    ) -> RefundRecord | None: ...

    def update_refund(
        self,
        record: RefundRecord,
        *,
        conn: Any | None = None,
        expected_status: str | None = None,
    ) -> None: ...

    def list_refunds(
        self, payment_intent_id: str, *, conn: Any | None = None
    ) -> list[RefundRecord]: ...

    def list_refunds_for_merchant(
        self,
        merchant_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
        *,
        conn: Any | None = None,
    ) -> list[RefundRecord]: ...

    def list_intents(
        self,
        merchant_id: str | None = None,
        customer_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
        *,
        conn: Any | None = None,
    ) -> list[PaymentIntentRecord]: ...

    def list_refunds_for_customer(
        self,
        customer_id: str,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
        *,
        conn: Any | None = None,
    ) -> list[RefundRecord]: ...

    def list_charged_since(
        self,
        since: datetime,
        *,
        conn: Any | None = None,
    ) -> list[tuple[PaymentIntentRecord, PaymentAttemptRecord]]:
        """Return (intent, attempt) pairs for all CHARGED attempts since ``since``.

        Used by ``TransactionMonitor`` to rehydrate its rolling-window history
        on startup so monitoring windows are not blind after a restart.  The
        caller must bound ``since`` to the longest monitoring window (72 h for
        structuring detection) to avoid an unbounded scan.
        """
        ...


class InMemoryPaymentStore:
    """Deterministic in-memory store for unit tests."""

    def __init__(self) -> None:
        self._intents: dict[str, PaymentIntentRecord] = {}
        self._attempts: dict[str, PaymentAttemptRecord] = {}
        self._refunds: dict[str, RefundRecord] = {}

    # -- intents -------------------------------------------------------------

    def insert_intent(self, record: PaymentIntentRecord, *, conn: Any | None = None) -> None:
        if record.payment_intent_id in self._intents:
            raise ConflictError(
                f"intent {record.payment_intent_id} already exists", code="duplicate_intent"
            )
        if record.idempotency_key is not None:
            existing = self.find_intent_by_idempotency_key(
                record.merchant_id, record.idempotency_key
            )
            if existing is not None:
                raise ConflictError(
                    "idempotency_key already used for this merchant",
                    code="duplicate_idempotency_key",
                )
        self._intents[record.payment_intent_id] = record

    def get_intent(
        self, payment_intent_id: str, *, conn: Any | None = None
    ) -> PaymentIntentRecord | None:
        return self._intents.get(payment_intent_id)

    def lock_intent(
        self, payment_intent_id: str, *, conn: Any | None = None
    ) -> PaymentIntentRecord | None:
        return self.get_intent(payment_intent_id)

    def update_intent(
        self,
        record: PaymentIntentRecord,
        *,
        conn: Any | None = None,
        expected_status: str | None = None,
    ) -> None:
        current = self._intents.get(record.payment_intent_id)
        if current is None:
            raise NotFoundError(f"unknown intent {record.payment_intent_id}")
        if expected_status is not None and current.status != expected_status:
            raise ConflictError(
                "stale payment intent transition",
                code="stale_state_transition",
            )
        self._intents[record.payment_intent_id] = record

    def find_intent_by_idempotency_key(
        self, merchant_id: str, idempotency_key: str, *, conn: Any | None = None
    ) -> PaymentIntentRecord | None:
        for record in self._intents.values():
            if record.merchant_id == merchant_id and record.idempotency_key == idempotency_key:
                return record
        return None

    def list_expired_intents(
        self, *, now: datetime, conn: Any | None = None
    ) -> list[PaymentIntentRecord]:
        return sorted(
            (
                r
                for r in self._intents.values()
                if r.status in TTL_SWEEP_STATES and r.expires_at <= now
            ),
            key=lambda r: r.created_at,
        )

    def list_intents(
        self,
        merchant_id: str | None = None,
        customer_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
        *,
        conn: Any | None = None,
    ) -> list[PaymentIntentRecord]:
        results = (
            r
            for r in self._intents.values()
            if (merchant_id is None or r.merchant_id == merchant_id)
            and (customer_id is None or r.customer_id == customer_id)
            and (status is None or r.status == status)
        )
        sorted_results = sorted(results, key=lambda r: (r.created_at, r.payment_intent_id))
        return sorted_results[offset : offset + limit]

    def list_refunds_for_customer(
        self,
        customer_id: str,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
        *,
        conn: Any | None = None,
    ) -> list[RefundRecord]:
        # Refunds carry no customer_id; ownership is derived from the parent intent.
        def _owned(r: RefundRecord) -> bool:
            intent = self._intents.get(r.payment_intent_id)
            return intent is not None and intent.customer_id == customer_id

        results = (
            r
            for r in self._refunds.values()
            if _owned(r) and (status is None or r.status == status)
        )
        sorted_results = sorted(results, key=lambda r: (r.created_at, r.refund_id))
        return sorted_results[offset : offset + limit]

    # -- attempts ------------------------------------------------------------

    def insert_attempt(self, record: PaymentAttemptRecord, *, conn: Any | None = None) -> None:
        if record.attempt_id in self._attempts:
            raise ConflictError(f"attempt {record.attempt_id} already exists")
        if any(
            a.connector_ref == record.connector_ref for a in self._attempts.values()
        ):
            raise ConflictError("connector_ref must be unique", code="duplicate_connector_ref")
        self._attempts[record.attempt_id] = record
        intent = self._intents.get(record.payment_intent_id)
        if intent is not None:
            self._intents[record.payment_intent_id] = replace(
                intent, latest_attempt_id=record.attempt_id
            )

    def get_attempt(
        self, attempt_id: str, *, conn: Any | None = None
    ) -> PaymentAttemptRecord | None:
        return self._attempts.get(attempt_id)

    def find_attempt_by_connector_ref(
        self, connector_id: str, connector_ref: str, *, conn: Any | None = None
    ) -> PaymentAttemptRecord | None:
        for attempt in self._attempts.values():
            if attempt.connector_id == connector_id and attempt.connector_ref == connector_ref:
                return attempt
        return None

    def update_attempt(
        self,
        record: PaymentAttemptRecord,
        *,
        conn: Any | None = None,
        expected_status: str | None = None,
    ) -> None:
        current = self._attempts.get(record.attempt_id)
        if current is None:
            raise NotFoundError(f"unknown attempt {record.attempt_id}")
        if expected_status is not None and current.status != expected_status:
            raise ConflictError(
                "stale payment attempt transition",
                code="stale_state_transition",
            )
        self._attempts[record.attempt_id] = record

    def list_attempts(
        self, payment_intent_id: str, *, conn: Any | None = None
    ) -> list[PaymentAttemptRecord]:
        return sorted(
            (a for a in self._attempts.values() if a.payment_intent_id == payment_intent_id),
            key=lambda a: a.attempt_number,
        )

    def list_charged_since(
        self,
        since: datetime,
        *,
        conn: Any | None = None,
    ) -> list[tuple[PaymentIntentRecord, PaymentAttemptRecord]]:
        result: list[tuple[PaymentIntentRecord, PaymentAttemptRecord]] = []
        for attempt in self._attempts.values():
            if attempt.charged_at is not None and attempt.charged_at >= since:
                intent = self._intents.get(attempt.payment_intent_id)
                if intent is not None:
                    result.append((intent, attempt))
        return sorted(result, key=lambda pair: pair[1].charged_at)  # type: ignore[arg-type]

    # -- refunds ---------------------------------------------------------------

    def insert_refund(self, record: RefundRecord, *, conn: Any | None = None) -> None:
        if record.refund_id in self._refunds:
            raise ConflictError(f"refund {record.refund_id} already exists")
        if record.idempotency_key is not None:
            existing = self.find_refund_by_idempotency_key(
                record.payment_intent_id, record.idempotency_key
            )
            if existing is not None:
                raise ConflictError(
                    "idempotency_key already used for this refund",
                    code="duplicate_idempotency_key",
                )
        self._refunds[record.refund_id] = record

    def get_refund(self, refund_id: str, *, conn: Any | None = None) -> RefundRecord | None:
        return self._refunds.get(refund_id)

    def find_refund_by_idempotency_key(
        self, payment_intent_id: str, idempotency_key: str, *, conn: Any | None = None
    ) -> RefundRecord | None:
        for refund in self._refunds.values():
            if (
                refund.payment_intent_id == payment_intent_id
                and refund.idempotency_key == idempotency_key
            ):
                return refund
        return None

    def find_refund_by_connector_ref(
        self, connector_id: str, connector_ref: str, *, conn: Any | None = None
    ) -> RefundRecord | None:
        for refund in self._refunds.values():
            if refund.connector_id == connector_id and refund.connector_ref == connector_ref:
                return refund
        return None

    def update_refund(
        self,
        record: RefundRecord,
        *,
        conn: Any | None = None,
        expected_status: str | None = None,
    ) -> None:
        current = self._refunds.get(record.refund_id)
        if current is None:
            raise NotFoundError(f"unknown refund {record.refund_id}")
        if expected_status is not None and current.status != expected_status:
            raise ConflictError(
                "stale refund transition",
                code="stale_state_transition",
            )
        self._refunds[record.refund_id] = record

    def list_refunds(
        self, payment_intent_id: str, *, conn: Any | None = None
    ) -> list[RefundRecord]:
        return sorted(
            (r for r in self._refunds.values() if r.payment_intent_id == payment_intent_id),
            key=lambda r: r.created_at,
        )

    def list_refunds_for_merchant(
        self,
        merchant_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
        *,
        conn: Any | None = None,
    ) -> list[RefundRecord]:
        # Refunds carry no merchant_id; ownership is derived from the parent
        # intent (refunds -> payment_intents -> merchant_id). Mirrors the
        # Postgres JOIN so in-memory and PG paths agree.
        def _owned(r: RefundRecord) -> bool:
            if merchant_id is None:
                return True
            intent = self._intents.get(r.payment_intent_id)
            return intent is not None and intent.merchant_id == merchant_id

        results = (
            r
            for r in self._refunds.values()
            if _owned(r) and (status is None or r.status == status)
        )
        sorted_results = sorted(results, key=lambda r: (r.created_at, r.refund_id))
        return sorted_results[offset : offset + limit]


# ---------------------------------------------------------------------------
# Postgres implementation (migrations 0032-0034)
# ---------------------------------------------------------------------------

_INTENT_COLUMNS = (
    "payment_intent_id, merchant_id, customer_id, amount_minor, currency, method, "
    "capture_method, status, description, statement_descriptor, metadata, "
    "idempotency_key, client_secret_hash, latest_attempt_id, succeeded_at, "
    "delivery_confirmed_at, cancelled_at, reversed_at, expires_at, created_at, "
    "updated_at, schema_version"
)

_ATTEMPT_COLUMNS = (
    "attempt_id, payment_intent_id, attempt_number, connector_id, instruction_id, "
    "connector_ref, status, amount_minor, currency, rail_transaction_id, error_code, "
    "raw_response_hash, poll_count, authorized_at, auth_expires_at, charged_at, "
    "failed_at, created_at, updated_at, schema_version"
)

_REFUND_COLUMNS = (
    "refund_id, payment_intent_id, attempt_id, amount_minor, currency, reason, status, "
    "connector_id, connector_ref, instruction_id, rail_transaction_id, "
    "raw_response_hash, retry_count, idempotency_key, metadata, succeeded_at, "
    "failed_at, created_at, updated_at, "
    "schema_version"
)


def _jsonb(payload: Mapping[str, str]) -> Any:
    from psycopg.types.json import Jsonb

    from bdpay.platform.canonical import canonical_json

    return Jsonb(dict(payload), dumps=lambda o: canonical_json(o).decode("utf-8"))


class PostgresPaymentStore:
    """psycopg3 store matching migrations 0032-0034 exactly.

    ``connection_factory`` returns a new psycopg connection; when the caller
    passes ``conn`` the statement joins that transaction instead (atomic with
    journal/audit/outbox writes).
    """

    def __init__(self, connection_factory: Callable[[], Any]) -> None:
        self._connect = connection_factory

    def _execute(self, conn: Any | None, sql: str, params: tuple) -> None:
        if conn is not None:
            conn.execute(sql, params)
            return
        with self._connect() as owned:
            owned.execute(sql, params)
            owned.commit()

    def _execute_update(
        self,
        conn: Any | None,
        sql: str,
        params: tuple,
        *,
        stale_message: str | None = None,
    ) -> None:
        if conn is not None:
            cur = conn.execute(sql, params)
            if stale_message is not None and cur.rowcount != 1:
                raise ConflictError(stale_message, code="stale_state_transition")
            return
        with self._connect() as owned:
            cur = owned.execute(sql, params)
            if stale_message is not None and cur.rowcount != 1:
                raise ConflictError(stale_message, code="stale_state_transition")
            owned.commit()

    def _fetchone(self, conn: Any | None, sql: str, params: tuple) -> Mapping[str, Any] | None:
        from psycopg.rows import dict_row

        if conn is not None:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(sql, params)
                return cur.fetchone()
        with self._connect() as owned:
            with owned.cursor(row_factory=dict_row) as cur:
                cur.execute(sql, params)
                return cur.fetchone()

    def _fetchall(self, conn: Any | None, sql: str, params: tuple) -> list[Mapping[str, Any]]:
        from psycopg.rows import dict_row

        if conn is not None:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(sql, params)
                return cur.fetchall()
        with self._connect() as owned:
            with owned.cursor(row_factory=dict_row) as cur:
                cur.execute(sql, params)
                return cur.fetchall()

    # -- intents -------------------------------------------------------------

    @staticmethod
    def _row_to_intent(row: Mapping[str, Any]) -> PaymentIntentRecord:
        return PaymentIntentRecord(
            payment_intent_id=row["payment_intent_id"],
            merchant_id=row["merchant_id"],
            customer_id=row["customer_id"],
            amount_minor=row["amount_minor"],
            currency=row["currency"].strip(),
            method=row["method"],
            capture_method=row["capture_method"],
            status=row["status"],
            description=row["description"],
            statement_descriptor=row["statement_descriptor"],
            metadata=row["metadata"],
            idempotency_key=row["idempotency_key"],
            client_secret_hash=row["client_secret_hash"],
            latest_attempt_id=row["latest_attempt_id"],
            succeeded_at=row["succeeded_at"],
            delivery_confirmed_at=row["delivery_confirmed_at"],
            cancelled_at=row["cancelled_at"],
            reversed_at=row["reversed_at"],
            expires_at=row["expires_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            schema_version=row["schema_version"],
        )

    def insert_intent(self, record: PaymentIntentRecord, *, conn: Any | None = None) -> None:
        sql = (
            f"INSERT INTO payment_intents ({_INTENT_COLUMNS}) VALUES "
            "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
            "%s, %s, %s, %s)"
        )
        self._execute(
            conn,
            sql,
            (
                record.payment_intent_id,
                record.merchant_id,
                record.customer_id,
                record.amount_minor,
                record.currency,
                record.method,
                record.capture_method,
                record.status,
                record.description,
                record.statement_descriptor,
                _jsonb(record.metadata),
                record.idempotency_key,
                record.client_secret_hash,
                record.latest_attempt_id,
                record.succeeded_at,
                record.delivery_confirmed_at,
                record.cancelled_at,
                record.reversed_at,
                record.expires_at,
                record.created_at,
                record.updated_at,
                record.schema_version,
            ),
        )

    def get_intent(
        self, payment_intent_id: str, *, conn: Any | None = None
    ) -> PaymentIntentRecord | None:
        row = self._fetchone(
            conn,
            f"SELECT {_INTENT_COLUMNS} FROM payment_intents WHERE payment_intent_id = %s",
            (payment_intent_id,),
        )
        return None if row is None else self._row_to_intent(row)

    def lock_intent(
        self, payment_intent_id: str, *, conn: Any | None = None
    ) -> PaymentIntentRecord | None:
        if conn is None:
            return self.get_intent(payment_intent_id)
        row = self._fetchone(
            conn,
            f"SELECT {_INTENT_COLUMNS} FROM payment_intents "
            "WHERE payment_intent_id = %s FOR UPDATE",
            (payment_intent_id,),
        )
        return None if row is None else self._row_to_intent(row)

    def update_intent(
        self,
        record: PaymentIntentRecord,
        *,
        conn: Any | None = None,
        expected_status: str | None = None,
    ) -> None:
        sql = (
            "UPDATE payment_intents SET status = %s, amount_minor = %s, "
            "latest_attempt_id = %s, succeeded_at = %s, delivery_confirmed_at = %s, "
            "cancelled_at = %s, reversed_at = %s, updated_at = %s, expires_at = %s "
            "WHERE payment_intent_id = %s"
        )
        params: tuple[Any, ...] = (
            record.status,
            record.amount_minor,
            record.latest_attempt_id,
            record.succeeded_at,
            record.delivery_confirmed_at,
            record.cancelled_at,
            record.reversed_at,
            record.updated_at,
            record.expires_at,
            record.payment_intent_id,
        )
        stale_message = None
        if expected_status is not None:
            sql += " AND status = %s"
            params = (*params, expected_status)
            stale_message = "stale payment intent transition"
        self._execute_update(conn, sql, params, stale_message=stale_message)

    def find_intent_by_idempotency_key(
        self, merchant_id: str, idempotency_key: str, *, conn: Any | None = None
    ) -> PaymentIntentRecord | None:
        row = self._fetchone(
            conn,
            f"SELECT {_INTENT_COLUMNS} FROM payment_intents "
            "WHERE merchant_id = %s AND idempotency_key = %s",
            (merchant_id, idempotency_key),
        )
        return None if row is None else self._row_to_intent(row)

    def list_expired_intents(
        self, *, now: datetime, conn: Any | None = None
    ) -> list[PaymentIntentRecord]:
        rows = self._fetchall(
            conn,
            f"SELECT {_INTENT_COLUMNS} FROM payment_intents "
            "WHERE status = ANY(%s) AND expires_at <= %s ORDER BY created_at",
            (list(TTL_SWEEP_STATES), now),
        )
        return [self._row_to_intent(row) for row in rows]

    def list_intents(
        self,
        merchant_id: str | None = None,
        customer_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
        *,
        conn: Any | None = None,
    ) -> list[PaymentIntentRecord]:
        conditions: list[str] = []
        params: list[Any] = []
        if merchant_id is not None:
            conditions.append("merchant_id = %s")
            params.append(merchant_id)
        if customer_id is not None:
            conditions.append("customer_id = %s")
            params.append(customer_id)
        if status is not None:
            conditions.append("status = %s")
            params.append(status)
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        params.extend([limit, offset])
        sql = (
            f"SELECT {_INTENT_COLUMNS} FROM payment_intents "
            f"{where} ORDER BY created_at, payment_intent_id "
            "LIMIT %s OFFSET %s"
        )
        rows = self._fetchall(conn, sql, tuple(params))
        return [self._row_to_intent(row) for row in rows]

    def list_refunds_for_customer(
        self,
        customer_id: str,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
        *,
        conn: Any | None = None,
    ) -> list[RefundRecord]:
        # The refunds table has no customer_id; scope is derived by joining to
        # the parent intent. Columns are qualified with the ``r.`` alias because
        # payment_intent_id exists on both tables.
        cols = ", ".join(f"r.{c.strip()}" for c in _REFUND_COLUMNS.split(","))
        conditions: list[str] = ["pi.customer_id = %s"]
        params: list[Any] = [customer_id]
        if status is not None:
            conditions.append("r.status = %s")
            params.append(status)
        where = "WHERE " + " AND ".join(conditions)
        params.extend([limit, offset])
        sql = (
            f"SELECT {cols} FROM refunds r "
            "JOIN payment_intents pi ON r.payment_intent_id = pi.payment_intent_id "
            f"{where} ORDER BY r.created_at, r.refund_id "
            "LIMIT %s OFFSET %s"
        )
        rows = self._fetchall(conn, sql, tuple(params))
        return [self._row_to_refund(row) for row in rows]

    def list_charged_since(
        self,
        since: datetime,
        *,
        conn: Any | None = None,
    ) -> list[tuple[PaymentIntentRecord, PaymentAttemptRecord]]:
        """Return (intent, attempt) pairs for all charged attempts since ``since``.

        Joined so the monitor rehydration path avoids N+1 intent lookups.
        Ordered by charged_at ascending so observations are replayed in
        chronological order — the monitor's window evaluations depend on order.
        """
        # Use table aliases to disambiguate columns shared between the two
        # tables (amount_minor, currency, created_at, updated_at,
        # schema_version) by fetching each table's columns under their own
        # SELECT-list so _row_to_intent / _row_to_attempt still work.
        from psycopg.rows import dict_row

        sql = (
            "SELECT "
            # intent columns — prefixed with i_
            "pi.payment_intent_id AS i_payment_intent_id, "
            "pi.merchant_id AS i_merchant_id, "
            "pi.customer_id AS i_customer_id, "
            "pi.amount_minor AS i_amount_minor, "
            "pi.currency AS i_currency, "
            "pi.method AS i_method, "
            "pi.capture_method AS i_capture_method, "
            "pi.status AS i_status, "
            "pi.description AS i_description, "
            "pi.statement_descriptor AS i_statement_descriptor, "
            "pi.metadata AS i_metadata, "
            "pi.idempotency_key AS i_idempotency_key, "
            "pi.client_secret_hash AS i_client_secret_hash, "
            "pi.latest_attempt_id AS i_latest_attempt_id, "
            "pi.succeeded_at AS i_succeeded_at, "
            "pi.delivery_confirmed_at AS i_delivery_confirmed_at, "
            "pi.cancelled_at AS i_cancelled_at, "
            "pi.reversed_at AS i_reversed_at, "
            "pi.expires_at AS i_expires_at, "
            "pi.created_at AS i_created_at, "
            "pi.updated_at AS i_updated_at, "
            "pi.schema_version AS i_schema_version, "
            # attempt columns — prefixed with a_
            "pa.attempt_id AS a_attempt_id, "
            "pa.payment_intent_id AS a_payment_intent_id, "
            "pa.attempt_number AS a_attempt_number, "
            "pa.connector_id AS a_connector_id, "
            "pa.instruction_id AS a_instruction_id, "
            "pa.connector_ref AS a_connector_ref, "
            "pa.status AS a_status, "
            "pa.amount_minor AS a_amount_minor, "
            "pa.currency AS a_currency, "
            "pa.rail_transaction_id AS a_rail_transaction_id, "
            "pa.error_code AS a_error_code, "
            "pa.raw_response_hash AS a_raw_response_hash, "
            "pa.poll_count AS a_poll_count, "
            "pa.authorized_at AS a_authorized_at, "
            "pa.auth_expires_at AS a_auth_expires_at, "
            "pa.charged_at AS a_charged_at, "
            "pa.failed_at AS a_failed_at, "
            "pa.created_at AS a_created_at, "
            "pa.updated_at AS a_updated_at, "
            "pa.schema_version AS a_schema_version "
            "FROM payment_attempts pa "
            "JOIN payment_intents pi ON pi.payment_intent_id = pa.payment_intent_id "
            "WHERE pa.charged_at >= %s "
            "ORDER BY pa.charged_at ASC"
        )

        def _split(row: dict) -> tuple[PaymentIntentRecord, PaymentAttemptRecord]:
            intent = self._row_to_intent(
                {k[2:]: v for k, v in row.items() if k.startswith("i_")}
            )
            attempt = self._row_to_attempt(
                {k[2:]: v for k, v in row.items() if k.startswith("a_")}
            )
            return intent, attempt

        if conn is not None:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(sql, (since,))
                rows = cur.fetchall()
        else:
            with self._connect() as owned:
                with owned.cursor(row_factory=dict_row) as cur:
                    cur.execute(sql, (since,))
                    rows = cur.fetchall()
        return [_split(row) for row in rows]

    # -- attempts ------------------------------------------------------------

    @staticmethod
    def _row_to_attempt(row: Mapping[str, Any]) -> PaymentAttemptRecord:
        return PaymentAttemptRecord(
            attempt_id=row["attempt_id"],
            payment_intent_id=row["payment_intent_id"],
            attempt_number=row["attempt_number"],
            connector_id=row["connector_id"],
            instruction_id=row["instruction_id"],
            connector_ref=row["connector_ref"],
            status=row["status"],
            amount_minor=row["amount_minor"],
            currency=row["currency"].strip(),
            rail_transaction_id=row["rail_transaction_id"],
            error_code=row["error_code"],
            raw_response_hash=row["raw_response_hash"],
            poll_count=row["poll_count"],
            authorized_at=row["authorized_at"],
            auth_expires_at=row["auth_expires_at"],
            charged_at=row["charged_at"],
            failed_at=row["failed_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            schema_version=row["schema_version"],
        )

    def insert_attempt(self, record: PaymentAttemptRecord, *, conn: Any | None = None) -> None:
        if conn is None:
            with self._connect() as owned:
                with owned.transaction():
                    self.insert_attempt(record, conn=owned)
            return
        sql = (
            f"INSERT INTO payment_attempts ({_ATTEMPT_COLUMNS}) VALUES "
            "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
            "%s, %s)"
        )
        self._execute(
            conn,
            sql,
            (
                record.attempt_id,
                record.payment_intent_id,
                record.attempt_number,
                record.connector_id,
                record.instruction_id,
                record.connector_ref,
                record.status,
                record.amount_minor,
                record.currency,
                record.rail_transaction_id,
                record.error_code,
                record.raw_response_hash,
                record.poll_count,
                record.authorized_at,
                record.auth_expires_at,
                record.charged_at,
                record.failed_at,
                record.created_at,
                record.updated_at,
                record.schema_version,
            ),
        )
        self._execute(
            conn,
            "UPDATE payment_intents SET latest_attempt_id = %s, updated_at = %s "
            "WHERE payment_intent_id = %s",
            (record.attempt_id, record.updated_at, record.payment_intent_id),
        )

    def get_attempt(
        self, attempt_id: str, *, conn: Any | None = None
    ) -> PaymentAttemptRecord | None:
        row = self._fetchone(
            conn,
            f"SELECT {_ATTEMPT_COLUMNS} FROM payment_attempts WHERE attempt_id = %s",
            (attempt_id,),
        )
        return None if row is None else self._row_to_attempt(row)

    def find_attempt_by_connector_ref(
        self, connector_id: str, connector_ref: str, *, conn: Any | None = None
    ) -> PaymentAttemptRecord | None:
        row = self._fetchone(
            conn,
            f"SELECT {_ATTEMPT_COLUMNS} FROM payment_attempts "
            "WHERE connector_id = %s AND connector_ref = %s",
            (connector_id, connector_ref),
        )
        return None if row is None else self._row_to_attempt(row)

    def update_attempt(
        self,
        record: PaymentAttemptRecord,
        *,
        conn: Any | None = None,
        expected_status: str | None = None,
    ) -> None:
        sql = (
            "UPDATE payment_attempts SET status = %s, rail_transaction_id = %s, "
            "error_code = %s, raw_response_hash = %s, poll_count = %s, "
            "authorized_at = %s, auth_expires_at = %s, charged_at = %s, failed_at = %s, "
            "updated_at = %s WHERE attempt_id = %s"
        )
        params: tuple[Any, ...] = (
            record.status,
            record.rail_transaction_id,
            record.error_code,
            record.raw_response_hash,
            record.poll_count,
            record.authorized_at,
            record.auth_expires_at,
            record.charged_at,
            record.failed_at,
            record.updated_at,
            record.attempt_id,
        )
        stale_message = None
        if expected_status is not None:
            sql += " AND status = %s"
            params = (*params, expected_status)
            stale_message = "stale payment attempt transition"
        self._execute_update(conn, sql, params, stale_message=stale_message)

    def list_attempts(
        self, payment_intent_id: str, *, conn: Any | None = None
    ) -> list[PaymentAttemptRecord]:
        rows = self._fetchall(
            conn,
            f"SELECT {_ATTEMPT_COLUMNS} FROM payment_attempts "
            "WHERE payment_intent_id = %s ORDER BY attempt_number",
            (payment_intent_id,),
        )
        return [self._row_to_attempt(row) for row in rows]

    # -- refunds ---------------------------------------------------------------

    @staticmethod
    def _row_to_refund(row: Mapping[str, Any]) -> RefundRecord:
        return RefundRecord(
            refund_id=row["refund_id"],
            payment_intent_id=row["payment_intent_id"],
            attempt_id=row["attempt_id"],
            amount_minor=row["amount_minor"],
            currency=row["currency"].strip(),
            reason=row["reason"],
            status=row["status"],
            connector_id=row["connector_id"],
            connector_ref=row["connector_ref"],
            instruction_id=row["instruction_id"],
            rail_transaction_id=row["rail_transaction_id"],
            raw_response_hash=row["raw_response_hash"],
            retry_count=row["retry_count"],
            idempotency_key=row["idempotency_key"],
            metadata=row["metadata"],
            succeeded_at=row["succeeded_at"],
            failed_at=row["failed_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            schema_version=row["schema_version"],
        )

    def insert_refund(self, record: RefundRecord, *, conn: Any | None = None) -> None:
        sql = (
            f"INSERT INTO refunds ({_REFUND_COLUMNS}) VALUES "
            "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
        )
        self._execute(
            conn,
            sql,
            (
                record.refund_id,
                record.payment_intent_id,
                record.attempt_id,
                record.amount_minor,
                record.currency,
                record.reason,
                record.status,
                record.connector_id,
                record.connector_ref,
                record.instruction_id,
                record.rail_transaction_id,
                record.raw_response_hash,
                record.retry_count,
                record.idempotency_key,
                _jsonb(record.metadata),
                record.succeeded_at,
                record.failed_at,
                record.created_at,
                record.updated_at,
                record.schema_version,
            ),
        )

    def get_refund(self, refund_id: str, *, conn: Any | None = None) -> RefundRecord | None:
        row = self._fetchone(
            conn,
            f"SELECT {_REFUND_COLUMNS} FROM refunds WHERE refund_id = %s",
            (refund_id,),
        )
        return None if row is None else self._row_to_refund(row)

    def find_refund_by_idempotency_key(
        self, payment_intent_id: str, idempotency_key: str, *, conn: Any | None = None
    ) -> RefundRecord | None:
        row = self._fetchone(
            conn,
            f"SELECT {_REFUND_COLUMNS} FROM refunds "
            "WHERE payment_intent_id = %s AND idempotency_key = %s",
            (payment_intent_id, idempotency_key),
        )
        return None if row is None else self._row_to_refund(row)

    def find_refund_by_connector_ref(
        self, connector_id: str, connector_ref: str, *, conn: Any | None = None
    ) -> RefundRecord | None:
        row = self._fetchone(
            conn,
            f"SELECT {_REFUND_COLUMNS} FROM refunds "
            "WHERE connector_id = %s AND connector_ref = %s",
            (connector_id, connector_ref),
        )
        return None if row is None else self._row_to_refund(row)

    def update_refund(
        self,
        record: RefundRecord,
        *,
        conn: Any | None = None,
        expected_status: str | None = None,
    ) -> None:
        sql = (
            "UPDATE refunds SET status = %s, connector_id = %s, connector_ref = %s, "
            "instruction_id = %s, rail_transaction_id = %s, raw_response_hash = %s, "
            "retry_count = %s, idempotency_key = %s, succeeded_at = %s, "
            "failed_at = %s, updated_at = %s "
            "WHERE refund_id = %s"
        )
        params: tuple[Any, ...] = (
            record.status,
            record.connector_id,
            record.connector_ref,
            record.instruction_id,
            record.rail_transaction_id,
            record.raw_response_hash,
            record.retry_count,
            record.idempotency_key,
            record.succeeded_at,
            record.failed_at,
            record.updated_at,
            record.refund_id,
        )
        stale_message = None
        if expected_status is not None:
            sql += " AND status = %s"
            params = (*params, expected_status)
            stale_message = "stale refund transition"
        self._execute_update(conn, sql, params, stale_message=stale_message)

    def list_refunds(
        self, payment_intent_id: str, *, conn: Any | None = None
    ) -> list[RefundRecord]:
        rows = self._fetchall(
            conn,
            f"SELECT {_REFUND_COLUMNS} FROM refunds "
            "WHERE payment_intent_id = %s ORDER BY created_at",
            (payment_intent_id,),
        )
        return [self._row_to_refund(row) for row in rows]

    def list_refunds_for_merchant(
        self,
        merchant_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
        *,
        conn: Any | None = None,
    ) -> list[RefundRecord]:
        # The refunds table has no merchant_id; scope is derived by joining to
        # the parent intent. Columns are qualified with the ``r.`` alias because
        # payment_intent_id exists on both tables.
        cols = ", ".join(f"r.{c.strip()}" for c in _REFUND_COLUMNS.split(","))
        conditions: list[str] = []
        params: list[Any] = []
        if merchant_id is not None:
            conditions.append("pi.merchant_id = %s")
            params.append(merchant_id)
        if status is not None:
            conditions.append("r.status = %s")
            params.append(status)
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        params.extend([limit, offset])
        sql = (
            f"SELECT {cols} FROM refunds r "
            "JOIN payment_intents pi ON r.payment_intent_id = pi.payment_intent_id "
            f"{where} ORDER BY r.created_at, r.refund_id "
            "LIMIT %s OFFSET %s"
        )
        rows = self._fetchall(conn, sql, tuple(params))
        return [self._row_to_refund(row) for row in rows]


def intent_ttl_default(created_at: datetime) -> datetime:
    """spec/02: ``expires_at = created_at + 30 min`` for new intents."""
    return created_at + timedelta(minutes=30)


# Re-exported for orchestrator/tests convenience.
INTENT_STATES = INTENT_TABLE.states

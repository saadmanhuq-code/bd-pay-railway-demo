"""Subscription storage records and repositories (spec/17 VX3)."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Protocol, runtime_checkable

from bdpay.kernel.models import validate_metadata
from bdpay.kernel.subscriptions.states import SUBSCRIPTION_CYCLE_TABLE, SUBSCRIPTION_TABLE
from bdpay.platform.errors import ConflictError, InvalidRequestError, NotFoundError

__all__ = [
    "DunningAttemptRecord",
    "InMemorySubscriptionStore",
    "PostgresSubscriptionStore",
    "SubscriptionCycleRecord",
    "SubscriptionRecord",
    "SubscriptionStore",
]

INTERVALS = ("WEEKLY", "MONTHLY")
COLLECTIONS = ("LINK", "MANDATE")


def _require_positive_int(value: object, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidRequestError(f"{what} must be integer paisa", code="invalid_amount")
    if value <= 0:
        raise InvalidRequestError(f"{what} must be > 0", code="invalid_amount")
    return value


def _require_aware(value: datetime, what: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise InvalidRequestError(f"{what} must be timezone-aware")
    return value


@dataclass(frozen=True)
class SubscriptionRecord:
    """One ``subscriptions`` row."""

    subscription_id: str
    merchant_id: str
    customer_ref: str
    plan_amount_minor: int
    interval: str
    anchor_date: date
    collection: str
    status: str
    created_at: datetime
    updated_at: datetime
    created_idempotency_key: str
    currency: str = "BDT"
    metadata: Mapping[str, str] = field(default_factory=dict)
    end_date: date | None = None
    terminal_at: datetime | None = None
    past_due_grace_expires_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.subscription_id or not self.merchant_id or not self.customer_ref:
            raise InvalidRequestError("subscription ids and refs must be non-empty")
        _require_positive_int(self.plan_amount_minor, "plan_amount_minor")
        if self.currency != "BDT":
            raise InvalidRequestError("currency must be BDT", code="currency_unsupported")
        if self.interval not in INTERVALS:
            raise InvalidRequestError("interval must be WEEKLY or MONTHLY", code="invalid_interval")
        if self.collection not in COLLECTIONS:
            raise InvalidRequestError(
                "collection must be LINK or MANDATE", code="invalid_collection"
            )
        if self.status not in SUBSCRIPTION_TABLE.states:
            raise InvalidRequestError(f"unknown subscription status {self.status!r}")
        if not isinstance(self.anchor_date, date) or isinstance(self.anchor_date, datetime):
            raise InvalidRequestError("anchor_date must be a date", code="invalid_anchor_date")
        _require_aware(self.created_at, "created_at")
        _require_aware(self.updated_at, "updated_at")
        object.__setattr__(self, "metadata", validate_metadata(self.metadata))


@dataclass(frozen=True)
class SubscriptionCycleRecord:
    """One ``subscription_cycles`` row."""

    cycle_id: str
    subscription_id: str
    cycle_number: int
    amount_minor: int
    due_at: datetime
    status: str
    created_at: datetime
    updated_at: datetime
    payment_intent_ids: tuple[str, ...] = ()
    dunning_attempts: int = 0
    dunning_started_at: datetime | None = None
    next_dunning_at: datetime | None = None
    paid_at: datetime | None = None
    failed_at: datetime | None = None
    terminal_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.cycle_id or not self.subscription_id:
            raise InvalidRequestError("cycle ids must be non-empty")
        if isinstance(self.cycle_number, bool) or not isinstance(self.cycle_number, int):
            raise InvalidRequestError("cycle_number must be int", code="invalid_cycle_number")
        if self.cycle_number < 1:
            raise InvalidRequestError("cycle_number must be >= 1", code="invalid_cycle_number")
        _require_positive_int(self.amount_minor, "amount_minor")
        if self.status not in SUBSCRIPTION_CYCLE_TABLE.states:
            raise InvalidRequestError(f"unknown cycle status {self.status!r}")
        if self.dunning_attempts < 0:
            raise InvalidRequestError("dunning_attempts must be >= 0")
        for name in ("due_at", "created_at", "updated_at"):
            _require_aware(getattr(self, name), name)


@dataclass(frozen=True)
class DunningAttemptRecord:
    """Durable per-cycle retry log."""

    cycle_id: str
    attempt_number: int
    payment_intent_id: str
    scheduled_at: datetime
    created_at: datetime

    def __post_init__(self) -> None:
        if self.attempt_number < 1:
            raise InvalidRequestError("attempt_number must be >= 1")
        _require_aware(self.scheduled_at, "scheduled_at")
        _require_aware(self.created_at, "created_at")


@runtime_checkable
class SubscriptionStore(Protocol):
    def insert_subscription(self, record: SubscriptionRecord) -> None: ...

    def save_subscription(self, record: SubscriptionRecord) -> None: ...

    def get_subscription(self, subscription_id: str) -> SubscriptionRecord | None: ...

    def find_subscription_by_idempotency_key(
        self, merchant_id: str, idempotency_key: str
    ) -> SubscriptionRecord | None: ...

    def list_subscriptions(
        self, *, merchant_id: str, limit: int, cursor: str | None
    ) -> tuple[Sequence[SubscriptionRecord], str | None]: ...

    def all_subscriptions(self) -> Sequence[SubscriptionRecord]: ...

    def insert_cycle(self, record: SubscriptionCycleRecord) -> None: ...

    def save_cycle(self, record: SubscriptionCycleRecord) -> None: ...

    def get_cycle(self, cycle_id: str) -> SubscriptionCycleRecord | None: ...

    def get_cycle_by_number(
        self, subscription_id: str, cycle_number: int
    ) -> SubscriptionCycleRecord | None: ...

    def list_cycles(
        self, subscription_id: str, *, limit: int | None = None, cursor: str | None = None
    ) -> tuple[Sequence[SubscriptionCycleRecord], str | None]: ...

    def list_due_cycles(self, *, now: datetime) -> Sequence[SubscriptionCycleRecord]: ...

    def list_due_dunning(self, *, now: datetime) -> Sequence[SubscriptionCycleRecord]: ...

    def find_cycle_by_intent(self, payment_intent_id: str) -> SubscriptionCycleRecord | None: ...

    def insert_dunning_attempt(self, record: DunningAttemptRecord) -> None: ...

    def list_dunning_attempts(self, cycle_id: str) -> Sequence[DunningAttemptRecord]: ...


class InMemorySubscriptionStore:
    """Deterministic in-memory subscription store."""

    def __init__(self) -> None:
        self._subscriptions: dict[str, SubscriptionRecord] = {}
        self._subscription_order: list[str] = []
        self._cycles: dict[str, SubscriptionCycleRecord] = {}
        self._cycle_order: list[str] = []
        self._attempts: dict[tuple[str, int], DunningAttemptRecord] = {}

    def insert_subscription(self, record: SubscriptionRecord) -> None:
        if record.subscription_id in self._subscriptions:
            raise ConflictError("subscription already exists", code="duplicate_subscription")
        if self.find_subscription_by_idempotency_key(
            record.merchant_id, record.created_idempotency_key
        ):
            raise ConflictError("idempotency key already used", code="duplicate_idempotency_key")
        self._subscriptions[record.subscription_id] = record
        self._subscription_order.append(record.subscription_id)

    def save_subscription(self, record: SubscriptionRecord) -> None:
        if record.subscription_id not in self._subscriptions:
            raise NotFoundError("subscription not found", code="subscription_not_found")
        self._subscriptions[record.subscription_id] = record

    def get_subscription(self, subscription_id: str) -> SubscriptionRecord | None:
        return self._subscriptions.get(subscription_id)

    def find_subscription_by_idempotency_key(
        self, merchant_id: str, idempotency_key: str
    ) -> SubscriptionRecord | None:
        for record in self._subscriptions.values():
            if (
                record.merchant_id == merchant_id
                and record.created_idempotency_key == idempotency_key
            ):
                return record
        return None

    def list_subscriptions(
        self, *, merchant_id: str, limit: int, cursor: str | None
    ) -> tuple[Sequence[SubscriptionRecord], str | None]:
        rows = [
            self._subscriptions[sub_id]
            for sub_id in self._subscription_order
            if self._subscriptions[sub_id].merchant_id == merchant_id
        ]
        rows.sort(key=lambda row: (row.created_at, row.subscription_id), reverse=True)
        offset = _offset(cursor)
        page = rows[offset : offset + limit]
        next_cursor = str(offset + limit) if offset + limit < len(rows) else None
        return page, next_cursor

    def all_subscriptions(self) -> Sequence[SubscriptionRecord]:
        return [self._subscriptions[sub_id] for sub_id in self._subscription_order]

    def insert_cycle(self, record: SubscriptionCycleRecord) -> None:
        if record.cycle_id in self._cycles:
            raise ConflictError("cycle already exists", code="duplicate_cycle")
        if self.get_cycle_by_number(record.subscription_id, record.cycle_number):
            raise ConflictError("cycle number already exists", code="duplicate_cycle")
        self._cycles[record.cycle_id] = record
        self._cycle_order.append(record.cycle_id)

    def save_cycle(self, record: SubscriptionCycleRecord) -> None:
        if record.cycle_id not in self._cycles:
            raise NotFoundError("cycle not found", code="subscription_cycle_not_found")
        self._cycles[record.cycle_id] = record

    def get_cycle(self, cycle_id: str) -> SubscriptionCycleRecord | None:
        return self._cycles.get(cycle_id)

    def get_cycle_by_number(
        self, subscription_id: str, cycle_number: int
    ) -> SubscriptionCycleRecord | None:
        for record in self._cycles.values():
            if record.subscription_id == subscription_id and record.cycle_number == cycle_number:
                return record
        return None

    def list_cycles(
        self, subscription_id: str, *, limit: int | None = None, cursor: str | None = None
    ) -> tuple[Sequence[SubscriptionCycleRecord], str | None]:
        rows = [
            self._cycles[cycle_id]
            for cycle_id in self._cycle_order
            if self._cycles[cycle_id].subscription_id == subscription_id
        ]
        rows.sort(key=lambda row: row.cycle_number)
        if limit is None:
            return rows, None
        offset = _offset(cursor)
        page = rows[offset : offset + limit]
        next_cursor = str(offset + limit) if offset + limit < len(rows) else None
        return page, next_cursor

    def list_due_cycles(self, *, now: datetime) -> Sequence[SubscriptionCycleRecord]:
        now = _require_aware(now, "now")
        return [
            self._cycles[cycle_id]
            for cycle_id in self._cycle_order
            if self._cycles[cycle_id].status == "SCHEDULED"
            and self._cycles[cycle_id].due_at <= now
        ]

    def list_due_dunning(self, *, now: datetime) -> Sequence[SubscriptionCycleRecord]:
        now = _require_aware(now, "now")
        return [
            self._cycles[cycle_id]
            for cycle_id in self._cycle_order
            if self._cycles[cycle_id].status == "DUNNING"
            and self._cycles[cycle_id].next_dunning_at is not None
            and self._cycles[cycle_id].next_dunning_at <= now
        ]

    def find_cycle_by_intent(self, payment_intent_id: str) -> SubscriptionCycleRecord | None:
        for record in self._cycles.values():
            if payment_intent_id in record.payment_intent_ids:
                return record
        return None

    def insert_dunning_attempt(self, record: DunningAttemptRecord) -> None:
        key = (record.cycle_id, record.attempt_number)
        if key in self._attempts:
            return
        self._attempts[key] = record

    def list_dunning_attempts(self, cycle_id: str) -> Sequence[DunningAttemptRecord]:
        rows = [row for (cid, _), row in self._attempts.items() if cid == cycle_id]
        return sorted(rows, key=lambda row: row.attempt_number)


def _offset(cursor: str | None) -> int:
    if cursor is None:
        return 0
    try:
        offset = int(cursor, 10)
    except ValueError as exc:
        raise InvalidRequestError("cursor is not valid", code="invalid_cursor") from exc
    if offset < 0:
        raise InvalidRequestError("cursor is not valid", code="invalid_cursor")
    return offset


_SUB_COLUMNS = (
    "subscription_id, merchant_id, customer_ref, plan_amount_minor, currency, interval, "
    "anchor_date, end_date, collection, status, metadata, created_idempotency_key, "
    "past_due_grace_expires_at, terminal_at, created_at, updated_at"
)

_CYCLE_COLUMNS = (
    "cycle_id, subscription_id, cycle_number, amount_minor, due_at, status, "
    "payment_intent_ids, dunning_attempts, dunning_started_at, next_dunning_at, "
    "paid_at, failed_at, terminal_at, created_at, updated_at"
)


class PostgresSubscriptionStore:
    """psycopg3 store matching migration 0110."""

    def __init__(self, connection_factory: Callable[[], Any]) -> None:
        self._connect = connection_factory

    def _run(self, fn: Callable[[Any], Any]) -> Any:
        with self._connect() as conn:
            try:
                result = fn(conn)
                conn.commit()
                return result
            except Exception:
                conn.rollback()
                raise

    @staticmethod
    def _sub_from_row(row: Mapping[str, Any]) -> SubscriptionRecord:
        return SubscriptionRecord(
            subscription_id=row["subscription_id"],
            merchant_id=row["merchant_id"],
            customer_ref=row["customer_ref"],
            plan_amount_minor=row["plan_amount_minor"],
            currency=row["currency"].strip(),
            interval=row["interval"],
            anchor_date=row["anchor_date"],
            end_date=row["end_date"],
            collection=row["collection"],
            status=row["status"],
            metadata=row["metadata"],
            created_idempotency_key=row["created_idempotency_key"],
            past_due_grace_expires_at=row["past_due_grace_expires_at"],
            terminal_at=row["terminal_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _cycle_from_row(row: Mapping[str, Any]) -> SubscriptionCycleRecord:
        return SubscriptionCycleRecord(
            cycle_id=row["cycle_id"],
            subscription_id=row["subscription_id"],
            cycle_number=row["cycle_number"],
            amount_minor=row["amount_minor"],
            due_at=row["due_at"],
            status=row["status"],
            payment_intent_ids=tuple(row["payment_intent_ids"]),
            dunning_attempts=row["dunning_attempts"],
            dunning_started_at=row["dunning_started_at"],
            next_dunning_at=row["next_dunning_at"],
            paid_at=row["paid_at"],
            failed_at=row["failed_at"],
            terminal_at=row["terminal_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _fetchone(self, sql: str, params: tuple) -> Mapping[str, Any] | None:
        from psycopg.rows import dict_row

        with self._connect() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(sql, params)
                return cur.fetchone()

    def _fetchall(self, sql: str, params: tuple) -> list[Mapping[str, Any]]:
        from psycopg.rows import dict_row

        with self._connect() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(sql, params)
                return cur.fetchall()

    def insert_subscription(self, record: SubscriptionRecord) -> None:
        from psycopg.types.json import Jsonb

        from bdpay.platform.canonical import canonical_json

        def op(conn: Any) -> None:
            conn.execute(
                f"INSERT INTO subscriptions ({_SUB_COLUMNS}) VALUES "
                "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    record.subscription_id,
                    record.merchant_id,
                    record.customer_ref,
                    record.plan_amount_minor,
                    record.currency,
                    record.interval,
                    record.anchor_date,
                    record.end_date,
                    record.collection,
                    record.status,
                    Jsonb(dict(record.metadata), dumps=lambda obj: canonical_json(obj).decode()),
                    record.created_idempotency_key,
                    record.past_due_grace_expires_at,
                    record.terminal_at,
                    record.created_at,
                    record.updated_at,
                ),
            )

        self._run(op)

    def save_subscription(self, record: SubscriptionRecord) -> None:
        def op(conn: Any) -> None:
            cur = conn.execute(
                "UPDATE subscriptions SET status = %s, past_due_grace_expires_at = %s, "
                "terminal_at = %s, updated_at = %s WHERE subscription_id = %s",
                (
                    record.status,
                    record.past_due_grace_expires_at,
                    record.terminal_at,
                    record.updated_at,
                    record.subscription_id,
                ),
            )
            if cur.rowcount != 1:
                raise NotFoundError("subscription not found", code="subscription_not_found")

        self._run(op)

    def get_subscription(self, subscription_id: str) -> SubscriptionRecord | None:
        row = self._fetchone(
            f"SELECT {_SUB_COLUMNS} FROM subscriptions WHERE subscription_id = %s",
            (subscription_id,),
        )
        return None if row is None else self._sub_from_row(row)

    def find_subscription_by_idempotency_key(
        self, merchant_id: str, idempotency_key: str
    ) -> SubscriptionRecord | None:
        row = self._fetchone(
            f"SELECT {_SUB_COLUMNS} FROM subscriptions "
            "WHERE merchant_id = %s AND created_idempotency_key = %s",
            (merchant_id, idempotency_key),
        )
        return None if row is None else self._sub_from_row(row)

    def list_subscriptions(
        self, *, merchant_id: str, limit: int, cursor: str | None
    ) -> tuple[Sequence[SubscriptionRecord], str | None]:
        offset = _offset(cursor)
        rows = self._fetchall(
            f"SELECT {_SUB_COLUMNS} FROM subscriptions WHERE merchant_id = %s "
            "ORDER BY created_at DESC, subscription_id LIMIT %s OFFSET %s",
            (merchant_id, limit + 1, offset),
        )
        page = [self._sub_from_row(row) for row in rows[:limit]]
        next_cursor = str(offset + limit) if len(rows) > limit else None
        return page, next_cursor

    def all_subscriptions(self) -> Sequence[SubscriptionRecord]:
        rows = self._fetchall(f"SELECT {_SUB_COLUMNS} FROM subscriptions ORDER BY created_at", ())
        return [self._sub_from_row(row) for row in rows]

    def insert_cycle(self, record: SubscriptionCycleRecord) -> None:
        def op(conn: Any) -> None:
            conn.execute(
                f"INSERT INTO subscription_cycles ({_CYCLE_COLUMNS}) VALUES "
                "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    record.cycle_id,
                    record.subscription_id,
                    record.cycle_number,
                    record.amount_minor,
                    record.due_at,
                    record.status,
                    list(record.payment_intent_ids),
                    record.dunning_attempts,
                    record.dunning_started_at,
                    record.next_dunning_at,
                    record.paid_at,
                    record.failed_at,
                    record.terminal_at,
                    record.created_at,
                    record.updated_at,
                ),
            )

        self._run(op)

    def save_cycle(self, record: SubscriptionCycleRecord) -> None:
        def op(conn: Any) -> None:
            cur = conn.execute(
                "UPDATE subscription_cycles SET status = %s, payment_intent_ids = %s, "
                "dunning_attempts = %s, dunning_started_at = %s, next_dunning_at = %s, "
                "paid_at = %s, failed_at = %s, terminal_at = %s, updated_at = %s "
                "WHERE cycle_id = %s",
                (
                    record.status,
                    list(record.payment_intent_ids),
                    record.dunning_attempts,
                    record.dunning_started_at,
                    record.next_dunning_at,
                    record.paid_at,
                    record.failed_at,
                    record.terminal_at,
                    record.updated_at,
                    record.cycle_id,
                ),
            )
            if cur.rowcount != 1:
                raise NotFoundError("cycle not found", code="subscription_cycle_not_found")

        self._run(op)

    def get_cycle(self, cycle_id: str) -> SubscriptionCycleRecord | None:
        row = self._fetchone(
            f"SELECT {_CYCLE_COLUMNS} FROM subscription_cycles WHERE cycle_id = %s",
            (cycle_id,),
        )
        return None if row is None else self._cycle_from_row(row)

    def get_cycle_by_number(
        self, subscription_id: str, cycle_number: int
    ) -> SubscriptionCycleRecord | None:
        row = self._fetchone(
            f"SELECT {_CYCLE_COLUMNS} FROM subscription_cycles "
            "WHERE subscription_id = %s AND cycle_number = %s",
            (subscription_id, cycle_number),
        )
        return None if row is None else self._cycle_from_row(row)

    def list_cycles(
        self, subscription_id: str, *, limit: int | None = None, cursor: str | None = None
    ) -> tuple[Sequence[SubscriptionCycleRecord], str | None]:
        if limit is None:
            rows = self._fetchall(
                f"SELECT {_CYCLE_COLUMNS} FROM subscription_cycles "
                "WHERE subscription_id = %s ORDER BY cycle_number",
                (subscription_id,),
            )
            return [self._cycle_from_row(row) for row in rows], None
        offset = _offset(cursor)
        rows = self._fetchall(
            f"SELECT {_CYCLE_COLUMNS} FROM subscription_cycles "
            "WHERE subscription_id = %s ORDER BY cycle_number LIMIT %s OFFSET %s",
            (subscription_id, limit + 1, offset),
        )
        page = [self._cycle_from_row(row) for row in rows[:limit]]
        next_cursor = str(offset + limit) if len(rows) > limit else None
        return page, next_cursor

    def list_due_cycles(self, *, now: datetime) -> Sequence[SubscriptionCycleRecord]:
        rows = self._fetchall(
            f"SELECT {_CYCLE_COLUMNS} FROM subscription_cycles "
            "WHERE status = 'SCHEDULED' AND due_at <= %s ORDER BY due_at, cycle_id",
            (_require_aware(now, "now"),),
        )
        return [self._cycle_from_row(row) for row in rows]

    def list_due_dunning(self, *, now: datetime) -> Sequence[SubscriptionCycleRecord]:
        rows = self._fetchall(
            f"SELECT {_CYCLE_COLUMNS} FROM subscription_cycles "
            "WHERE status = 'DUNNING' AND next_dunning_at <= %s "
            "ORDER BY next_dunning_at, cycle_id",
            (_require_aware(now, "now"),),
        )
        return [self._cycle_from_row(row) for row in rows]

    def find_cycle_by_intent(self, payment_intent_id: str) -> SubscriptionCycleRecord | None:
        row = self._fetchone(
            f"SELECT {_CYCLE_COLUMNS} FROM subscription_cycles "
            "WHERE %s = ANY(payment_intent_ids)",
            (payment_intent_id,),
        )
        return None if row is None else self._cycle_from_row(row)

    def insert_dunning_attempt(self, record: DunningAttemptRecord) -> None:
        def op(conn: Any) -> None:
            conn.execute(
                "INSERT INTO subscription_dunning_attempts "
                "(cycle_id, attempt_number, payment_intent_id, scheduled_at, created_at) "
                "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (cycle_id, attempt_number) DO NOTHING",
                (
                    record.cycle_id,
                    record.attempt_number,
                    record.payment_intent_id,
                    record.scheduled_at,
                    record.created_at,
                ),
            )

        self._run(op)

    def list_dunning_attempts(self, cycle_id: str) -> Sequence[DunningAttemptRecord]:
        rows = self._fetchall(
            "SELECT cycle_id, attempt_number, payment_intent_id, scheduled_at, created_at "
            "FROM subscription_dunning_attempts WHERE cycle_id = %s ORDER BY attempt_number",
            (cycle_id,),
        )
        return [
            DunningAttemptRecord(
                cycle_id=row["cycle_id"],
                attempt_number=row["attempt_number"],
                payment_intent_id=row["payment_intent_id"],
                scheduled_at=row["scheduled_at"],
                created_at=row["created_at"],
            )
            for row in rows
        ]

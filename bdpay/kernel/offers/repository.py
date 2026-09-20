"""Offer storage — repository protocol, in-memory and Postgres stores.

Storage pattern per IMPLEMENTATION.md: a repository Protocol with TWO
implementations — deterministic in-memory (unit tests, no infrastructure)
and psycopg3 matching ``db/migrations/0130_offer_engine.sql`` exactly
(tests marked ``@pytest.mark.integration``).

Cap enforcement (spec/18 §Cap enforcement — the concurrency-critical
section) is fail-closed and atomic in BOTH implementations:

- Postgres: the single-statement conditional
  ``UPDATE offer_counters SET used = used + 1 WHERE counter_id = %s AND
  used < cap_limit RETURNING used`` — no row means cap full means refused;
  there is no read-then-write race by construction.
- In-memory: the same conditional under one process-wide lock, emulating
  the single-statement semantics for the unit-level concurrency tests.

Day-scope re-credit carries the same-Asia/Dhaka-day guard in the statement
itself (``AND scope_key = %s`` with today's Dhaka date) so a re-credit after
midnight is a structural no-op.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from bdpay.kernel.offers.models import (
    OfferCounterRecord,
    OfferRecord,
    OfferRedemptionRecord,
    OfferVersionRecord,
    OfferWindow,
)
from bdpay.platform.errors import ConflictError, NotFoundError

__all__ = ["InMemoryOfferStore", "OfferStore", "PostgresOfferStore"]


@runtime_checkable
class OfferStore(Protocol):
    """Storage contract for offers, versions, redemptions, counters, dedupe."""

    # -- offers ---------------------------------------------------------------

    def insert_offer(self, record: OfferRecord, *, conn: Any | None = None) -> None: ...

    def get_offer(self, offer_id: str, *, conn: Any | None = None) -> OfferRecord | None: ...

    def update_offer(self, record: OfferRecord, *, conn: Any | None = None) -> None: ...

    def find_offer_by_create_key(
        self, merchant_id: str, idempotency_key: str, *, conn: Any | None = None
    ) -> OfferRecord | None: ...

    def list_offers(
        self,
        merchant_id: str,
        *,
        state: str | None = None,
        limit: int = 20,
        cursor: str | None = None,
        conn: Any | None = None,
    ) -> tuple[list[OfferRecord], str | None]: ...

    def list_merchant_offers_in_states(
        self, merchant_id: str, states: tuple[str, ...], *, conn: Any | None = None
    ) -> list[OfferRecord]: ...

    def list_offers_past_validity(
        self, *, now: datetime, conn: Any | None = None
    ) -> list[OfferRecord]: ...

    # -- versions ---------------------------------------------------------------

    def insert_version(
        self, record: OfferVersionRecord, *, conn: Any | None = None
    ) -> None: ...

    def get_version(
        self, offer_id: str, version: int, *, conn: Any | None = None
    ) -> OfferVersionRecord | None: ...

    # -- redemptions --------------------------------------------------------------

    def insert_redemption(
        self, record: OfferRedemptionRecord, *, conn: Any | None = None
    ) -> None: ...

    def get_redemption(
        self, redemption_id: str, *, conn: Any | None = None
    ) -> OfferRedemptionRecord | None: ...

    def get_redemption_by_intent(
        self, payment_intent_id: str, *, conn: Any | None = None
    ) -> OfferRedemptionRecord | None: ...

    def update_redemption(
        self, record: OfferRedemptionRecord, *, conn: Any | None = None
    ) -> None: ...

    def list_redemptions(
        self, offer_id: str, *, states: tuple[str, ...] | None = None, conn: Any | None = None
    ) -> list[OfferRedemptionRecord]: ...

    def list_expired_reservations(
        self, *, now: datetime, conn: Any | None = None
    ) -> list[OfferRedemptionRecord]: ...

    def set_intent_offer_id(
        self, payment_intent_id: str, offer_id: str, *, conn: Any | None = None
    ) -> None: ...

    # -- counters (atomic, fail-closed) ------------------------------------------

    def get_or_create_counter(
        self,
        counter_id: str,
        *,
        offer_id: str,
        scope: str,
        scope_key: str,
        cap_limit: int,
        now: datetime,
        conn: Any | None = None,
    ) -> OfferCounterRecord: ...

    def try_reserve_counter(
        self, counter_id: str, *, now: datetime, conn: Any | None = None
    ) -> bool: ...

    def recredit_counter(
        self,
        counter_id: str,
        *,
        now: datetime,
        require_scope_key: str | None = None,
        conn: Any | None = None,
    ) -> bool: ...

    def get_counter(
        self, counter_id: str, *, conn: Any | None = None
    ) -> OfferCounterRecord | None: ...

    def set_counter_cap(
        self, counter_id: str, cap_limit: int, *, now: datetime, conn: Any | None = None
    ) -> None: ...

    def prune_day_counters(
        self, *, older_than_scope_key: str, conn: Any | None = None
    ) -> int: ...

    # -- consumed-event dedupe (idempotent on event_id) ----------------------------

    def mark_event_consumed(
        self, event_id: str, *, now: datetime, conn: Any | None = None
    ) -> bool: ...


# ---------------------------------------------------------------------------
# In-memory implementation (deterministic; unit tests)
# ---------------------------------------------------------------------------


class InMemoryOfferStore:
    """Deterministic in-memory store. Counter ops are lock-serialized so the
    fail-closed single-statement semantics hold under threaded tests."""

    def __init__(self) -> None:
        self._offers: dict[str, OfferRecord] = {}
        self._versions: dict[tuple[str, int], OfferVersionRecord] = {}
        self._redemptions: dict[str, OfferRedemptionRecord] = {}
        self._counters: dict[str, OfferCounterRecord] = {}
        self._intent_offer: dict[str, str] = {}
        self._consumed_events: set[str] = set()
        self._lock = threading.Lock()

    # -- offers ---------------------------------------------------------------

    def insert_offer(self, record: OfferRecord, *, conn: Any | None = None) -> None:
        if record.offer_id in self._offers:
            raise ConflictError(f"offer {record.offer_id} already exists", code="duplicate_offer")
        existing = self.find_offer_by_create_key(
            record.merchant_id, record.created_idempotency_key
        )
        if existing is not None:
            raise ConflictError(
                "create idempotency key already used for this merchant",
                code="duplicate_idempotency_key",
            )
        self._offers[record.offer_id] = record

    def get_offer(self, offer_id: str, *, conn: Any | None = None) -> OfferRecord | None:
        return self._offers.get(offer_id)

    def update_offer(self, record: OfferRecord, *, conn: Any | None = None) -> None:
        if record.offer_id not in self._offers:
            raise NotFoundError(f"unknown offer {record.offer_id}")
        self._offers[record.offer_id] = record

    def find_offer_by_create_key(
        self, merchant_id: str, idempotency_key: str, *, conn: Any | None = None
    ) -> OfferRecord | None:
        for record in self._offers.values():
            if (
                record.merchant_id == merchant_id
                and record.created_idempotency_key == idempotency_key
            ):
                return record
        return None

    def list_offers(
        self,
        merchant_id: str,
        *,
        state: str | None = None,
        limit: int = 20,
        cursor: str | None = None,
        conn: Any | None = None,
    ) -> tuple[list[OfferRecord], str | None]:
        rows = sorted(
            (
                r
                for r in self._offers.values()
                if r.merchant_id == merchant_id and (state is None or r.state == state)
            ),
            key=lambda r: r.offer_id,
        )
        if cursor is not None:
            rows = [r for r in rows if r.offer_id > cursor]
        page = rows[:limit]
        next_cursor = page[-1].offer_id if len(rows) > limit else None
        return page, next_cursor

    def list_merchant_offers_in_states(
        self, merchant_id: str, states: tuple[str, ...], *, conn: Any | None = None
    ) -> list[OfferRecord]:
        return sorted(
            (
                r
                for r in self._offers.values()
                if r.merchant_id == merchant_id and r.state in states
            ),
            key=lambda r: r.offer_id,
        )

    def list_offers_past_validity(
        self, *, now: datetime, conn: Any | None = None
    ) -> list[OfferRecord]:
        return sorted(
            (
                r
                for r in self._offers.values()
                if r.state in ("ACTIVE", "PAUSED") and now > r.valid_until
            ),
            key=lambda r: r.offer_id,
        )

    # -- versions ---------------------------------------------------------------

    def insert_version(self, record: OfferVersionRecord, *, conn: Any | None = None) -> None:
        key = (record.offer_id, record.version)
        if key in self._versions:
            raise ConflictError(
                f"version {record.version} of {record.offer_id} already exists",
                code="duplicate_offer_version",
            )
        self._versions[key] = record

    def get_version(
        self, offer_id: str, version: int, *, conn: Any | None = None
    ) -> OfferVersionRecord | None:
        return self._versions.get((offer_id, version))

    # -- redemptions --------------------------------------------------------------

    def insert_redemption(
        self, record: OfferRedemptionRecord, *, conn: Any | None = None
    ) -> None:
        if record.redemption_id in self._redemptions:
            raise ConflictError(
                f"redemption {record.redemption_id} already exists",
                code="duplicate_redemption",
            )
        if any(
            r.payment_intent_id == record.payment_intent_id
            for r in self._redemptions.values()
        ):
            raise ConflictError(
                "payment intent already carries a redemption",
                code="duplicate_redemption",
            )
        self._redemptions[record.redemption_id] = record

    def get_redemption(
        self, redemption_id: str, *, conn: Any | None = None
    ) -> OfferRedemptionRecord | None:
        return self._redemptions.get(redemption_id)

    def get_redemption_by_intent(
        self, payment_intent_id: str, *, conn: Any | None = None
    ) -> OfferRedemptionRecord | None:
        for record in self._redemptions.values():
            if record.payment_intent_id == payment_intent_id:
                return record
        return None

    def update_redemption(
        self, record: OfferRedemptionRecord, *, conn: Any | None = None
    ) -> None:
        if record.redemption_id not in self._redemptions:
            raise NotFoundError(f"unknown redemption {record.redemption_id}")
        self._redemptions[record.redemption_id] = record

    def list_redemptions(
        self, offer_id: str, *, states: tuple[str, ...] | None = None, conn: Any | None = None
    ) -> list[OfferRedemptionRecord]:
        return sorted(
            (
                r
                for r in self._redemptions.values()
                if r.offer_id == offer_id and (states is None or r.state in states)
            ),
            key=lambda r: r.redemption_id,
        )

    def list_expired_reservations(
        self, *, now: datetime, conn: Any | None = None
    ) -> list[OfferRedemptionRecord]:
        return sorted(
            (
                r
                for r in self._redemptions.values()
                if r.state == "RESERVED" and now > r.reserved_expires_at
            ),
            key=lambda r: r.redemption_id,
        )

    def set_intent_offer_id(
        self, payment_intent_id: str, offer_id: str, *, conn: Any | None = None
    ) -> None:
        self._intent_offer[payment_intent_id] = offer_id

    def intent_offer_id(self, payment_intent_id: str) -> str | None:
        """In-memory mirror of ``payment_intents.offer_id`` (seam column)."""
        return self._intent_offer.get(payment_intent_id)

    # -- counters -----------------------------------------------------------------

    def get_or_create_counter(
        self,
        counter_id: str,
        *,
        offer_id: str,
        scope: str,
        scope_key: str,
        cap_limit: int,
        now: datetime,
        conn: Any | None = None,
    ) -> OfferCounterRecord:
        with self._lock:
            existing = self._counters.get(counter_id)
            if existing is not None:
                return existing
            record = OfferCounterRecord(
                counter_id=counter_id,
                offer_id=offer_id,
                scope=scope,
                scope_key=scope_key,
                cap_limit=cap_limit,
                used=0,
                updated_at=now,
            )
            self._counters[counter_id] = record
            return record

    def try_reserve_counter(
        self, counter_id: str, *, now: datetime, conn: Any | None = None
    ) -> bool:
        with self._lock:
            record = self._counters.get(counter_id)
            if record is None:
                return False  # no counter row = fail closed, never over-redeem
            if record.used >= record.cap_limit:
                return False
            self._counters[counter_id] = replace(
                record, used=record.used + 1, updated_at=now
            )
            return True

    def recredit_counter(
        self,
        counter_id: str,
        *,
        now: datetime,
        require_scope_key: str | None = None,
        conn: Any | None = None,
    ) -> bool:
        with self._lock:
            record = self._counters.get(counter_id)
            if record is None or record.used <= 0:
                return False
            if require_scope_key is not None and record.scope_key != require_scope_key:
                return False  # day rolled over in Dhaka: structurally a no-op
            self._counters[counter_id] = replace(
                record, used=record.used - 1, updated_at=now
            )
            return True

    def get_counter(
        self, counter_id: str, *, conn: Any | None = None
    ) -> OfferCounterRecord | None:
        return self._counters.get(counter_id)

    def set_counter_cap(
        self, counter_id: str, cap_limit: int, *, now: datetime, conn: Any | None = None
    ) -> None:
        with self._lock:
            record = self._counters.get(counter_id)
            if record is None:
                raise NotFoundError(f"unknown counter {counter_id}")
            self._counters[counter_id] = replace(
                record, cap_limit=cap_limit, updated_at=now
            )

    def prune_day_counters(
        self, *, older_than_scope_key: str, conn: Any | None = None
    ) -> int:
        with self._lock:
            doomed = [
                cid
                for cid, rec in self._counters.items()
                if rec.scope == "day" and rec.scope_key < older_than_scope_key
            ]
            for cid in doomed:
                del self._counters[cid]
            return len(doomed)

    # -- consumed-event dedupe -------------------------------------------------------

    def mark_event_consumed(
        self, event_id: str, *, now: datetime, conn: Any | None = None
    ) -> bool:
        with self._lock:
            if event_id in self._consumed_events:
                return False
            self._consumed_events.add(event_id)
            return True


# ---------------------------------------------------------------------------
# Postgres implementation (migration 0130)
# ---------------------------------------------------------------------------

_OFFER_COLUMNS = (
    "offer_id, version, merchant_id, kind, percent_bps, bogo_config, title, title_bn, "
    "windows, valid_from, valid_until, min_spend_minor, max_discount_minor, cap_per_day, "
    "cap_total, cap_per_customer, allowed_methods, cap_recredit_on_refund, commission_bps, "
    "subsidy_bps, state, currency, created_idempotency_key, created_at, updated_at"
)

_VERSION_COLUMNS = (
    "offer_id, version, percent_bps, bogo_config, windows, valid_from, valid_until, "
    "min_spend_minor, max_discount_minor, cap_per_day, cap_total, cap_per_customer, "
    "allowed_methods, commission_bps, subsidy_bps, created_at"
)

_REDEMPTION_COLUMNS = (
    "redemption_id, offer_id, offer_version, payment_intent_id, merchant_id, customer_id, "
    "gross_amount_minor, discount_minor, net_amount_minor, commission_minor, subsidy_minor, "
    "refunded_minor, currency, state, reserved_at, reserved_expires_at, resolved_at"
)

_COUNTER_COLUMNS = "counter_id, offer_id, scope, scope_key, cap_limit, used, updated_at"


def _jsonb(payload: object) -> Any:
    from psycopg.types.json import Jsonb

    from bdpay.platform.canonical import canonical_json

    return Jsonb(payload, dumps=lambda o: canonical_json(o).decode("utf-8"))


class PostgresOfferStore:
    """psycopg3 store matching migration 0130 exactly.

    ``connection_factory`` returns a new psycopg connection; when the caller
    passes ``conn`` the statement joins that transaction instead — which is
    how the reservation composes atomically with the ``payment_intents`` and
    ``offer_redemptions`` inserts (spec/18 §Cap enforcement).
    """

    def __init__(self, connection_factory: Callable[[], Any]) -> None:
        self._connect = connection_factory

    # -- low-level helpers ------------------------------------------------------

    def _execute(self, conn: Any | None, sql: str, params: tuple) -> None:
        if conn is not None:
            conn.execute(sql, params)
            return
        with self._connect() as owned:
            owned.execute(sql, params)
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
                row = cur.fetchone()
            owned.commit()
            return row

    def _fetchall(self, conn: Any | None, sql: str, params: tuple) -> list[Mapping[str, Any]]:
        from psycopg.rows import dict_row

        if conn is not None:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(sql, params)
                return cur.fetchall()
        with self._connect() as owned:
            with owned.cursor(row_factory=dict_row) as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()
            owned.commit()
            return rows

    # -- row mappers -------------------------------------------------------------

    @staticmethod
    def _row_to_offer(row: Mapping[str, Any]) -> OfferRecord:
        return OfferRecord(
            offer_id=row["offer_id"],
            version=row["version"],
            merchant_id=row["merchant_id"],
            kind=row["kind"],
            percent_bps=row["percent_bps"],
            bogo_config=row["bogo_config"],
            title=row["title"],
            title_bn=row["title_bn"],
            windows=tuple(OfferWindow.from_payload(w) for w in row["windows"]),
            valid_from=row["valid_from"],
            valid_until=row["valid_until"],
            min_spend_minor=row["min_spend_minor"],
            max_discount_minor=row["max_discount_minor"],
            cap_per_day=row["cap_per_day"],
            cap_total=row["cap_total"],
            cap_per_customer=row["cap_per_customer"],
            allowed_methods=tuple(row["allowed_methods"]),
            cap_recredit_on_refund=row["cap_recredit_on_refund"],
            commission_bps=row["commission_bps"],
            subsidy_bps=row["subsidy_bps"],
            state=row["state"],
            currency=row["currency"].strip(),
            created_idempotency_key=row["created_idempotency_key"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _row_to_version(row: Mapping[str, Any]) -> OfferVersionRecord:
        return OfferVersionRecord(
            offer_id=row["offer_id"],
            version=row["version"],
            percent_bps=row["percent_bps"],
            bogo_config=row["bogo_config"],
            windows=tuple(OfferWindow.from_payload(w) for w in row["windows"]),
            valid_from=row["valid_from"],
            valid_until=row["valid_until"],
            min_spend_minor=row["min_spend_minor"],
            max_discount_minor=row["max_discount_minor"],
            cap_per_day=row["cap_per_day"],
            cap_total=row["cap_total"],
            cap_per_customer=row["cap_per_customer"],
            allowed_methods=tuple(row["allowed_methods"]),
            commission_bps=row["commission_bps"],
            subsidy_bps=row["subsidy_bps"],
            created_at=row["created_at"],
        )

    @staticmethod
    def _row_to_redemption(row: Mapping[str, Any]) -> OfferRedemptionRecord:
        return OfferRedemptionRecord(
            redemption_id=row["redemption_id"],
            offer_id=row["offer_id"],
            offer_version=row["offer_version"],
            payment_intent_id=row["payment_intent_id"],
            merchant_id=row["merchant_id"],
            customer_id=row["customer_id"],
            gross_amount_minor=row["gross_amount_minor"],
            discount_minor=row["discount_minor"],
            net_amount_minor=row["net_amount_minor"],
            commission_minor=row["commission_minor"],
            subsidy_minor=row["subsidy_minor"],
            refunded_minor=row["refunded_minor"],
            currency=row["currency"].strip(),
            state=row["state"],
            reserved_at=row["reserved_at"],
            reserved_expires_at=row["reserved_expires_at"],
            resolved_at=row["resolved_at"],
        )

    @staticmethod
    def _row_to_counter(row: Mapping[str, Any]) -> OfferCounterRecord:
        return OfferCounterRecord(
            counter_id=row["counter_id"],
            offer_id=row["offer_id"],
            scope=row["scope"],
            scope_key=row["scope_key"],
            cap_limit=row["cap_limit"],
            used=row["used"],
            updated_at=row["updated_at"],
        )

    # -- offers ---------------------------------------------------------------

    def insert_offer(self, record: OfferRecord, *, conn: Any | None = None) -> None:
        sql = (
            f"INSERT INTO offers ({_OFFER_COLUMNS}) VALUES "
            "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
            "%s, %s, %s, %s, %s, %s, %s)"
        )
        self._execute(
            conn,
            sql,
            (
                record.offer_id,
                record.version,
                record.merchant_id,
                record.kind,
                record.percent_bps,
                _jsonb(dict(record.bogo_config)) if record.bogo_config is not None else None,
                record.title,
                record.title_bn,
                _jsonb([w.to_payload() for w in record.windows]),
                record.valid_from,
                record.valid_until,
                record.min_spend_minor,
                record.max_discount_minor,
                record.cap_per_day,
                record.cap_total,
                record.cap_per_customer,
                list(record.allowed_methods),
                record.cap_recredit_on_refund,
                record.commission_bps,
                record.subsidy_bps,
                record.state,
                record.currency,
                record.created_idempotency_key,
                record.created_at,
                record.updated_at,
            ),
        )

    def get_offer(self, offer_id: str, *, conn: Any | None = None) -> OfferRecord | None:
        row = self._fetchone(
            conn, f"SELECT {_OFFER_COLUMNS} FROM offers WHERE offer_id = %s", (offer_id,)
        )
        return None if row is None else self._row_to_offer(row)

    def update_offer(self, record: OfferRecord, *, conn: Any | None = None) -> None:
        sql = (
            "UPDATE offers SET version = %s, percent_bps = %s, bogo_config = %s, "
            "title = %s, title_bn = %s, windows = %s, valid_from = %s, valid_until = %s, "
            "min_spend_minor = %s, max_discount_minor = %s, cap_per_day = %s, "
            "cap_total = %s, cap_per_customer = %s, allowed_methods = %s, "
            "cap_recredit_on_refund = %s, commission_bps = %s, subsidy_bps = %s, "
            "state = %s, updated_at = %s WHERE offer_id = %s"
        )
        self._execute(
            conn,
            sql,
            (
                record.version,
                record.percent_bps,
                _jsonb(dict(record.bogo_config)) if record.bogo_config is not None else None,
                record.title,
                record.title_bn,
                _jsonb([w.to_payload() for w in record.windows]),
                record.valid_from,
                record.valid_until,
                record.min_spend_minor,
                record.max_discount_minor,
                record.cap_per_day,
                record.cap_total,
                record.cap_per_customer,
                list(record.allowed_methods),
                record.cap_recredit_on_refund,
                record.commission_bps,
                record.subsidy_bps,
                record.state,
                record.updated_at,
                record.offer_id,
            ),
        )

    def find_offer_by_create_key(
        self, merchant_id: str, idempotency_key: str, *, conn: Any | None = None
    ) -> OfferRecord | None:
        row = self._fetchone(
            conn,
            f"SELECT {_OFFER_COLUMNS} FROM offers "
            "WHERE merchant_id = %s AND created_idempotency_key = %s",
            (merchant_id, idempotency_key),
        )
        return None if row is None else self._row_to_offer(row)

    def list_offers(
        self,
        merchant_id: str,
        *,
        state: str | None = None,
        limit: int = 20,
        cursor: str | None = None,
        conn: Any | None = None,
    ) -> tuple[list[OfferRecord], str | None]:
        clauses = ["merchant_id = %s"]
        params: list[object] = [merchant_id]
        if state is not None:
            clauses.append("state = %s")
            params.append(state)
        if cursor is not None:
            clauses.append("offer_id > %s")
            params.append(cursor)
        params.append(limit + 1)
        rows = self._fetchall(
            conn,
            f"SELECT {_OFFER_COLUMNS} FROM offers WHERE {' AND '.join(clauses)} "
            "ORDER BY offer_id LIMIT %s",
            tuple(params),
        )
        records = [self._row_to_offer(r) for r in rows]
        page = records[:limit]
        next_cursor = page[-1].offer_id if len(records) > limit else None
        return page, next_cursor

    def list_merchant_offers_in_states(
        self, merchant_id: str, states: tuple[str, ...], *, conn: Any | None = None
    ) -> list[OfferRecord]:
        rows = self._fetchall(
            conn,
            f"SELECT {_OFFER_COLUMNS} FROM offers "
            "WHERE merchant_id = %s AND state = ANY(%s) ORDER BY offer_id",
            (merchant_id, list(states)),
        )
        return [self._row_to_offer(r) for r in rows]

    def list_offers_past_validity(
        self, *, now: datetime, conn: Any | None = None
    ) -> list[OfferRecord]:
        rows = self._fetchall(
            conn,
            f"SELECT {_OFFER_COLUMNS} FROM offers "
            "WHERE state = ANY(%s) AND valid_until < %s ORDER BY offer_id",
            (["ACTIVE", "PAUSED"], now),
        )
        return [self._row_to_offer(r) for r in rows]

    # -- versions ---------------------------------------------------------------

    def insert_version(self, record: OfferVersionRecord, *, conn: Any | None = None) -> None:
        sql = (
            f"INSERT INTO offer_versions ({_VERSION_COLUMNS}) VALUES "
            "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
        )
        self._execute(
            conn,
            sql,
            (
                record.offer_id,
                record.version,
                record.percent_bps,
                _jsonb(dict(record.bogo_config)) if record.bogo_config is not None else None,
                _jsonb([w.to_payload() for w in record.windows]),
                record.valid_from,
                record.valid_until,
                record.min_spend_minor,
                record.max_discount_minor,
                record.cap_per_day,
                record.cap_total,
                record.cap_per_customer,
                list(record.allowed_methods),
                record.commission_bps,
                record.subsidy_bps,
                record.created_at,
            ),
        )

    def get_version(
        self, offer_id: str, version: int, *, conn: Any | None = None
    ) -> OfferVersionRecord | None:
        row = self._fetchone(
            conn,
            f"SELECT {_VERSION_COLUMNS} FROM offer_versions "
            "WHERE offer_id = %s AND version = %s",
            (offer_id, version),
        )
        return None if row is None else self._row_to_version(row)

    # -- redemptions --------------------------------------------------------------

    def insert_redemption(
        self, record: OfferRedemptionRecord, *, conn: Any | None = None
    ) -> None:
        sql = (
            f"INSERT INTO offer_redemptions ({_REDEMPTION_COLUMNS}) VALUES "
            "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
        )
        self._execute(
            conn,
            sql,
            (
                record.redemption_id,
                record.offer_id,
                record.offer_version,
                record.payment_intent_id,
                record.merchant_id,
                record.customer_id,
                record.gross_amount_minor,
                record.discount_minor,
                record.net_amount_minor,
                record.commission_minor,
                record.subsidy_minor,
                record.refunded_minor,
                record.currency,
                record.state,
                record.reserved_at,
                record.reserved_expires_at,
                record.resolved_at,
            ),
        )

    def get_redemption(
        self, redemption_id: str, *, conn: Any | None = None
    ) -> OfferRedemptionRecord | None:
        row = self._fetchone(
            conn,
            f"SELECT {_REDEMPTION_COLUMNS} FROM offer_redemptions WHERE redemption_id = %s",
            (redemption_id,),
        )
        return None if row is None else self._row_to_redemption(row)

    def get_redemption_by_intent(
        self, payment_intent_id: str, *, conn: Any | None = None
    ) -> OfferRedemptionRecord | None:
        row = self._fetchone(
            conn,
            f"SELECT {_REDEMPTION_COLUMNS} FROM offer_redemptions "
            "WHERE payment_intent_id = %s",
            (payment_intent_id,),
        )
        return None if row is None else self._row_to_redemption(row)

    def update_redemption(
        self, record: OfferRedemptionRecord, *, conn: Any | None = None
    ) -> None:
        sql = (
            "UPDATE offer_redemptions SET state = %s, refunded_minor = %s, "
            "resolved_at = %s WHERE redemption_id = %s"
        )
        self._execute(
            conn,
            sql,
            (record.state, record.refunded_minor, record.resolved_at, record.redemption_id),
        )

    def list_redemptions(
        self, offer_id: str, *, states: tuple[str, ...] | None = None, conn: Any | None = None
    ) -> list[OfferRedemptionRecord]:
        if states is None:
            rows = self._fetchall(
                conn,
                f"SELECT {_REDEMPTION_COLUMNS} FROM offer_redemptions "
                "WHERE offer_id = %s ORDER BY redemption_id",
                (offer_id,),
            )
        else:
            rows = self._fetchall(
                conn,
                f"SELECT {_REDEMPTION_COLUMNS} FROM offer_redemptions "
                "WHERE offer_id = %s AND state = ANY(%s) ORDER BY redemption_id",
                (offer_id, list(states)),
            )
        return [self._row_to_redemption(r) for r in rows]

    def list_expired_reservations(
        self, *, now: datetime, conn: Any | None = None
    ) -> list[OfferRedemptionRecord]:
        rows = self._fetchall(
            conn,
            f"SELECT {_REDEMPTION_COLUMNS} FROM offer_redemptions "
            "WHERE state = 'RESERVED' AND reserved_expires_at < %s ORDER BY redemption_id",
            (now,),
        )
        return [self._row_to_redemption(r) for r in rows]

    def set_intent_offer_id(
        self, payment_intent_id: str, offer_id: str, *, conn: Any | None = None
    ) -> None:
        self._execute(
            conn,
            "UPDATE payment_intents SET offer_id = %s WHERE payment_intent_id = %s",
            (offer_id, payment_intent_id),
        )

    # -- counters (the atomic single statements) ------------------------------------

    def get_or_create_counter(
        self,
        counter_id: str,
        *,
        offer_id: str,
        scope: str,
        scope_key: str,
        cap_limit: int,
        now: datetime,
        conn: Any | None = None,
    ) -> OfferCounterRecord:
        self._execute(
            conn,
            f"INSERT INTO offer_counters ({_COUNTER_COLUMNS}) "
            "VALUES (%s, %s, %s, %s, %s, 0, %s) "
            "ON CONFLICT (offer_id, scope, scope_key) DO NOTHING",
            (counter_id, offer_id, scope, scope_key, cap_limit, now),
        )
        record = self.get_counter(counter_id, conn=conn)
        if record is None:  # only reachable on storage failure — fail closed
            raise ConflictError(
                f"counter {counter_id} could not be created", code="counter_unavailable"
            )
        return record

    def try_reserve_counter(
        self, counter_id: str, *, now: datetime, conn: Any | None = None
    ) -> bool:
        # spec/18 §Cap enforcement, verbatim semantics: no row = cap full = refused.
        sql = (
            "UPDATE offer_counters SET used = used + 1, updated_at = %s "
            "WHERE counter_id = %s AND used < cap_limit RETURNING used"
        )
        row = self._fetchone(conn, sql, (now, counter_id))
        return row is not None

    def recredit_counter(
        self,
        counter_id: str,
        *,
        now: datetime,
        require_scope_key: str | None = None,
        conn: Any | None = None,
    ) -> bool:
        if require_scope_key is None:
            sql = (
                "UPDATE offer_counters SET used = used - 1, updated_at = %s "
                "WHERE counter_id = %s AND used > 0 RETURNING used"
            )
            row = self._fetchone(conn, sql, (now, counter_id))
        else:
            # Day-scope guard in SQL on the counter's scope_key (spec/18):
            # the re-credit applies only on the same Asia/Dhaka calendar day.
            sql = (
                "UPDATE offer_counters SET used = used - 1, updated_at = %s "
                "WHERE counter_id = %s AND used > 0 AND scope_key = %s RETURNING used"
            )
            row = self._fetchone(conn, sql, (now, counter_id, require_scope_key))
        return row is not None

    def get_counter(
        self, counter_id: str, *, conn: Any | None = None
    ) -> OfferCounterRecord | None:
        row = self._fetchone(
            conn,
            f"SELECT {_COUNTER_COLUMNS} FROM offer_counters WHERE counter_id = %s",
            (counter_id,),
        )
        return None if row is None else self._row_to_counter(row)

    def set_counter_cap(
        self, counter_id: str, cap_limit: int, *, now: datetime, conn: Any | None = None
    ) -> None:
        self._execute(
            conn,
            "UPDATE offer_counters SET cap_limit = %s, updated_at = %s WHERE counter_id = %s",
            (cap_limit, now, counter_id),
        )

    def prune_day_counters(
        self, *, older_than_scope_key: str, conn: Any | None = None
    ) -> int:
        rows = self._fetchall(
            conn,
            "DELETE FROM offer_counters WHERE scope = 'day' AND scope_key < %s "
            "RETURNING counter_id",
            (older_than_scope_key,),
        )
        return len(rows)

    # -- consumed-event dedupe -------------------------------------------------------

    def mark_event_consumed(
        self, event_id: str, *, now: datetime, conn: Any | None = None
    ) -> bool:
        row = self._fetchone(
            conn,
            "INSERT INTO offer_event_consumption (event_id, consumed_at) VALUES (%s, %s) "
            "ON CONFLICT (event_id) DO NOTHING RETURNING event_id",
            (event_id, now),
        )
        return row is not None

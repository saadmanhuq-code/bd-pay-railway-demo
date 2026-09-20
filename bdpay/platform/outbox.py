"""Atomic outbox — envelope, store, and queue-drain worker (spec/00 §5, spec/02).

Events are produced ONLY via the outbox: the producing package inserts the
outbox row inside the same Postgres transaction as its business write, and the
:class:`OutboxWorker` drains the queue and publishes to the event bus. Delivery
is at-least-once; consumers MUST be idempotent on ``event_id`` (spec/00 §5) —
:class:`IdempotentConsumer` is the reference dedupe wrapper.

Envelope shape is exactly spec/00 §5::

    { "event_id":"obx_<id>", "type":"<entity>.<past_tense_verb>",
      "occurred_at":"<RFC3339 UTC>", "subject_type":"...", "subject_id":"...",
      "schema_version":1, "producer":"<service>@<version>",
      "payload":{...PII-redacted...} }

Queue-drain pattern ported from
``dse-profit-engine/src/dse_engine/storage/store.py`` (ADAPT PATTERN, per
PORTING-MAP.md): batched drain on a short injectable interval, INSERT-or-ignore
idempotency on the content-addressed ``event_id``, and a publish callback
abstraction (the Redpanda producer is wired later; tests use
:class:`RecordingPublisher`). The SQLite threading code was NOT lifted — the
Postgres implementation matches ``db/migrations/0001_outbox.sql`` exactly.

The integrity row: outbox status is ``PENDING -> CLAIMED -> PUBLISHED`` with a
lease so a crashed worker's claims are reclaimable; publish failures release
the claim with ``attempt_count + 1`` and the row turns ``POISON`` at the
spec/02 cap (25) — it never silently disappears.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, runtime_checkable

from bdpay.platform.canonical import sha256_canonical
from bdpay.platform.clock import Clock
from bdpay.platform.ids import make_id

__all__ = [
    "MAX_ATTEMPTS",
    "TOPICS",
    "EventPublisher",
    "IdempotentConsumer",
    "InMemoryOutboxStore",
    "OutboxError",
    "OutboxEvent",
    "OutboxRecord",
    "OutboxStore",
    "OutboxWorker",
    "PostgresOutboxStore",
    "RecordingPublisher",
    "build_event",
]

#: Closed topic registry (spec/00 §5 + the accepted extensions from the
#: canonical event registry, spec/02 Appendix A). New topics require a
#: registry entry — enqueueing to an unknown topic fails closed.
TOPICS: frozenset[str] = frozenset(
    {
        "payment.events",
        "settlement.events",
        "aml.events",
        "recon.events",
        "kyc.events",
        "connector.events",
        "qr.events",
        "vault.events",
        "platform.events",
        "notification.events",
        # spec/18 G4 grant (principal directive 2026-06-12): the offer-engine
        # topic registry entry. Errata S18-E9 records the fold-in.
        "offer.events",
    }
)

#: spec/02 ``outbox_attempt_cap CHECK (attempt_count <= 25)``.
MAX_ATTEMPTS = 25

_STATUSES = ("PENDING", "CLAIMED", "PUBLISHED", "POISON")


class OutboxError(ValueError):
    """Raised on invalid outbox construction or an invalid state transition."""


def _require_aware(value: datetime, what: str) -> datetime:
    if not isinstance(value, datetime):
        raise OutboxError(f"{what} must be datetime, got {type(value).__name__}")
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise OutboxError(f"{what} must be timezone-aware (naive rejected, spec 00 §7)")
    return value.astimezone(UTC)


def _rfc3339_ms(dt: datetime) -> str:
    """RFC3339 UTC string, millisecond precision, Z suffix (E12 form)."""
    dt = dt.astimezone(UTC)
    return (
        f"{dt.year:04d}-{dt.month:02d}-{dt.day:02d}"
        f"T{dt.hour:02d}:{dt.minute:02d}:{dt.second:02d}"
        f".{dt.microsecond // 1000:03d}Z"
    )


@dataclass(frozen=True)
class OutboxEvent:
    """The spec/00 §5 event envelope, with ``occurred_at`` as aware datetime.

    Wire form (``to_envelope``) serializes ``occurred_at`` to the RFC3339
    string; in-process the value stays an aware UTC datetime per the
    platform timestamp convention.
    """

    event_id: str
    event_type: str
    occurred_at: datetime
    subject_type: str
    subject_id: str
    producer: str
    topic: str
    payload: Mapping[str, object]
    schema_version: int = 1

    def to_envelope(self) -> dict[str, object]:
        """The exact spec/00 §5 envelope dict (key ``type``, RFC3339 string)."""
        return {
            "event_id": self.event_id,
            "type": self.event_type,
            "occurred_at": _rfc3339_ms(self.occurred_at),
            "subject_type": self.subject_type,
            "subject_id": self.subject_id,
            "schema_version": self.schema_version,
            "producer": self.producer,
            "payload": dict(self.payload),
        }


def build_event(
    *,
    event_type: str,
    subject_type: str,
    subject_id: str,
    producer: str,
    topic: str,
    payload: Mapping[str, object],
    occurred_at: datetime,
) -> OutboxEvent:
    """Validate fields and mint the content-addressed ``obx_`` event id.

    Deterministic by construction: identical inputs produce the identical
    ``event_id``, which is what makes INSERT-or-ignore enqueue and consumer
    dedupe correct.
    """
    if topic not in TOPICS:
        raise OutboxError(
            f"unknown topic {topic!r}; topics are a closed registry (spec 00 §5): "
            f"{sorted(TOPICS)}"
        )
    if not event_type or "." not in event_type:
        raise OutboxError(
            f"event_type must be '<entity>.<past_tense_verb>' (spec 00 §5), got {event_type!r}"
        )
    for name, value in (
        ("subject_type", subject_type),
        ("subject_id", subject_id),
        ("producer", producer),
    ):
        if not isinstance(value, str) or not value:
            raise OutboxError(f"{name} must be a non-empty string")
    if not isinstance(payload, Mapping):
        raise OutboxError(f"payload must be a mapping, got {type(payload).__name__}")
    occurred_at = _require_aware(occurred_at, "occurred_at")
    payload_dict = dict(payload)
    event_id = make_id(
        "obx",
        {
            "event_type": event_type,
            "subject_type": subject_type,
            "subject_id": subject_id,
            "producer": producer,
            "topic": topic,
            "occurred_at": occurred_at,
            "payload_hash": sha256_canonical(payload_dict),
        },
    )
    return OutboxEvent(
        event_id=event_id,
        event_type=event_type,
        occurred_at=occurred_at,
        subject_type=subject_type,
        subject_id=subject_id,
        producer=producer,
        topic=topic,
        payload=payload_dict,
    )


@dataclass(frozen=True)
class OutboxRecord:
    """An outbox row: the envelope plus queue state (spec/02 columns)."""

    event: OutboxEvent
    status: str
    produced_at: datetime
    attempt_count: int = 0
    claimed_by: str | None = None
    claimed_at: datetime | None = None
    lease_expires_at: datetime | None = None
    published_at: datetime | None = None
    last_error: str | None = None


@runtime_checkable
class OutboxStore(Protocol):
    """Storage contract for the outbox queue.

    ``enqueue`` MUST be callable inside the producing package's transaction
    (``conn`` parameter on the Postgres implementation); it is idempotent on
    ``event_id`` (INSERT-or-ignore) and returns the event id either way.
    """

    def enqueue(self, event: OutboxEvent, *, conn: Any | None = None) -> str: ...

    def claim_batch(
        self, *, worker_id: str, limit: int, now: datetime, lease_seconds: int = 30
    ) -> list[OutboxRecord]: ...

    def mark_published(self, event_id: str, *, worker_id: str, now: datetime) -> None: ...

    def release_failed(
        self, event_id: str, *, worker_id: str, error: str, now: datetime
    ) -> None: ...

    def get(self, event_id: str) -> OutboxRecord | None: ...

    def pending_count(self, *, now: datetime) -> int: ...


class InMemoryOutboxStore:
    """Deterministic in-memory store for unit tests (FIFO by produced_at)."""

    def __init__(self) -> None:
        self._rows: dict[str, OutboxRecord] = {}
        self._order: list[str] = []

    def enqueue(self, event: OutboxEvent, *, conn: Any | None = None) -> str:
        if not isinstance(event, OutboxEvent):
            raise OutboxError(f"expected OutboxEvent, got {type(event).__name__}")
        if event.event_id in self._rows:  # INSERT-or-ignore idempotency
            return event.event_id
        self._rows[event.event_id] = OutboxRecord(
            event=event, status="PENDING", produced_at=event.occurred_at
        )
        self._order.append(event.event_id)
        return event.event_id

    def _due_ids(self, now: datetime) -> list[str]:
        def sort_key(eid: str) -> tuple[datetime, int]:
            return (self._rows[eid].produced_at, self._order.index(eid))

        due = [
            eid
            for eid in self._order
            if self._rows[eid].status == "PENDING"
            or (
                self._rows[eid].status == "CLAIMED"
                and self._rows[eid].lease_expires_at is not None
                and self._rows[eid].lease_expires_at < now
            )
        ]
        return sorted(due, key=sort_key)

    def claim_batch(
        self, *, worker_id: str, limit: int, now: datetime, lease_seconds: int = 30
    ) -> list[OutboxRecord]:
        now = _require_aware(now, "now")
        claimed: list[OutboxRecord] = []
        for eid in self._due_ids(now)[:limit]:
            row = replace(
                self._rows[eid],
                status="CLAIMED",
                claimed_by=worker_id,
                claimed_at=now,
                lease_expires_at=now + timedelta(seconds=lease_seconds),
            )
            self._rows[eid] = row
            claimed.append(row)
        return claimed

    def _owned(self, event_id: str, worker_id: str) -> OutboxRecord:
        row = self._rows.get(event_id)
        if row is None:
            raise OutboxError(f"unknown outbox event {event_id!r}")
        if row.status != "CLAIMED" or row.claimed_by != worker_id:
            raise OutboxError(
                f"event {event_id!r} is not claimed by {worker_id!r} "
                f"(status={row.status}, claimed_by={row.claimed_by!r})"
            )
        return row

    def mark_published(self, event_id: str, *, worker_id: str, now: datetime) -> None:
        row = self._owned(event_id, worker_id)
        self._rows[event_id] = replace(
            row,
            status="PUBLISHED",
            published_at=_require_aware(now, "now"),
            claimed_by=None,
            lease_expires_at=None,
        )

    def release_failed(
        self, event_id: str, *, worker_id: str, error: str, now: datetime
    ) -> None:
        row = self._owned(event_id, worker_id)
        attempts = row.attempt_count + 1
        status = "POISON" if attempts >= MAX_ATTEMPTS else "PENDING"
        self._rows[event_id] = replace(
            row,
            status=status,
            attempt_count=attempts,
            last_error=error,
            claimed_by=None,
            claimed_at=None,
            lease_expires_at=None,
        )

    def get(self, event_id: str) -> OutboxRecord | None:
        return self._rows.get(event_id)

    def pending_count(self, *, now: datetime) -> int:
        return len(self._due_ids(_require_aware(now, "now")))


_OUTBOX_COLUMNS = (
    "outbox_id, event_type, subject_type, subject_id, topic, payload, schema_version, "
    "status, producer, produced_at, claimed_by, claimed_at, lease_expires_at, "
    "published_at, attempt_count, last_error"
)


class PostgresOutboxStore:
    """psycopg3 store matching ``db/migrations/0001_outbox.sql`` exactly.

    ``connection_factory`` returns a NEW psycopg connection (the caller of
    ``enqueue`` may instead pass its own in-transaction connection so the
    outbox row commits atomically with the business write — the spec/00
    atomic-outbox rule).
    """

    def __init__(self, connection_factory: Callable[[], Any]) -> None:
        self._connect = connection_factory

    @staticmethod
    def _row_to_record(row: Mapping[str, Any]) -> OutboxRecord:
        event = OutboxEvent(
            event_id=row["outbox_id"],
            event_type=row["event_type"],
            occurred_at=row["produced_at"],
            subject_type=row["subject_type"],
            subject_id=row["subject_id"],
            producer=row["producer"],
            topic=row["topic"],
            payload=row["payload"],
            schema_version=row["schema_version"],
        )
        return OutboxRecord(
            event=event,
            status=row["status"],
            produced_at=row["produced_at"],
            attempt_count=row["attempt_count"],
            claimed_by=row["claimed_by"],
            claimed_at=row["claimed_at"],
            lease_expires_at=row["lease_expires_at"],
            published_at=row["published_at"],
            last_error=row["last_error"],
        )

    def enqueue(self, event: OutboxEvent, *, conn: Any | None = None) -> str:
        from psycopg.types.json import Jsonb

        from bdpay.platform.canonical import canonical_json

        if not isinstance(event, OutboxEvent):
            raise OutboxError(f"expected OutboxEvent, got {type(event).__name__}")
        sql = (
            "INSERT INTO outbox (outbox_id, event_type, subject_type, subject_id, topic, "
            "payload, schema_version, status, producer, produced_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, 'PENDING', %s, %s) "
            "ON CONFLICT (outbox_id) DO NOTHING"
        )
        params = (
            event.event_id,
            event.event_type,
            event.subject_type,
            event.subject_id,
            event.topic,
            # E12 canonical bytes are the one wire form for content-addressed data.
            Jsonb(dict(event.payload), dumps=lambda o: canonical_json(o).decode("utf-8")),
            event.schema_version,
            event.producer,
            event.occurred_at,
        )
        if conn is not None:
            conn.execute(sql, params)
            return event.event_id
        with self._connect() as owned:
            owned.execute(sql, params)
            owned.commit()
        return event.event_id

    def claim_batch(
        self, *, worker_id: str, limit: int, now: datetime, lease_seconds: int = 30
    ) -> list[OutboxRecord]:
        sql = (
            "WITH due AS ("
            "  SELECT outbox_id FROM outbox"
            "  WHERE status = 'PENDING'"
            "     OR (status = 'CLAIMED' AND lease_expires_at < %(now)s)"
            "  ORDER BY produced_at ASC"
            "  LIMIT %(limit)s"
            "  FOR UPDATE SKIP LOCKED"
            ") "
            "UPDATE outbox o SET status = 'CLAIMED', claimed_by = %(worker)s, "
            "claimed_at = %(now)s, "
            "lease_expires_at = %(now)s + make_interval(secs => %(lease)s) "
            "FROM due WHERE o.outbox_id = due.outbox_id "
            f"RETURNING {_OUTBOX_COLUMNS.replace('outbox_id', 'o.outbox_id')}"
        )
        from psycopg.rows import dict_row

        with self._connect() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    sql,
                    {
                        "now": _require_aware(now, "now"),
                        "limit": limit,
                        "worker": worker_id,
                        "lease": lease_seconds,
                    },
                )
                rows = cur.fetchall()
            conn.commit()
        records = [self._row_to_record(row) for row in rows]
        records.sort(key=lambda r: r.produced_at)
        return records

    def mark_published(self, event_id: str, *, worker_id: str, now: datetime) -> None:
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE outbox SET status = 'PUBLISHED', published_at = %s "
                "WHERE outbox_id = %s AND status = 'CLAIMED' AND claimed_by = %s",
                (_require_aware(now, "now"), event_id, worker_id),
            )
            if cur.rowcount != 1:
                conn.rollback()
                raise OutboxError(
                    f"event {event_id!r} is not claimed by {worker_id!r}; publish not recorded"
                )
            conn.commit()

    def release_failed(
        self, event_id: str, *, worker_id: str, error: str, now: datetime
    ) -> None:
        _require_aware(now, "now")
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE outbox SET "
                "status = CASE WHEN attempt_count + 1 >= %(cap)s THEN 'POISON'::outbox_status "
                "ELSE 'PENDING'::outbox_status END, "
                "attempt_count = attempt_count + 1, last_error = %(error)s, "
                "claimed_by = NULL, claimed_at = NULL, lease_expires_at = NULL "
                "WHERE outbox_id = %(event_id)s AND status = 'CLAIMED' "
                "AND claimed_by = %(worker)s",
                {
                    "cap": MAX_ATTEMPTS,
                    "error": error,
                    "event_id": event_id,
                    "worker": worker_id,
                },
            )
            if cur.rowcount != 1:
                conn.rollback()
                raise OutboxError(
                    f"event {event_id!r} is not claimed by {worker_id!r}; release not recorded"
                )
            conn.commit()

    def get(self, event_id: str) -> OutboxRecord | None:
        from psycopg.rows import dict_row

        with self._connect() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    f"SELECT {_OUTBOX_COLUMNS} FROM outbox WHERE outbox_id = %s",
                    (event_id,),
                )
                row = cur.fetchone()
        return None if row is None else self._row_to_record(row)

    def pending_count(self, *, now: datetime) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT count(*) FROM outbox WHERE status = 'PENDING' "
                "OR (status = 'CLAIMED' AND lease_expires_at < %s)",
                (_require_aware(now, "now"),),
            )
            (count,) = cur.fetchone()
        return int(count)


@runtime_checkable
class EventPublisher(Protocol):
    """Publish callback abstraction; the Redpanda producer is wired later."""

    def publish(self, topic: str, envelope: Mapping[str, object]) -> None: ...


class RecordingPublisher:
    """Test publisher that records every (topic, envelope) pair in order.

    ``fail_event_ids`` simulates a broker outage for specific events so the
    retry / POISON paths can be exercised deterministically.
    """

    def __init__(self, *, fail_event_ids: frozenset[str] = frozenset()) -> None:
        self.published: list[tuple[str, dict[str, object]]] = []
        self.fail_event_ids = set(fail_event_ids)

    def publish(self, topic: str, envelope: Mapping[str, object]) -> None:
        if envelope.get("event_id") in self.fail_event_ids:
            raise ConnectionError(f"simulated publish outage for {envelope.get('event_id')}")
        self.published.append((topic, dict(envelope)))


class IdempotentConsumer:
    """Consumer-side dedupe on ``event_id`` (spec/00 §5 consumer rule).

    Wraps a handler; a second delivery of the same ``event_id`` is a recorded
    no-op. At-least-once delivery composes with this to give effectively-once
    handling.
    """

    def __init__(self, handler: Callable[[Mapping[str, object]], None]) -> None:
        self._handler = handler
        self._seen: set[str] = set()
        self.duplicate_count = 0

    def handle(self, envelope: Mapping[str, object]) -> bool:
        event_id = envelope.get("event_id")
        if not isinstance(event_id, str) or not event_id:
            raise OutboxError("envelope is missing a string event_id")
        if event_id in self._seen:
            self.duplicate_count += 1
            return False
        self._handler(envelope)
        self._seen.add(event_id)
        return True


SleepFn = Callable[[float], Awaitable[None]]


@dataclass
class DrainReport:
    """Summary of one drain tick."""

    claimed: int = 0
    published: int = 0
    failed: int = 0
    errors: tuple[str, ...] = field(default_factory=tuple)


class OutboxWorker:
    """Queue-drain loop: claim a batch, publish, mark published.

    At-least-once by construction — ``publish`` happens BEFORE
    ``mark_published``, so a crash between the two re-delivers the event on
    lease expiry; consumers dedupe on ``event_id``. A publish failure releases
    the claim with an incremented attempt count (POISON at the spec/02 cap).
    All time comes from the injected :class:`Clock`.
    """

    def __init__(
        self,
        store: OutboxStore,
        publisher: EventPublisher,
        *,
        clock: Clock,
        worker_id: str,
        batch_size: int = 100,
        lease_seconds: int = 30,
        poll_interval_seconds: float = 0.5,
        sleep: SleepFn | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        if batch_size < 1:
            raise ValueError("batch_size must be >= 1")
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be >= 1")
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be positive")
        self._store = store
        self._publisher = publisher
        self._clock = clock
        self._worker_id = worker_id
        self._batch_size = batch_size
        self._lease_seconds = lease_seconds
        self._poll_interval = float(poll_interval_seconds)
        self._sleep = sleep
        self._log = logger or logging.getLogger("bdpay.platform.outbox")

    def drain_once(self) -> DrainReport:
        """One tick: claim due rows, publish each, settle each row's state."""
        report = DrainReport()
        now = self._clock.now()
        batch = self._store.claim_batch(
            worker_id=self._worker_id,
            limit=self._batch_size,
            now=now,
            lease_seconds=self._lease_seconds,
        )
        report.claimed = len(batch)
        for record in batch:
            event = record.event
            try:
                self._publisher.publish(event.topic, event.to_envelope())
            except Exception as exc:  # noqa: BLE001 - single-event isolation
                message = f"{type(exc).__name__}: {exc}"
                self._log.warning(
                    "outbox publish failed for %s on %s: %s",
                    event.event_id,
                    event.topic,
                    message,
                )
                self._store.release_failed(
                    event.event_id,
                    worker_id=self._worker_id,
                    error=message,
                    now=self._clock.now(),
                )
                report.failed += 1
                report.errors = (*report.errors, message)
                continue
            self._store.mark_published(
                event.event_id, worker_id=self._worker_id, now=self._clock.now()
            )
            report.published += 1
        return report

    async def run(self, *, max_iterations: int | None = None) -> int:
        """Drain repeatedly. ``max_iterations`` is a TEST-ONLY loop guard.

        Returns the total number of published events.
        """
        if self._sleep is None:
            import asyncio

            sleep: SleepFn = asyncio.sleep
        else:
            sleep = self._sleep
        total = 0
        iterations = 0
        while True:
            try:
                total += self.drain_once().published
            except Exception as exc:  # noqa: BLE001 - the loop must survive a bad tick
                self._log.exception(
                    "outbox drain tick raised %s; continuing", type(exc).__name__
                )
            iterations += 1
            if max_iterations is not None and iterations >= max_iterations:
                return total
            await sleep(self._poll_interval)

"""Durable Idempotency-Key store — in-memory implementation + shared logic.

Implements :class:`bdpay.platform.interfaces.IdempotencyPort` (reserve /
claim / complete) against the platform-owned ``idempotency_keys`` table shape
(migration 0002 — referenced, never redefined; the Postgres implementation
lives in :mod:`bdpay.gateway.pg`). Scope key is
``(idempotency_key, scope_id, request_path)`` with NULL ``scope_id`` rows
deduplicating together (errata PR-3a's NULLS NOT DISTINCT semantics).
``scope_id`` is the authenticated principal's stable identity; ``key_id`` is
retained only for the deferred FK to ``api_keys``.

Outcomes (spec/01 middleware algorithm + spec/00 §4):

- first sighting -> ``NEW`` (row inserted IN_FLIGHT)
- IN_FLIGHT duplicate -> ``IN_FLIGHT`` (caller answers 409 conflict /
  ``idempotency_processing``)
- COMPLETED + same request hash -> ``REPLAY`` (stored response verbatim;
  oversized bodies replay as 409 ``idempotency_large_response`` because only
  the hash was stored)
- COMPLETED + different hash -> ``CONFLICT`` (caller answers 422, errata G-2)

A mismatched replay does NOT mutate the stored record (errata G-7): the
COMPLETED row stays the truth so a later correct replay still succeeds.
``release`` (beyond the port surface) drops an IN_FLIGHT reservation after a
5xx so clients can safely retry (errata G-5).
"""

from __future__ import annotations

import json
import threading
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from bdpay.platform.canonical import canonical_json, sha256_canonical
from bdpay.platform.ids import make_id
from bdpay.platform.interfaces import IdempotencyReservation

__all__ = ["InMemoryIdempotencyStore", "StoredIdempotencyRecord", "make_idem_id", "scope_key"]


def make_idem_id(
    *,
    idempotency_key: str,
    scope_id: str | None,
    request_path: str,
    request_body_hash: str,
) -> str:
    """Content-addressed ``idem_<...>`` id (spec/01 middleware algorithm)."""
    return make_id(
        "idem",
        {
            "idempotency_key": idempotency_key,
            "scope_id": scope_id,
            "request_path": request_path,
            "request_body_hash": request_body_hash,
        },
    )


def scope_key(
    idempotency_key: str, scope_id: str | None, request_path: str
) -> tuple[str, str, str]:
    """The unique scope (NULL scope_id folds to '' — NULLS NOT DISTINCT)."""
    return (idempotency_key, scope_id or "", request_path)


@dataclass(frozen=True)
class StoredIdempotencyRecord:
    """One ``idempotency_keys`` row (migration 0002 + 0158 column set)."""

    idem_id: str
    idempotency_key: str
    key_id: str | None
    scope_id: str | None
    customer_id: str | None
    http_method: str
    request_path: str
    request_body_hash: str
    state: str  # IN_FLIGHT | COMPLETED | CONFLICTED
    created_at: datetime
    expires_at: datetime
    response_status: int | None = None
    response_body_hash: str | None = None
    response_body: Mapping[str, object] | None = None
    locked_at: datetime | None = None
    completed_at: datetime | None = None
    schema_version: int = 1


def _reservation_for(
    record: StoredIdempotencyRecord, request_body_hash: str
) -> IdempotencyReservation:
    if record.state == "IN_FLIGHT":
        return IdempotencyReservation(outcome="IN_FLIGHT", idem_id=record.idem_id)
    if record.request_body_hash != request_body_hash:
        return IdempotencyReservation(outcome="CONFLICT", idem_id=record.idem_id)
    return IdempotencyReservation(
        outcome="REPLAY",
        idem_id=record.idem_id,
        response_status=record.response_status,
        response_body=record.response_body,
    )


class InMemoryIdempotencyStore:
    """Deterministic in-memory IdempotencyPort implementation (unit tests)."""

    def __init__(
        self, *, ttl_seconds: int = 86_400, max_stored_response_bytes: int = 65_536
    ) -> None:
        if ttl_seconds <= 0 or max_stored_response_bytes <= 0:
            raise ValueError("ttl_seconds and max_stored_response_bytes must be positive")
        self._ttl = timedelta(seconds=ttl_seconds)
        self._max_body = max_stored_response_bytes
        self._rows: dict[tuple[str, str, str], StoredIdempotencyRecord] = {}
        self._lock = threading.Lock()

    def reserve(
        self,
        *,
        idempotency_key: str,
        key_id: str | None,
        scope_id: str | None,
        customer_id: str | None,
        http_method: str,
        request_path: str,
        request_body_hash: str,
        now: datetime,
    ) -> IdempotencyReservation:
        if not idempotency_key:
            raise ValueError("idempotency_key must be non-empty")
        scope = scope_key(idempotency_key, scope_id, request_path)
        with self._lock:
            existing = self._rows.get(scope)
            if existing is not None and existing.expires_at <= now:
                del self._rows[scope]
                existing = None
            if existing is not None:
                return _reservation_for(existing, request_body_hash)
            idem_id = make_idem_id(
                idempotency_key=idempotency_key,
                scope_id=scope_id,
                request_path=request_path,
                request_body_hash=request_body_hash,
            )
            self._rows[scope] = StoredIdempotencyRecord(
                idem_id=idem_id,
                idempotency_key=idempotency_key,
                key_id=key_id,
                scope_id=scope_id,
                customer_id=customer_id,
                http_method=http_method,
                request_path=request_path,
                request_body_hash=request_body_hash,
                state="IN_FLIGHT",
                created_at=now,
                expires_at=now + self._ttl,
            )
            return IdempotencyReservation(outcome="NEW", idem_id=idem_id)

    def claim(
        self,
        *,
        idempotency_key: str,
        key_id: str | None,
        scope_id: str | None,
        request_path: str,
        now: datetime,
    ) -> bool:
        scope = scope_key(idempotency_key, scope_id, request_path)
        with self._lock:
            record = self._rows.get(scope)
            if record is None or record.state != "IN_FLIGHT" or record.locked_at is not None:
                return False
            self._rows[scope] = replace(record, locked_at=now)
            return True

    def complete(
        self,
        *,
        idempotency_key: str,
        key_id: str | None,
        scope_id: str | None,
        request_path: str,
        response_status: int,
        response_body: Mapping[str, object],
        now: datetime,
    ) -> None:
        scope = scope_key(idempotency_key, scope_id, request_path)
        with self._lock:
            record = self._rows.get(scope)
            if record is None:
                raise ValueError("no reservation exists for this idempotency scope")
            body_bytes = canonical_json(dict(response_body))
            stored_body: Mapping[str, object] | None = json.loads(body_bytes)
            if len(body_bytes) > self._max_body:
                stored_body = None  # replay answers 409 idempotency_large_response
            self._rows[scope] = replace(
                record,
                state="COMPLETED",
                response_status=response_status,
                response_body=stored_body,
                response_body_hash=sha256_canonical(dict(response_body)),
                completed_at=now,
                locked_at=None,
            )

    def release(
        self, *, idempotency_key: str, key_id: str | None, scope_id: str | None, request_path: str
    ) -> None:
        """Drop an IN_FLIGHT reservation (5xx outcome — retries stay possible)."""
        scope = scope_key(idempotency_key, scope_id, request_path)
        with self._lock:
            record = self._rows.get(scope)
            if record is not None and record.state == "IN_FLIGHT":
                del self._rows[scope]

    def get_record(
        self, *, idempotency_key: str, key_id: str | None, scope_id: str | None, request_path: str
    ) -> StoredIdempotencyRecord | None:
        """Test/inspection helper."""
        return self._rows.get(scope_key(idempotency_key, scope_id, request_path))

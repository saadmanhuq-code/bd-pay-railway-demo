"""Outbound merchant webhooks — endpoints, signing, rotation, delivery FSM.

Implements the spec/01 §I/§Signing surface that spec/16 LR-5 consumes, plus
the LR-5 §D signing-secret rotation with the dual-read grace window:

- :class:`WebhookEndpointService` — register/list endpoints (HTTPS-only, max
  10 ACTIVE per merchant, closed event catalogue) and ``rotate_secret``:
  a new ``whsec_`` secret (shown once), previous secret valid for
  ``WEBHOOK_SECRET_GRACE_HOURS``; rotation while a grace window is open is
  refused (``409 rotation_grace_in_progress``).
- :func:`build_signature_header` — ``t={ts},v1={sig_new}[,v1={sig_old}]``;
  during grace EVERY delivery carries both ``v1`` entries (spec/16 §D).
  :func:`verify_signature_header` is the verifier contract (constant-time,
  accepts ANY matching ``v1``, rejects ``|now - t| > 300s``) — also the
  plugin-contract reference implementation (spec/16 §H rule 3).
- :class:`WebhookDeliveryService` — per-event fan-out to enabled endpoints,
  the spec/01 delivery FSM (PENDING → DELIVERED | RETRYING → … → EXHAUSTED
  after 8 attempts, exponential backoff), and the ``webhook.delivery_exhausted``
  outbox event at exhaustion (which LR-5 turns into the merchant email).

Secrets at rest (errata E-S16-06): spec/01 stores only ``sha256(secret)``,
but the outbound signer must produce an HMAC at delivery time, so the secret
is ALSO stored AES-256-GCM-encrypted under the gateway master key — the same
pattern spec/01 §A binds for API-key HMAC material. The raw secret is still
shown exactly once.

Storage follows the E25 precedent: protocol + in-memory implementation here;
the durable table ships in migration 0084 and a Postgres store plugs into the
same protocol. Retry backoff is the spec/01 schedule with the jitter term
injectable (zero by default — determinism for replay/tests).
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlsplit

from bdpay.gateway.credentials import decrypt_blob, encrypt_blob, sha256_hex
from bdpay.platform.clock import Clock
from bdpay.platform.errors import (
    ConflictError,
    InvalidRequestError,
    NotFoundError,
)
from bdpay.platform.outbox import OutboxEvent, build_event
from bdpay.platform.pii import redact
from bdpay.security.outbound_allowlist import pinned_request

__all__ = [
    "MAX_ACTIVE_ENDPOINTS_PER_MERCHANT",
    "MAX_DELIVERY_ATTEMPTS",
    "MERCHANT_WEBHOOK_EVENT_CATALOGUE",
    "RETRY_DELAYS_SECONDS",
    "SIGNATURE_TIMESTAMP_TOLERANCE_SECONDS",
    "HttpxWebhookTransport",
    "InMemoryWebhookEndpointStore",
    "PostgresWebhookEndpointStore",
    "RecordingWebhookTransport",
    "WebhookDeliveryRecord",
    "WebhookDeliveryService",
    "WebhookEndpointRecord",
    "WebhookEndpointService",
    "WebhookEndpointStore",
    "build_signature_header",
    "generate_webhook_secret",
    "verify_signature_header",
]

_LOG = logging.getLogger("bdpay.gateway.webhooks_out")

#: spec/01 §I catalogue + the spec/16 LR-5 additive rows (binding, closed).
MERCHANT_WEBHOOK_EVENT_CATALOGUE: frozenset[str] = frozenset(
    {
        # spec/01 §I
        "payment_intent.succeeded",
        "payment_intent.failed",
        "refund.succeeded",
        "refund.failed",
        "payment_attempt.charged",
        "merchant.activated",
        "merchant.suspended",
        # spec/16 LR-5 additive catalogue rows
        "settlement_batch.held",
        "settlement_batch.released",
        "payment_intent.deferred",
        "dispute.opened",
        "dispute.resolved",
        "payment_link.paid",
    }
)

MAX_ACTIVE_ENDPOINTS_PER_MERCHANT = 10
MAX_DELIVERY_ATTEMPTS = 8  # spec/01 FSM: EXHAUSTED after attempt 8
SIGNATURE_TIMESTAMP_TOLERANCE_SECONDS = 300

#: spec/01 retry schedule (base delays; the ± jitter term is injectable and
#: zero by default so replay stays deterministic). Index = attempts made.
RETRY_DELAYS_SECONDS: tuple[int, ...] = (0, 30, 120, 600, 1_800, 7_200, 21_600, 86_400)

_SECRET_PREFIX = "whsec_"
_LOCAL_WEBHOOK_HOSTNAMES = frozenset({"localhost", "localhost.localdomain"})
_WEBHOOK_ALLOWED_PORTS = frozenset({443})


def generate_webhook_secret() -> str:
    """``whsec_`` + 64 hex chars of OS entropy (shown exactly once)."""
    return _SECRET_PREFIX + os.urandom(32).hex()


def _ext_id(prefix: str, payload: Mapping[str, object]) -> str:
    """Format-parity content-addressed id for the spec/01 ``whe``/``whd``
    prefixes (E28 shim pattern; collapses to ``make_id`` at fold-in)."""
    from bdpay.platform.canonical import sha256_canonical

    return f"{prefix}_{sha256_canonical(dict(payload))[:24]}"


def _webhook_url_error(message: str, code: str) -> InvalidRequestError:
    return InvalidRequestError(message, code=code)


def _forbidden_webhook_ip(raw: str) -> bool:
    try:
        ip = ipaddress.ip_address(raw)
    except ValueError:
        return False
    return not ip.is_global


def _validate_webhook_url(url: str) -> None:
    """Webhook-specific syntactic policy (scheme/port/userinfo/host shape).

    DNS-dependent policy (resolved-address allowlisting) is NOT done here —
    it lives in :func:`bdpay.security.outbound_allowlist.pinned_request`,
    which resolves once (bounded, off-loop) and pins the validated address
    through the connection.
    """
    parsed = urlsplit(url)
    if parsed.scheme.lower() != "https":
        raise _webhook_url_error(
            "webhook URLs must be HTTPS", "webhook_url_must_be_https"
        )
    if not parsed.netloc or not parsed.hostname:
        raise _webhook_url_error(
            "webhook URLs must include a host", "webhook_url_host_required"
        )
    if parsed.username or parsed.password:
        raise _webhook_url_error(
            "webhook URLs must not include userinfo", "webhook_url_userinfo_refused"
        )
    try:
        port = parsed.port
    except ValueError as exc:
        raise _webhook_url_error(
            "webhook URL port is invalid", "webhook_url_port_invalid"
        ) from exc
    if port is not None and port not in _WEBHOOK_ALLOWED_PORTS:
        raise _webhook_url_error(
            "webhook URL port is not allowed", "webhook_url_port_not_allowed"
        )

    host = parsed.hostname.rstrip(".").lower()
    if host in _LOCAL_WEBHOOK_HOSTNAMES or host.endswith(".localhost"):
        raise _webhook_url_error(
            "webhook URL host is not allowed", "webhook_url_host_not_allowed"
        )
    if _forbidden_webhook_ip(host):
        raise _webhook_url_error(
            "webhook URL IP target is not allowed", "webhook_url_ip_not_allowed"
        )


def _rfc3339(at: datetime) -> str:
    return at.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WebhookEndpointRecord:
    """One ``webhook_endpoints`` row (spec/01 DDL + LR-5/E-S16-06 columns)."""

    webhook_id: str
    merchant_id: str
    url: str
    enabled_events: tuple[str, ...]
    signing_secret_hash: str  # sha256(secret) — spec/01 column
    signing_secret_encrypted: str  # AES-256-GCM under master key (E-S16-06)
    status: str  # ACTIVE | DISABLED | DELETED
    created_at: datetime
    updated_at: datetime
    description: str | None = None
    # LR-5 §D rotation grace (spec/16 additive columns on spec/01's table)
    signing_secret_prev_hash: str | None = None
    signing_secret_prev_encrypted: str | None = None
    secret_rotation_grace_until: datetime | None = None
    schema_version: int = 1


@dataclass(frozen=True)
class WebhookDeliveryRecord:
    """One ``webhook_deliveries`` row (spec/01 DDL; unpartitioned, E-S16-06)."""

    delivery_id: str
    webhook_id: str
    event_id: str
    event_type: str
    payload: Mapping[str, object]  # the signed envelope's ``data.object``
    payload_hash: str
    status: str  # PENDING | RETRYING | DELIVERED | EXHAUSTED
    attempt_count: int
    created_at: datetime
    next_attempt_at: datetime | None = None
    last_attempted_at: datetime | None = None
    last_response_status: int | None = None
    last_error: str | None = None
    delivered_at: datetime | None = None
    schema_version: int = 1


# ---------------------------------------------------------------------------
# Store protocol + in-memory implementation
# ---------------------------------------------------------------------------


@runtime_checkable
class WebhookEndpointStore(Protocol):
    """Storage contract for endpoints + deliveries."""

    def insert_endpoint(self, record: WebhookEndpointRecord) -> None: ...

    def save_endpoint(self, record: WebhookEndpointRecord) -> None: ...

    def get_endpoint(self, webhook_id: str) -> WebhookEndpointRecord | None: ...

    def list_endpoints(self, merchant_id: str) -> list[WebhookEndpointRecord]: ...

    def insert_delivery(self, record: WebhookDeliveryRecord) -> None: ...

    def save_delivery(self, record: WebhookDeliveryRecord) -> None: ...

    def get_delivery(self, delivery_id: str) -> WebhookDeliveryRecord | None: ...

    def find_delivery(self, webhook_id: str, event_id: str) -> WebhookDeliveryRecord | None: ...

    def list_deliveries(
        self, *, webhook_id: str | None = None, status: str | None = None
    ) -> list[WebhookDeliveryRecord]: ...

    def due_deliveries(self, *, now: datetime, limit: int) -> list[WebhookDeliveryRecord]: ...


class InMemoryWebhookEndpointStore:
    """Deterministic in-memory store (insertion-ordered)."""

    def __init__(self) -> None:
        self._endpoints: dict[str, WebhookEndpointRecord] = {}
        self._deliveries: dict[str, WebhookDeliveryRecord] = {}
        self._delivery_order: list[str] = []

    # -- endpoints ----------------------------------------------------------

    def insert_endpoint(self, record: WebhookEndpointRecord) -> None:
        if record.webhook_id in self._endpoints:
            raise ConflictError(f"webhook endpoint {record.webhook_id} exists")
        for existing in self._endpoints.values():
            if (
                existing.merchant_id == record.merchant_id
                and existing.url == record.url
                and existing.status == "ACTIVE"
            ):
                raise ConflictError(
                    "an endpoint with this URL already exists for the merchant",
                    code="webhook_url_exists",
                )
        self._endpoints[record.webhook_id] = record

    def save_endpoint(self, record: WebhookEndpointRecord) -> None:
        if record.webhook_id not in self._endpoints:
            raise NotFoundError(f"unknown webhook endpoint {record.webhook_id}")
        self._endpoints[record.webhook_id] = record

    def get_endpoint(self, webhook_id: str) -> WebhookEndpointRecord | None:
        return self._endpoints.get(webhook_id)

    def list_endpoints(self, merchant_id: str) -> list[WebhookEndpointRecord]:
        return [
            record
            for record in self._endpoints.values()
            if record.merchant_id == merchant_id
        ]

    # -- deliveries -----------------------------------------------------------

    def insert_delivery(self, record: WebhookDeliveryRecord) -> None:
        if record.delivery_id in self._deliveries:
            raise ConflictError(f"delivery {record.delivery_id} exists")
        self._deliveries[record.delivery_id] = record
        self._delivery_order.append(record.delivery_id)

    def save_delivery(self, record: WebhookDeliveryRecord) -> None:
        if record.delivery_id not in self._deliveries:
            raise NotFoundError(f"unknown delivery {record.delivery_id}")
        self._deliveries[record.delivery_id] = record

    def get_delivery(self, delivery_id: str) -> WebhookDeliveryRecord | None:
        return self._deliveries.get(delivery_id)

    def find_delivery(self, webhook_id: str, event_id: str) -> WebhookDeliveryRecord | None:
        for did in self._delivery_order:
            row = self._deliveries[did]
            if row.webhook_id == webhook_id and row.event_id == event_id:
                return row
        return None

    def list_deliveries(
        self, *, webhook_id: str | None = None, status: str | None = None
    ) -> list[WebhookDeliveryRecord]:
        return [
            self._deliveries[did]
            for did in self._delivery_order
            if (webhook_id is None or self._deliveries[did].webhook_id == webhook_id)
            and (status is None or self._deliveries[did].status == status)
        ]

    def due_deliveries(self, *, now: datetime, limit: int) -> list[WebhookDeliveryRecord]:
        out: list[WebhookDeliveryRecord] = []
        for did in self._delivery_order:
            row = self._deliveries[did]
            if (
                row.status in ("PENDING", "RETRYING")
                and row.next_attempt_at is not None
                and row.next_attempt_at <= now
            ):
                out.append(row)
            if len(out) >= limit:
                break
        return out


_WEBHOOK_ENDPOINT_COLUMNS = """
    webhook_id, merchant_id, url, description, signing_secret_hash,
    signing_secret_encrypted, enabled_events, status::text,
    signing_secret_prev_hash, signing_secret_prev_encrypted,
    secret_rotation_grace_until, created_at, updated_at, schema_version
"""

_WEBHOOK_DELIVERY_COLUMNS = """
    delivery_id, webhook_id, event_id, event_type, payload, payload_hash,
    status::text, attempt_count, created_at, next_attempt_at, last_attempted_at,
    last_response_status, last_error, delivered_at, schema_version
"""


def _endpoint_from_row(row: Any) -> WebhookEndpointRecord:
    return WebhookEndpointRecord(
        webhook_id=row[0],
        merchant_id=row[1],
        url=row[2],
        description=row[3],
        signing_secret_hash=row[4],
        signing_secret_encrypted=row[5],
        enabled_events=tuple(row[6] or ()),
        status=row[7],
        signing_secret_prev_hash=row[8],
        signing_secret_prev_encrypted=row[9],
        secret_rotation_grace_until=row[10],
        created_at=row[11],
        updated_at=row[12],
        schema_version=row[13],
    )


def _delivery_from_row(row: Any) -> WebhookDeliveryRecord:
    return WebhookDeliveryRecord(
        delivery_id=row[0],
        webhook_id=row[1],
        event_id=row[2],
        event_type=row[3],
        payload=dict(row[4] or {}),
        payload_hash=row[5],
        status=row[6],
        attempt_count=row[7],
        created_at=row[8],
        next_attempt_at=row[9],
        last_attempted_at=row[10],
        last_response_status=row[11],
        last_error=row[12],
        delivered_at=row[13],
        schema_version=row[14],
    )


class PostgresWebhookEndpointStore:
    """psycopg3 store matching ``db/migrations/0084_gateway_webhook_endpoints.sql``."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def close(self) -> None:
        self._conn.close()

    def _run(self, fn: Callable[[Any], Any]) -> Any:
        try:
            with self._conn.cursor() as cur:
                result = fn(cur)
            self._conn.commit()
            return result
        except Exception:
            self._conn.rollback()
            raise

    def insert_endpoint(self, record: WebhookEndpointRecord) -> None:
        import psycopg

        def op(cur: Any) -> None:
            try:
                cur.execute(
                    """
                    INSERT INTO webhook_endpoints (
                        webhook_id, merchant_id, url, description,
                        signing_secret_hash, signing_secret_encrypted,
                        enabled_events, status, signing_secret_prev_hash,
                        signing_secret_prev_encrypted, secret_rotation_grace_until,
                        created_at, updated_at, schema_version
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s::webhook_endpoint_status,
                              %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        record.webhook_id,
                        record.merchant_id,
                        record.url,
                        record.description,
                        record.signing_secret_hash,
                        record.signing_secret_encrypted,
                        list(record.enabled_events),
                        record.status,
                        record.signing_secret_prev_hash,
                        record.signing_secret_prev_encrypted,
                        record.secret_rotation_grace_until,
                        record.created_at,
                        record.updated_at,
                        record.schema_version,
                    ),
                )
            except psycopg.errors.UniqueViolation as exc:
                raise ConflictError(
                    "webhook endpoint uniqueness constraint violated",
                    code="webhook_url_exists",
                ) from exc

        self._run(op)

    def save_endpoint(self, record: WebhookEndpointRecord) -> None:
        def op(cur: Any) -> None:
            cur.execute(
                """
                UPDATE webhook_endpoints SET
                    url = %s,
                    description = %s,
                    signing_secret_hash = %s,
                    signing_secret_encrypted = %s,
                    enabled_events = %s,
                    status = %s::webhook_endpoint_status,
                    signing_secret_prev_hash = %s,
                    signing_secret_prev_encrypted = %s,
                    secret_rotation_grace_until = %s,
                    updated_at = %s,
                    schema_version = %s
                WHERE webhook_id = %s
                """,
                (
                    record.url,
                    record.description,
                    record.signing_secret_hash,
                    record.signing_secret_encrypted,
                    list(record.enabled_events),
                    record.status,
                    record.signing_secret_prev_hash,
                    record.signing_secret_prev_encrypted,
                    record.secret_rotation_grace_until,
                    record.updated_at,
                    record.schema_version,
                    record.webhook_id,
                ),
            )
            if cur.rowcount != 1:
                raise NotFoundError(
                    f"unknown webhook endpoint {record.webhook_id}",
                    code="webhook_not_found",
                )

        self._run(op)

    def get_endpoint(self, webhook_id: str) -> WebhookEndpointRecord | None:
        def op(cur: Any) -> WebhookEndpointRecord | None:
            cur.execute(
                f"SELECT {_WEBHOOK_ENDPOINT_COLUMNS} FROM webhook_endpoints "
                "WHERE webhook_id = %s",
                (webhook_id,),
            )
            row = cur.fetchone()
            return None if row is None else _endpoint_from_row(row)

        return self._run(op)

    def list_endpoints(self, merchant_id: str) -> list[WebhookEndpointRecord]:
        def op(cur: Any) -> list[WebhookEndpointRecord]:
            cur.execute(
                f"SELECT {_WEBHOOK_ENDPOINT_COLUMNS} FROM webhook_endpoints "
                "WHERE merchant_id = %s ORDER BY created_at, webhook_id",
                (merchant_id,),
            )
            return [_endpoint_from_row(row) for row in cur.fetchall()]

        return self._run(op)

    def insert_delivery(self, record: WebhookDeliveryRecord) -> None:
        import psycopg
        from psycopg.types.json import Jsonb

        def op(cur: Any) -> None:
            try:
                cur.execute(
                    """
                    INSERT INTO webhook_deliveries (
                        delivery_id, webhook_id, event_id, event_type, payload,
                        payload_hash, status, attempt_count, created_at,
                        next_attempt_at, last_attempted_at, last_response_status,
                        last_error, delivered_at, schema_version
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s::webhook_delivery_status,
                              %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        record.delivery_id,
                        record.webhook_id,
                        record.event_id,
                        record.event_type,
                        Jsonb(dict(record.payload)),
                        record.payload_hash,
                        record.status,
                        record.attempt_count,
                        record.created_at,
                        record.next_attempt_at,
                        record.last_attempted_at,
                        record.last_response_status,
                        record.last_error,
                        record.delivered_at,
                        record.schema_version,
                    ),
                )
            except psycopg.errors.UniqueViolation as exc:
                raise ConflictError(
                    "webhook delivery uniqueness constraint violated",
                    code="webhook_delivery_exists",
                ) from exc

        self._run(op)

    def save_delivery(self, record: WebhookDeliveryRecord) -> None:
        from psycopg.types.json import Jsonb

        def op(cur: Any) -> None:
            cur.execute(
                """
                UPDATE webhook_deliveries SET
                    event_type = %s,
                    payload = %s,
                    payload_hash = %s,
                    status = %s::webhook_delivery_status,
                    attempt_count = %s,
                    next_attempt_at = %s,
                    last_attempted_at = %s,
                    last_response_status = %s,
                    last_error = %s,
                    delivered_at = %s,
                    schema_version = %s
                WHERE delivery_id = %s
                """,
                (
                    record.event_type,
                    Jsonb(dict(record.payload)),
                    record.payload_hash,
                    record.status,
                    record.attempt_count,
                    record.next_attempt_at,
                    record.last_attempted_at,
                    record.last_response_status,
                    record.last_error,
                    record.delivered_at,
                    record.schema_version,
                    record.delivery_id,
                ),
            )
            if cur.rowcount != 1:
                raise NotFoundError(f"unknown delivery {record.delivery_id}")

        self._run(op)

    def get_delivery(self, delivery_id: str) -> WebhookDeliveryRecord | None:
        def op(cur: Any) -> WebhookDeliveryRecord | None:
            cur.execute(
                f"SELECT {_WEBHOOK_DELIVERY_COLUMNS} FROM webhook_deliveries "
                "WHERE delivery_id = %s",
                (delivery_id,),
            )
            row = cur.fetchone()
            return None if row is None else _delivery_from_row(row)

        return self._run(op)

    def find_delivery(self, webhook_id: str, event_id: str) -> WebhookDeliveryRecord | None:
        def op(cur: Any) -> WebhookDeliveryRecord | None:
            cur.execute(
                f"SELECT {_WEBHOOK_DELIVERY_COLUMNS} FROM webhook_deliveries "
                "WHERE webhook_id = %s AND event_id = %s",
                (webhook_id, event_id),
            )
            row = cur.fetchone()
            return None if row is None else _delivery_from_row(row)

        return self._run(op)

    def list_deliveries(
        self, *, webhook_id: str | None = None, status: str | None = None
    ) -> list[WebhookDeliveryRecord]:
        def op(cur: Any) -> list[WebhookDeliveryRecord]:
            cur.execute(
                f"""
                SELECT {_WEBHOOK_DELIVERY_COLUMNS} FROM webhook_deliveries
                WHERE (%s::text IS NULL OR webhook_id = %s)
                  AND (
                      %s::webhook_delivery_status IS NULL
                      OR status = %s::webhook_delivery_status
                  )
                ORDER BY created_at, delivery_id
                """,
                (webhook_id, webhook_id, status, status),
            )
            return [_delivery_from_row(row) for row in cur.fetchall()]

        return self._run(op)

    def due_deliveries(self, *, now: datetime, limit: int) -> list[WebhookDeliveryRecord]:
        def op(cur: Any) -> list[WebhookDeliveryRecord]:
            cur.execute(
                f"""
                SELECT {_WEBHOOK_DELIVERY_COLUMNS} FROM webhook_deliveries
                WHERE status IN ('PENDING', 'RETRYING')
                  AND next_attempt_at IS NOT NULL
                  AND next_attempt_at <= %s
                ORDER BY next_attempt_at, created_at, delivery_id
                LIMIT %s
                """,
                (now, limit),
            )
            return [_delivery_from_row(row) for row in cur.fetchall()]

        return self._run(op)


# ---------------------------------------------------------------------------
# Signing (spec/01 §Signing; spec/16 §D dual-read grace)
# ---------------------------------------------------------------------------


def _canonical_payload_str(payload: Mapping[str, object]) -> str:
    """The exact spec/01 preimage serialization (compact, key-sorted)."""
    return json.dumps(dict(payload), separators=(",", ":"), sort_keys=True)


def _sign(secret: str, timestamp: str, payload_str: str) -> str:
    preimage = f"{timestamp}.{payload_str}"
    return hmac.new(secret.encode(), preimage.encode(), hashlib.sha256).hexdigest()


def build_signature_header(
    payload: Mapping[str, object],
    *,
    timestamp: int,
    secret: str,
    previous_secret: str | None = None,
) -> str:
    """``t={ts},v1={sig_new}[,v1={sig_old}]`` (spec/01; spec/16 §D grace)."""
    payload_str = _canonical_payload_str(payload)
    ts = str(int(timestamp))
    header = f"t={ts},v1={_sign(secret, ts, payload_str)}"
    if previous_secret is not None:
        header += f",v1={_sign(previous_secret, ts, payload_str)}"
    return header


def verify_signature_header(
    raw_payload: str,
    header: str,
    secret: str,
    *,
    now_epoch: int,
    tolerance_seconds: int = SIGNATURE_TIMESTAMP_TOLERANCE_SECONDS,
) -> bool:
    """The merchant/plugin verifier contract (spec/01 + spec/16 §H rule 3).

    Constant-time compare; accepts if ANY ``v1`` entry matches; rejects when
    ``|now - t| > tolerance``. Fail-closed on any malformed header.
    """
    try:
        parts = [item.split("=", 1) for item in header.split(",")]
        timestamp = next(value for key, value in parts if key == "t")
        signatures = [value for key, value in parts if key == "v1"]
        ts = int(timestamp)
    except (ValueError, StopIteration):
        return False
    if not signatures or abs(now_epoch - ts) > tolerance_seconds:
        return False
    expected = _sign(secret, str(ts), raw_payload)
    return any(hmac.compare_digest(expected, candidate) for candidate in signatures)


def _redact_obj(value: object) -> object:
    """Recursive PII screen over the webhook ``data.object`` (spec/01)."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, Mapping):
        return {key: _redact_obj(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_redact_obj(item) for item in value]
    return value


# ---------------------------------------------------------------------------
# Endpoint service (register / list / rotate)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EndpointCreated:
    """Registration / rotation result; ``signing_secret`` is shown ONCE."""

    record: WebhookEndpointRecord
    signing_secret: str


class WebhookEndpointService:
    """spec/01 §I subset + the spec/16 §D rotation surface."""

    def __init__(
        self,
        store: WebhookEndpointStore,
        *,
        clock: Clock,
        master_key: bytes,
        grace_hours: int = 24,
        secret_factory: Callable[[], str] = generate_webhook_secret,
    ) -> None:
        if isinstance(grace_hours, bool) or not isinstance(grace_hours, int):
            raise InvalidRequestError("grace_hours must be an int")
        if grace_hours < 1:  # spec/16 §config: default 24, min 1
            raise InvalidRequestError("grace_hours must be >= 1 (spec/16 minimum)")
        self._store = store
        self._clock = clock
        self._master_key = master_key
        self._grace = timedelta(hours=grace_hours)
        self._secret_factory = secret_factory

    # -- registration ---------------------------------------------------------

    def register(
        self,
        *,
        merchant_id: str,
        url: str,
        enabled_events: tuple[str, ...] | list[str],
        description: str | None = None,
    ) -> EndpointCreated:
        if not merchant_id:
            raise InvalidRequestError("merchant_id is required")
        _validate_webhook_url(url)
        events = tuple(dict.fromkeys(enabled_events))  # de-dupe, keep order
        if not events:
            raise InvalidRequestError("enabled_events must be non-empty")
        unknown = sorted(set(events) - MERCHANT_WEBHOOK_EVENT_CATALOGUE)
        if unknown:
            raise InvalidRequestError(
                f"unknown event types {unknown}; the catalogue is closed",
                code="unknown_event_type",
            )
        active = [
            row for row in self._store.list_endpoints(merchant_id) if row.status == "ACTIVE"
        ]
        if len(active) >= MAX_ACTIVE_ENDPOINTS_PER_MERCHANT:
            raise ConflictError(
                "maximum active webhook endpoints reached for this merchant",
                code="webhook_endpoint_limit",
            )
        now = self._clock.now()
        secret = self._secret_factory()
        record = WebhookEndpointRecord(
            webhook_id=_ext_id(
                "whe", {"merchant_id": merchant_id, "url": url, "created_at": now}
            ),
            merchant_id=merchant_id,
            url=url,
            enabled_events=events,
            signing_secret_hash=sha256_hex(secret),
            signing_secret_encrypted=encrypt_blob(self._master_key, secret.encode()),
            status="ACTIVE",
            created_at=now,
            updated_at=now,
            description=description,
        )
        self._store.insert_endpoint(record)
        return EndpointCreated(record=record, signing_secret=secret)

    def get(self, webhook_id: str) -> WebhookEndpointRecord | None:
        return self._store.get_endpoint(webhook_id)

    def list_for_merchant(self, merchant_id: str) -> list[WebhookEndpointRecord]:
        return self._store.list_endpoints(merchant_id)

    def enabled_endpoints(
        self, merchant_id: str, event_type: str
    ) -> list[WebhookEndpointRecord]:
        return [
            row
            for row in self._store.list_endpoints(merchant_id)
            if row.status == "ACTIVE" and event_type in row.enabled_events
        ]

    # -- rotation (spec/16 §D) --------------------------------------------------

    def rotate_secret(self, webhook_id: str, *, merchant_id: str) -> EndpointCreated:
        """New ``whsec_`` secret; old stays valid through the grace window.

        Refused while a prior grace window is open
        (``409 conflict / rotation_grace_in_progress``).
        """
        record = self._store.get_endpoint(webhook_id)
        if record is None or record.merchant_id != merchant_id:
            # unknown OR foreign endpoint: identical refusal (no existence leak)
            raise NotFoundError("webhook endpoint not found", code="webhook_not_found")
        if record.status != "ACTIVE":
            raise ConflictError(
                "only ACTIVE endpoints can rotate their secret",
                code="webhook_not_active",
            )
        now = self._clock.now()
        if (
            record.secret_rotation_grace_until is not None
            and now < record.secret_rotation_grace_until
        ):
            raise ConflictError(
                "a rotation grace window is already in progress for this endpoint",
                code="rotation_grace_in_progress",
            )
        secret = self._secret_factory()
        updated = replace(
            record,
            signing_secret_hash=sha256_hex(secret),
            signing_secret_encrypted=encrypt_blob(self._master_key, secret.encode()),
            signing_secret_prev_hash=record.signing_secret_hash,
            signing_secret_prev_encrypted=record.signing_secret_encrypted,
            secret_rotation_grace_until=now + self._grace,
            updated_at=now,
        )
        self._store.save_endpoint(updated)
        return EndpointCreated(record=updated, signing_secret=secret)

    # -- signing-secret access (delivery path only) ------------------------------

    def signing_secrets(self, record: WebhookEndpointRecord) -> tuple[str, str | None]:
        """``(current, previous_or_None)`` — previous only inside grace."""
        current = decrypt_blob(self._master_key, record.signing_secret_encrypted).decode()
        previous: str | None = None
        if (
            record.signing_secret_prev_encrypted is not None
            and record.secret_rotation_grace_until is not None
            and self._clock.now() < record.secret_rotation_grace_until
        ):
            previous = decrypt_blob(
                self._master_key, record.signing_secret_prev_encrypted
            ).decode()
        return current, previous


# ---------------------------------------------------------------------------
# Delivery service (spec/01 FSM; exhaustion event for LR-5)
# ---------------------------------------------------------------------------


@runtime_checkable
class WebhookTransport(Protocol):
    """HTTPS POST to the merchant endpoint; returns the HTTP status code."""

    async def post(
        self, url: str, body: bytes, headers: Mapping[str, str]
    ) -> int: ...


class RecordingWebhookTransport:
    """Deterministic transport fake/in-process sink.

    ``status_codes`` is consumed per call (default: always 200). Every send
    is recorded as ``(url, body, headers)``.
    """

    def __init__(self, *, status_codes: tuple[int, ...] = ()) -> None:
        self.sent: list[tuple[str, bytes, dict[str, str]]] = []
        self._codes = list(status_codes)

    async def post(self, url: str, body: bytes, headers: Mapping[str, str]) -> int:
        self.sent.append((url, body, dict(headers)))
        return self._codes.pop(0) if self._codes else 200


class HttpxWebhookTransport:
    """Production transport — HTTPS POST with the spec/01 delivery timeouts
    (connect 10s, read 30s). Non-2xx and network errors feed the retry FSM.

    Delivery goes through :func:`~bdpay.security.outbound_allowlist.pinned_request`:
    the destination is resolved once (bounded, off the event loop), every
    resolved address is allowlist-validated, and the connection is pinned to
    the validated address (hostname preserved for Host + TLS verification) —
    no resolve-then-connect rebinding window."""

    def __init__(self, *, connect_timeout: float = 10.0, read_timeout: float = 30.0) -> None:
        self._connect_timeout = connect_timeout
        self._read_timeout = read_timeout

    async def post(self, url: str, body: bytes, headers: Mapping[str, str]) -> int:
        import httpx

        _validate_webhook_url(url)
        timeout = httpx.Timeout(
            connect=self._connect_timeout,
            read=self._read_timeout,
            write=self._read_timeout,
            pool=self._connect_timeout,
        )
        response = await pinned_request(
            "POST", url, headers=dict(headers), content=body, timeout=timeout
        )
        return response.status_code


@runtime_checkable
class OutboxEnqueuePort(Protocol):
    """The one outbox call the delivery service makes at exhaustion."""

    def enqueue(self, event: OutboxEvent, *, conn: Any | None = None) -> str: ...


class WebhookDeliveryService:
    """Creates deliveries for an event and drives them to a terminal state."""

    def __init__(
        self,
        store: WebhookEndpointStore,
        endpoints: WebhookEndpointService,
        transport: WebhookTransport,
        *,
        clock: Clock,
        outbox: OutboxEnqueuePort | None = None,
        jitter_seconds: Callable[[int], int] | None = None,
        on_exhausted: Callable[[WebhookDeliveryRecord, str], None] | None = None,
    ) -> None:
        self._store = store
        self._endpoints = endpoints
        self._transport = transport
        self._clock = clock
        self._outbox = outbox
        self._jitter = jitter_seconds or (lambda attempt: 0)
        self._on_exhausted = on_exhausted

    # -- fan-out (called by the event consumer, sync) ---------------------------

    def dispatch_event(
        self,
        *,
        merchant_id: str,
        event_id: str,
        event_type: str,
        payload: Mapping[str, object],
    ) -> list[WebhookDeliveryRecord]:
        """Create one PENDING delivery per enabled ACTIVE endpoint.

        Idempotent on ``(webhook_id, event_id)`` — a redelivered event never
        creates a second delivery row.
        """
        from bdpay.platform.canonical import sha256_canonical

        created: list[WebhookDeliveryRecord] = []
        clean_payload = _redact_obj(dict(payload))
        for endpoint in self._endpoints.enabled_endpoints(merchant_id, event_type):
            existing = self._store.find_delivery(endpoint.webhook_id, event_id)
            if existing is not None:
                continue
            now = self._clock.now()
            record = WebhookDeliveryRecord(
                delivery_id=_ext_id(
                    "whd",
                    {
                        "webhook_id": endpoint.webhook_id,
                        "event_id": event_id,
                        "attempt": 0,
                    },
                ),
                webhook_id=endpoint.webhook_id,
                event_id=event_id,
                event_type=event_type,
                payload=clean_payload,  # type: ignore[arg-type]
                payload_hash=sha256_canonical(clean_payload),
                status="PENDING",
                attempt_count=0,
                created_at=now,
                next_attempt_at=now,  # attempt 1 is immediate (spec/01)
            )
            self._store.insert_delivery(record)
            created.append(record)
        return created

    # -- delivery loop -----------------------------------------------------------

    def _envelope(
        self, record: WebhookDeliveryRecord, endpoint: WebhookEndpointRecord
    ) -> dict[str, object]:
        """The spec/01 §Signing payload envelope."""
        return {
            "webhook_id": endpoint.webhook_id,
            "delivery_id": record.delivery_id,
            "event_id": record.event_id,
            "type": record.event_type,
            "api_version": "1",
            "created": _rfc3339(record.created_at),
            "data": {"object": dict(record.payload)},
        }

    async def process_due(self, *, limit: int = 50) -> int:
        """Attempt every due delivery once; returns the number attempted."""
        now = self._clock.now()
        attempted = 0
        for record in self._store.due_deliveries(now=now, limit=limit):
            endpoint = self._store.get_endpoint(record.webhook_id)
            if endpoint is None or endpoint.status != "ACTIVE":
                # Endpoint disabled mid-flight: park as EXHAUSTED via the
                # normal terminal path (delivery can never succeed).
                self._exhaust(record, error="endpoint_not_active")
                attempted += 1
                continue
            envelope = self._envelope(record, endpoint)
            payload_str = _canonical_payload_str(envelope)
            timestamp = int(now.timestamp())
            current, previous = self._endpoints.signing_secrets(endpoint)
            header = build_signature_header(
                envelope, timestamp=timestamp, secret=current, previous_secret=previous
            )
            headers = {
                "Content-Type": "application/json",
                "BDPay-Signature": header,
                "BDPay-Event-Type": record.event_type,
                "BDPay-Delivery-Id": record.delivery_id,
                "BDPay-Api-Version": "1",
            }
            attempt_no = record.attempt_count + 1
            try:
                status = await self._transport.post(
                    endpoint.url, payload_str.encode(), headers
                )
                ok = 200 <= int(status) < 300
                error = None if ok else f"http_{int(status)}"
                response_status: int | None = int(status)
            except Exception as exc:  # noqa: BLE001 - network isolation
                ok = False
                error = type(exc).__name__
                response_status = None
            done_at = self._clock.now()
            if ok:
                self._store.save_delivery(
                    replace(
                        record,
                        status="DELIVERED",
                        attempt_count=attempt_no,
                        last_attempted_at=done_at,
                        last_response_status=response_status,
                        next_attempt_at=None,
                        delivered_at=done_at,
                    )
                )
            elif attempt_no >= MAX_DELIVERY_ATTEMPTS:
                self._exhaust(
                    replace(
                        record,
                        attempt_count=attempt_no,
                        last_attempted_at=done_at,
                        last_response_status=response_status,
                    ),
                    error=error or "delivery_failed",
                )
            else:
                delay = RETRY_DELAYS_SECONDS[
                    min(attempt_no, len(RETRY_DELAYS_SECONDS) - 1)
                ] + self._jitter(attempt_no)
                self._store.save_delivery(
                    replace(
                        record,
                        status="RETRYING",
                        attempt_count=attempt_no,
                        last_attempted_at=done_at,
                        last_response_status=response_status,
                        last_error=error,
                        next_attempt_at=done_at + timedelta(seconds=delay),
                    )
                )
            attempted += 1
        return attempted

    def _exhaust(self, record: WebhookDeliveryRecord, *, error: str) -> None:
        """EXHAUSTED terminal + the ``webhook.delivery_exhausted`` event."""
        endpoint = self._store.get_endpoint(record.webhook_id)
        terminal = replace(
            record,
            status="EXHAUSTED",
            last_error=error,
            next_attempt_at=None,
        )
        self._store.save_delivery(terminal)
        _LOG.error(
            "webhook delivery %s EXHAUSTED after %d attempts (event %s)",
            record.delivery_id,
            record.attempt_count,
            record.event_type,
        )
        if self._outbox is not None and endpoint is not None:
            self._outbox.enqueue(
                build_event(
                    event_type="webhook.delivery_exhausted",
                    subject_type="WebhookDelivery",
                    subject_id=record.delivery_id,
                    producer="gateway@1",
                    topic="payment.events",
                    payload={
                        "delivery_id": record.delivery_id,
                        "webhook_id": record.webhook_id,
                        "merchant_id": endpoint.merchant_id,
                        "event_type": record.event_type,
                        "attempt_count": record.attempt_count,
                    },
                    occurred_at=self._clock.now(),
                )
            )
        if self._on_exhausted is not None:
            self._on_exhausted(terminal, error)

    # -- read surface --------------------------------------------------------------

    def list_deliveries(
        self,
        *,
        merchant_id: str,
        webhook_id: str | None = None,
        status: str | None = None,
        limit: int = 20,
        cursor: str | None = None,
    ) -> tuple[list[dict[str, object]], str | None]:
        """GET /v1/webhook-deliveries — merchant-scoped, cursor-paginated."""
        merchant_endpoint_ids = {
            row.webhook_id for row in self._store.list_endpoints(merchant_id)
        }
        if webhook_id is not None and webhook_id not in merchant_endpoint_ids:
            raise NotFoundError("webhook endpoint not found", code="webhook_not_found")
        rows = [
            row
            for row in self._store.list_deliveries(webhook_id=webhook_id, status=status)
            if row.webhook_id in merchant_endpoint_ids
        ]
        offset = int(cursor) if cursor else 0
        page = rows[offset : offset + limit]
        next_cursor = str(offset + limit) if offset + limit < len(rows) else None
        return [self._delivery_view(row) for row in page], next_cursor

    @staticmethod
    def _delivery_view(row: WebhookDeliveryRecord) -> dict[str, object]:
        return {
            "delivery_id": row.delivery_id,
            "webhook_id": row.webhook_id,
            "event_id": row.event_id,
            "event_type": row.event_type,
            "status": row.status,
            "attempt_count": row.attempt_count,
            "last_response_status": row.last_response_status,
            "last_error": row.last_error,
            "created_at": _rfc3339(row.created_at),
            "delivered_at": (
                _rfc3339(row.delivered_at) if row.delivered_at is not None else None
            ),
        }

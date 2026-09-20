"""Postgres (psycopg3) repositories matching migrations 0080-0085 + 0002.

Every repository takes a live psycopg3 connection; methods run their own
small transactions (commit on success, rollback on failure). Marked
``@pytest.mark.integration`` tests exercise these against a throwaway schema;
the unit suite never touches them.

Enum-typed columns receive explicit ``::cast``\\ s because psycopg3 binds str
parameters as ``text``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any

from psycopg import errors as pg_errors
from psycopg.types.json import Jsonb

from bdpay.gateway.apikeys import ApiKeyRecord
from bdpay.gateway.credentials import constant_time_equals, sha256_hex
from bdpay.gateway.idempotency import make_idem_id
from bdpay.gateway.operators import (
    OPERATOR_ROLES,
    OperatorFailureState,
    OperatorSession,
    OperatorTotpEnrollment,
)
from bdpay.platform.canonical import canonical_json, sha256_canonical
from bdpay.platform.errors import AuthenticationError, ConflictError, NotFoundError
from bdpay.platform.interfaces import IdempotencyReservation
from bdpay.platform.notifications import NotificationDispatch, NotificationError

__all__ = [
    "PostgresApiKeyRepository",
    "PostgresIdempotencyStore",
    "PostgresNotificationStore",
    "PostgresOperatorDirectory",
    "PostgresOperatorStore",
]


class _PgBase:
    def __init__(self, conn) -> None:  # type: ignore[no-untyped-def]
        self._conn = conn

    def close(self) -> None:
        self._conn.close()

    def _run(self, fn):  # type: ignore[no-untyped-def]
        try:
            with self._conn.cursor() as cur:
                result = fn(cur)
            self._conn.commit()
            return result
        except Exception:
            self._conn.rollback()
            raise


# ---------------------------------------------------------------------------
# API keys (migration 0080)
# ---------------------------------------------------------------------------

_API_KEY_COLUMNS = """
    key_id, merchant_id, key_name, key_prefix, secret_hash, hmac_key_enc,
    status::text, ip_allowlist, expires_at, rotation_successor,
    rotation_grace_ends_at, last_used_at, created_at, revoked_at, schema_version
"""


def _api_key_from_row(row, scopes: tuple[str, ...]) -> ApiKeyRecord:  # type: ignore[no-untyped-def]
    return ApiKeyRecord(
        key_id=row[0],
        merchant_id=row[1],
        key_name=row[2],
        key_prefix=row[3],
        secret_hash=row[4],
        hmac_key_enc=row[5],
        status=row[6],
        ip_allowlist=tuple(row[7] or ()),
        expires_at=row[8],
        rotation_successor=row[9],
        rotation_grace_ends_at=row[10],
        last_used_at=row[11],
        created_at=row[12],
        revoked_at=row[13],
        schema_version=row[14],
        scopes=scopes,
    )


class PostgresApiKeyRepository(_PgBase):
    """ApiKeyRepository over ``api_keys`` + ``api_key_scopes``."""

    def _insert_record(self, cur, record: ApiKeyRecord) -> None:  # type: ignore[no-untyped-def]
        cur.execute(
            """
            INSERT INTO api_keys (
                key_id, merchant_id, key_name, key_prefix, secret_hash,
                hmac_key_enc, status, ip_allowlist, expires_at,
                rotation_successor, rotation_grace_ends_at, last_used_at,
                created_at, revoked_at, schema_version
            ) VALUES (%s, %s, %s, %s, %s, %s, %s::api_key_status, %s, %s,
                      %s, %s, %s, %s, %s, %s)
            """,
            (
                record.key_id,
                record.merchant_id,
                record.key_name,
                record.key_prefix,
                record.secret_hash,
                record.hmac_key_enc,
                record.status,
                list(record.ip_allowlist),
                record.expires_at,
                record.rotation_successor,
                record.rotation_grace_ends_at,
                record.last_used_at,
                record.created_at,
                record.revoked_at,
                record.schema_version,
            ),
        )
        for scope in record.scopes:
            cur.execute(
                "INSERT INTO api_key_scopes (key_id, scope) VALUES (%s, %s::api_scope)",
                (record.key_id, scope),
            )

    def insert(self, record: ApiKeyRecord, *, conn: Any | None = None) -> None:
        def op(cur):  # type: ignore[no-untyped-def]
            self._insert_record(cur, record)

        # When a caller's connection is threaded in (e.g. from the KYB
        # sign_agreement transaction), run the insert on THAT connection
        # without self-committing so the caller's commit/rollback controls
        # the row's durability (atomic commit-or-rollback with the KYB unit).
        # conn=None preserves the standalone self-committing behaviour.
        if conn is not None:
            with conn.cursor() as cur:
                op(cur)
            return
        self._run(op)

    def save(self, record: ApiKeyRecord) -> None:
        def op(cur):  # type: ignore[no-untyped-def]
            cur.execute(
                """
                UPDATE api_keys SET
                    status = %s::api_key_status,
                    rotation_successor = %s,
                    rotation_grace_ends_at = %s,
                    last_used_at = %s,
                    revoked_at = %s
                WHERE key_id = %s
                """,
                (
                    record.status,
                    record.rotation_successor,
                    record.rotation_grace_ends_at,
                    record.last_used_at,
                    record.revoked_at,
                    record.key_id,
                ),
            )
            if cur.rowcount != 1:
                raise ValueError(f"unknown api key {record.key_id!r}")

        self._run(op)

    def rotate_secret_atomic(
        self,
        key_id: str,
        *,
        merchant_id: str,
        new_secret_hash: str,
        new_hmac_key_enc: str,
    ) -> ApiKeyRecord:
        def op(cur):  # type: ignore[no-untyped-def]
            cur.execute(
                f"""
                UPDATE api_keys SET
                    secret_hash = %s,
                    hmac_key_enc = %s,
                    status = 'ACTIVE'::api_key_status,
                    rotation_successor = NULL,
                    rotation_grace_ends_at = NULL
                WHERE key_id = %s
                  AND merchant_id = %s
                  AND status = 'ACTIVE'
                  AND rotation_successor IS NULL
                RETURNING {_API_KEY_COLUMNS}
                """,
                (new_secret_hash, new_hmac_key_enc, key_id, merchant_id),
            )
            row = cur.fetchone()
            if row is not None:
                return _api_key_from_row(row, self._scopes_for(cur, key_id))
            cur.execute(
                """
                SELECT merchant_id, status::text, rotation_successor
                FROM api_keys
                WHERE key_id = %s
                FOR UPDATE
                """,
                (key_id,),
            )
            existing = cur.fetchone()
            if existing is None or existing[0] != merchant_id:
                raise NotFoundError("api key not found", code="api_key_not_found")
            raise ConflictError(
                "only ACTIVE keys without a rotation successor can rotate their secret",
                code="api_key_not_active",
            )

        return self._run(op)

    def rotate_key_atomic(
        self,
        original: ApiKeyRecord,
        successor: ApiKeyRecord,
        *,
        grace_ends_at: datetime,
    ) -> ApiKeyRecord:
        def op(cur):  # type: ignore[no-untyped-def]
            cur.execute(
                f"SELECT {_API_KEY_COLUMNS} FROM api_keys WHERE key_id = %s FOR UPDATE",
                (original.key_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise AuthenticationError("api key not found", code="api_key_invalid")
            current = _api_key_from_row(row, self._scopes_for(cur, original.key_id))
            if current.status != "ACTIVE" or current.rotation_successor is not None:
                raise AuthenticationError(
                    "only ACTIVE keys can rotate (refusal-first FSM)",
                    code="api_key_invalid",
                )
            if (
                current.secret_hash != original.secret_hash
                or current.hmac_key_enc != original.hmac_key_enc
            ):
                raise ConflictError(
                    "api key changed during rotation; retry with the latest key state",
                    code="api_key_rotation_conflict",
                )
            self._insert_record(cur, successor)
            cur.execute(
                """
                UPDATE api_keys SET
                    status = 'ROTATION_PENDING'::api_key_status,
                    rotation_successor = %s,
                    rotation_grace_ends_at = %s
                WHERE key_id = %s
                RETURNING key_id, merchant_id, key_name, key_prefix, secret_hash,
                          hmac_key_enc, status::text, ip_allowlist, expires_at,
                          rotation_successor, rotation_grace_ends_at, last_used_at,
                          created_at, revoked_at, schema_version
                """,
                (successor.key_id, grace_ends_at, original.key_id),
            )
            updated = cur.fetchone()
            if updated is None:
                raise ValueError(f"unknown api key {original.key_id!r}")
            return _api_key_from_row(updated, self._scopes_for(cur, original.key_id))

        return self._run(op)

    def _scopes_for(self, cur, key_id: str) -> tuple[str, ...]:  # type: ignore[no-untyped-def]
        cur.execute(
            "SELECT scope::text FROM api_key_scopes WHERE key_id = %s ORDER BY scope",
            (key_id,),
        )
        return tuple(row[0] for row in cur.fetchall())

    def get(self, key_id: str) -> ApiKeyRecord | None:
        def op(cur):  # type: ignore[no-untyped-def]
            cur.execute(
                f"SELECT {_API_KEY_COLUMNS} FROM api_keys WHERE key_id = %s", (key_id,)
            )
            row = cur.fetchone()
            if row is None:
                return None
            return _api_key_from_row(row, self._scopes_for(cur, key_id))

        return self._run(op)

    def candidates_by_prefix(self, key_prefix: str) -> Sequence[ApiKeyRecord]:
        def op(cur):  # type: ignore[no-untyped-def]
            cur.execute(
                f"""
                SELECT {_API_KEY_COLUMNS} FROM api_keys
                WHERE key_prefix = %s AND status IN ('ACTIVE', 'ROTATION_PENDING')
                ORDER BY created_at DESC
                LIMIT 50
                """,
                (key_prefix,),
            )
            rows = cur.fetchall()
            return [_api_key_from_row(row, self._scopes_for(cur, row[0])) for row in rows]

        return self._run(op)

    def list_for_merchant(self, merchant_id: str) -> Sequence[ApiKeyRecord]:
        def op(cur):  # type: ignore[no-untyped-def]
            cur.execute(
                f"""
                SELECT {_API_KEY_COLUMNS} FROM api_keys
                WHERE merchant_id = %s
                ORDER BY created_at DESC
                LIMIT 200
                """,
                (merchant_id,),
            )
            rows = cur.fetchall()
            return [_api_key_from_row(row, self._scopes_for(cur, row[0])) for row in rows]

        return self._run(op)


# ---------------------------------------------------------------------------
# Operator auth (migration 0081)
# ---------------------------------------------------------------------------


class PostgresOperatorStore(_PgBase):
    """OperatorStore over sessions / enrollments / failure counters."""

    def get_enrollment(self, operator_id: str) -> OperatorTotpEnrollment | None:
        def op(cur):  # type: ignore[no-untyped-def]
            cur.execute(
                """
                SELECT operator_id, totp_secret_enc, backup_code_hashes,
                       enrolled_at, last_used_at, schema_version
                FROM operator_totp_enrollments WHERE operator_id = %s
                """,
                (operator_id,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            return OperatorTotpEnrollment(
                operator_id=row[0],
                totp_secret_enc=row[1],
                backup_code_hashes=tuple(row[2] or ()),
                enrolled_at=row[3],
                last_used_at=row[4],
                schema_version=row[5],
            )

        return self._run(op)

    def save_enrollment(self, enrollment: OperatorTotpEnrollment) -> None:
        def op(cur):  # type: ignore[no-untyped-def]
            cur.execute(
                """
                INSERT INTO operator_totp_enrollments (
                    operator_id, totp_secret_enc, backup_code_hashes,
                    enrolled_at, last_used_at, schema_version
                ) VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (operator_id) DO UPDATE SET
                    totp_secret_enc = EXCLUDED.totp_secret_enc,
                    backup_code_hashes = EXCLUDED.backup_code_hashes,
                    enrolled_at = EXCLUDED.enrolled_at,
                    last_used_at = EXCLUDED.last_used_at
                """,
                (
                    enrollment.operator_id,
                    enrollment.totp_secret_enc,
                    list(enrollment.backup_code_hashes),
                    enrollment.enrolled_at,
                    enrollment.last_used_at,
                    enrollment.schema_version,
                ),
            )

        self._run(op)

    def insert_session(self, session: OperatorSession) -> None:
        def op(cur):  # type: ignore[no-untyped-def]
            cur.execute(
                """
                INSERT INTO operator_sessions (
                    session_id, operator_id, jti, roles, ip_hash, user_agent_hash,
                    status, created_at, expires_at, last_activity_at,
                    totp_verified_at, terminated_at, termination_reason, schema_version
                ) VALUES (%s, %s, %s, %s::operator_role[], %s, %s,
                          %s::operator_session_status, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    session.session_id,
                    session.operator_id,
                    session.jti,
                    list(session.roles),
                    session.ip_hash,
                    session.user_agent_hash,
                    session.status,
                    session.created_at,
                    session.expires_at,
                    session.last_activity_at,
                    session.totp_verified_at,
                    session.terminated_at,
                    session.termination_reason,
                    session.schema_version,
                ),
            )

        self._run(op)

    def save_session(self, session: OperatorSession) -> None:
        def op(cur):  # type: ignore[no-untyped-def]
            cur.execute(
                """
                UPDATE operator_sessions SET
                    status = %s::operator_session_status,
                    last_activity_at = %s,
                    terminated_at = %s,
                    termination_reason = %s
                WHERE jti = %s
                """,
                (
                    session.status,
                    session.last_activity_at,
                    session.terminated_at,
                    session.termination_reason,
                    session.jti,
                ),
            )
            if cur.rowcount != 1:
                raise ValueError("unknown session")

        self._run(op)

    def get_session_by_jti(self, jti: str) -> OperatorSession | None:
        def op(cur):  # type: ignore[no-untyped-def]
            cur.execute(
                """
                SELECT session_id, operator_id, jti, roles::text[], ip_hash,
                       user_agent_hash, status::text, created_at, expires_at,
                       last_activity_at, totp_verified_at, terminated_at,
                       termination_reason, schema_version
                FROM operator_sessions WHERE jti = %s
                """,
                (jti,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            return OperatorSession(
                session_id=row[0],
                operator_id=row[1],
                jti=row[2],
                roles=tuple(row[3] or ()),
                ip_hash=row[4],
                user_agent_hash=row[5],
                status=row[6],
                created_at=row[7],
                expires_at=row[8],
                last_activity_at=row[9],
                totp_verified_at=row[10],
                terminated_at=row[11],
                termination_reason=row[12],
                schema_version=row[13],
            )

        return self._run(op)

    def get_failure_state(self, operator_id: str) -> OperatorFailureState | None:
        def op(cur):  # type: ignore[no-untyped-def]
            cur.execute(
                """
                SELECT operator_id, failed_attempts, window_started_at, locked_until
                FROM operator_auth_failures WHERE operator_id = %s
                """,
                (operator_id,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            return OperatorFailureState(
                operator_id=row[0],
                failed_attempts=row[1],
                window_started_at=row[2],
                locked_until=row[3],
            )

        return self._run(op)

    def save_failure_state(self, state: OperatorFailureState) -> None:
        def op(cur):  # type: ignore[no-untyped-def]
            cur.execute(
                """
                INSERT INTO operator_auth_failures (
                    operator_id, failed_attempts, window_started_at, locked_until
                ) VALUES (%s, %s, %s, %s)
                ON CONFLICT (operator_id) DO UPDATE SET
                    failed_attempts = EXCLUDED.failed_attempts,
                    window_started_at = EXCLUDED.window_started_at,
                    locked_until = EXCLUDED.locked_until
                """,
                (
                    state.operator_id,
                    state.failed_attempts,
                    state.window_started_at,
                    state.locked_until,
                ),
            )

        self._run(op)


class PostgresOperatorDirectory(_PgBase):
    """Durable spec/15 IAM directory for the gateway first-factor check."""

    def register(
        self, operator_id: str, password_token: str, roles: Sequence[str]
    ) -> None:
        """Seed or rotate an operator's pre-2FA password token.

        This mirrors ``InMemoryOperatorDirectory.register`` so tests and
        deployment seed scripts can use the same small API. Raw tokens are never
        stored; the table carries only SHA-256 digests and role enum values.
        """
        if not operator_id:
            raise ValueError("operator_id must be non-empty")
        if not password_token:
            raise ValueError("password_token must be non-empty")
        role_tuple = tuple(roles)
        if not role_tuple:
            raise ValueError("roles must be non-empty")
        unknown = set(role_tuple) - OPERATOR_ROLES
        if unknown:
            raise ValueError(f"unknown roles {sorted(unknown)}; catalogue is closed")
        token_hash = sha256_hex(password_token)

        def op(cur):  # type: ignore[no-untyped-def]
            cur.execute(
                """
                INSERT INTO operator_directory_entries (
                    operator_id, password_token_hash, roles, status,
                    created_at, updated_at, disabled_at, schema_version
                ) VALUES (%s, %s, %s::operator_role[], 'ACTIVE', now(), now(), NULL, 1)
                ON CONFLICT (operator_id) DO UPDATE SET
                    password_token_hash = EXCLUDED.password_token_hash,
                    roles = EXCLUDED.roles,
                    status = 'ACTIVE',
                    updated_at = now(),
                    disabled_at = NULL
                """,
                (operator_id, token_hash, list(role_tuple)),
            )

        self._run(op)

    def verify_password_token(
        self, operator_id: str, password_token: str
    ) -> tuple[str, ...] | None:
        def op(cur):  # type: ignore[no-untyped-def]
            cur.execute(
                """
                SELECT password_token_hash, roles::text[]
                FROM operator_directory_entries
                WHERE operator_id = %s AND status = 'ACTIVE'
                """,
                (operator_id,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            token_hash, roles = row
            if not constant_time_equals(sha256_hex(password_token), token_hash):
                return None
            return tuple(roles or ())

        return self._run(op)


# ---------------------------------------------------------------------------
# Idempotency (platform migration 0002 + 0158 — referenced, never redefined)
# ---------------------------------------------------------------------------

_SCOPE_WHERE = """
    idempotency_key = %s AND scope_id IS NOT DISTINCT FROM %s AND request_path = %s
"""


class PostgresIdempotencyStore(_PgBase):
    """IdempotencyPort over the platform ``idempotency_keys`` table."""

    def __init__(
        self, conn, *, ttl_seconds: int = 86_400, max_stored_response_bytes: int = 65_536
    ) -> None:  # type: ignore[no-untyped-def]
        super().__init__(conn)
        if ttl_seconds <= 0 or max_stored_response_bytes <= 0:
            raise ValueError("ttl_seconds and max_stored_response_bytes must be positive")
        self._ttl = timedelta(seconds=ttl_seconds)
        self._max_body = max_stored_response_bytes

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

        def op(cur):  # type: ignore[no-untyped-def]
            cur.execute(
                f"DELETE FROM idempotency_keys WHERE {_SCOPE_WHERE} AND expires_at <= %s",
                (idempotency_key, scope_id, request_path, now),
            )
            idem_id = make_idem_id(
                idempotency_key=idempotency_key,
                scope_id=scope_id,
                request_path=request_path,
                request_body_hash=request_body_hash,
            )
            cur.execute(
                """
                INSERT INTO idempotency_keys (
                    idem_id, idempotency_key, key_id, scope_id, customer_id, http_method,
                    request_path, request_body_hash, state, created_at, expires_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'IN_FLIGHT', %s, %s)
                ON CONFLICT (idempotency_key, scope_id, request_path) DO NOTHING
                RETURNING idem_id
                """,
                (
                    idem_id,
                    idempotency_key,
                    key_id,
                    scope_id,
                    customer_id,
                    http_method,
                    request_path,
                    request_body_hash,
                    now,
                    now + self._ttl,
                ),
            )
            if cur.fetchone() is not None:
                return IdempotencyReservation(outcome="NEW", idem_id=idem_id)
            cur.execute(
                f"""
                SELECT idem_id, state, request_body_hash, response_status, response_body
                FROM idempotency_keys WHERE {_SCOPE_WHERE}
                """,
                (idempotency_key, scope_id, request_path),
            )
            row = cur.fetchone()
            if row is None:  # concurrent delete; treat as in flight, caller retries
                return IdempotencyReservation(outcome="IN_FLIGHT", idem_id=idem_id)
            existing_id, state, stored_hash, status, body = row
            if state == "IN_FLIGHT":
                return IdempotencyReservation(outcome="IN_FLIGHT", idem_id=existing_id)
            if stored_hash != request_body_hash:
                return IdempotencyReservation(outcome="CONFLICT", idem_id=existing_id)
            return IdempotencyReservation(
                outcome="REPLAY",
                idem_id=existing_id,
                response_status=status,
                response_body=body,
            )

        return self._run(op)

    def claim(
        self,
        *,
        idempotency_key: str,
        key_id: str | None,
        scope_id: str | None,
        request_path: str,
        now: datetime,
    ) -> bool:
        def op(cur):  # type: ignore[no-untyped-def]
            cur.execute(
                f"""
                UPDATE idempotency_keys SET locked_at = %s
                WHERE {_SCOPE_WHERE} AND state = 'IN_FLIGHT' AND locked_at IS NULL
                RETURNING idem_id
                """,
                (now, idempotency_key, scope_id, request_path),
            )
            return cur.fetchone() is not None

        return self._run(op)

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
        body_bytes = canonical_json(dict(response_body))
        stored: object | None = json.loads(body_bytes)
        if len(body_bytes) > self._max_body:
            stored = None  # replay answers 409 idempotency_large_response

        def op(cur):  # type: ignore[no-untyped-def]
            cur.execute(
                f"""
                UPDATE idempotency_keys SET
                    state = 'COMPLETED',
                    response_status = %s,
                    response_body = %s,
                    response_body_hash = %s,
                    completed_at = %s,
                    locked_at = NULL
                WHERE {_SCOPE_WHERE}
                """,
                (
                    response_status,
                    Jsonb(stored) if stored is not None else None,
                    sha256_canonical(dict(response_body)),
                    now,
                    idempotency_key,
                    scope_id,
                    request_path,
                ),
            )
            if cur.rowcount != 1:
                raise ValueError("no reservation exists for this idempotency scope")

        self._run(op)

    def release(
        self, *, idempotency_key: str, key_id: str | None, scope_id: str | None, request_path: str
    ) -> None:
        def op(cur):  # type: ignore[no-untyped-def]
            cur.execute(
                f"DELETE FROM idempotency_keys WHERE {_SCOPE_WHERE} AND state = 'IN_FLIGHT'",
                (idempotency_key, scope_id, request_path),
            )

        self._run(op)


# ---------------------------------------------------------------------------
# Notification dispatches (migration 0082; platform PR-7 plug-in)
# ---------------------------------------------------------------------------

_NDSP_COLUMNS = """
    dispatch_id, idempotency_key, recipient_type, recipient_id, channel::text,
    template_id, template_vars, template_vars_hash, language, status::text,
    attempt_count, next_attempt_at, connector_ref, connector_message_id,
    delivered_at, failed_reason, created_at, updated_at, schema_version
"""


def _dispatch_from_row(row) -> NotificationDispatch:  # type: ignore[no-untyped-def]
    return NotificationDispatch(
        dispatch_id=row[0],
        idempotency_key=row[1],
        recipient_type=row[2],
        recipient_id=row[3],
        channel=row[4],
        template_id=row[5],
        template_vars=dict(row[6] or {}),
        template_vars_hash=row[7],
        language=row[8],
        status=row[9],
        attempt_count=row[10],
        next_attempt_at=row[11],
        connector_ref=row[12],
        connector_message_id=row[13],
        delivered_at=row[14],
        failed_reason=row[15],
        created_at=row[16],
        updated_at=row[17],
        schema_version=row[18],
    )


class PostgresNotificationStore(_PgBase):
    """platform ``NotificationStore`` over ``notification_dispatches``."""

    def insert(self, record: NotificationDispatch) -> None:
        def op(cur):  # type: ignore[no-untyped-def]
            try:
                cur.execute(
                    """
                    INSERT INTO notification_dispatches (
                        dispatch_id, idempotency_key, recipient_type, recipient_id,
                        channel, template_id, template_vars, template_vars_hash,
                        language, status, attempt_count, next_attempt_at,
                        connector_ref, connector_message_id, delivered_at,
                        failed_reason, created_at, updated_at, schema_version
                    ) VALUES (%s, %s, %s, %s, %s::notification_channel, %s, %s, %s,
                              %s, %s::notification_status, %s, %s, %s, %s, %s,
                              %s, %s, %s, %s)
                    """,
                    (
                        record.dispatch_id,
                        record.idempotency_key,
                        record.recipient_type,
                        record.recipient_id,
                        record.channel,
                        record.template_id,
                        Jsonb(dict(record.template_vars)),
                        record.template_vars_hash,
                        record.language,
                        record.status,
                        record.attempt_count,
                        record.next_attempt_at,
                        record.connector_ref,
                        record.connector_message_id,
                        record.delivered_at,
                        record.failed_reason,
                        record.created_at,
                        record.updated_at,
                        record.schema_version,
                    ),
                )
            except pg_errors.UniqueViolation as exc:
                raise NotificationError(
                    f"dispatch {record.dispatch_id!r} already exists"
                ) from exc

        self._run(op)

    def save(self, record: NotificationDispatch) -> None:
        def op(cur):  # type: ignore[no-untyped-def]
            cur.execute(
                """
                UPDATE notification_dispatches SET
                    status = %s::notification_status,
                    attempt_count = %s,
                    next_attempt_at = %s,
                    connector_ref = %s,
                    connector_message_id = %s,
                    delivered_at = %s,
                    failed_reason = %s,
                    updated_at = %s
                WHERE dispatch_id = %s
                """,
                (
                    record.status,
                    record.attempt_count,
                    record.next_attempt_at,
                    record.connector_ref,
                    record.connector_message_id,
                    record.delivered_at,
                    record.failed_reason,
                    record.updated_at,
                    record.dispatch_id,
                ),
            )
            if cur.rowcount != 1:
                raise NotificationError(f"unknown dispatch {record.dispatch_id!r}")

        self._run(op)

    def get(self, dispatch_id: str) -> NotificationDispatch | None:
        def op(cur):  # type: ignore[no-untyped-def]
            cur.execute(
                f"SELECT {_NDSP_COLUMNS} FROM notification_dispatches WHERE dispatch_id = %s",
                (dispatch_id,),
            )
            row = cur.fetchone()
            return None if row is None else _dispatch_from_row(row)

        return self._run(op)

    def by_idempotency_key(self, idempotency_key: str) -> NotificationDispatch | None:
        def op(cur):  # type: ignore[no-untyped-def]
            cur.execute(
                f"""
                SELECT {_NDSP_COLUMNS} FROM notification_dispatches
                WHERE idempotency_key = %s
                """,
                (idempotency_key,),
            )
            row = cur.fetchone()
            return None if row is None else _dispatch_from_row(row)

        return self._run(op)

    def due(self, *, now: datetime, limit: int) -> list[NotificationDispatch]:
        def op(cur):  # type: ignore[no-untyped-def]
            cur.execute(
                f"""
                SELECT {_NDSP_COLUMNS} FROM notification_dispatches
                WHERE status = 'QUEUED' AND next_attempt_at IS NOT NULL
                  AND next_attempt_at <= %s
                ORDER BY next_attempt_at
                LIMIT %s
                """,
                (now, limit),
            )
            return [_dispatch_from_row(row) for row in cur.fetchall()]

        return self._run(op)

"""Postgres-backed customer OTP session store."""

from __future__ import annotations

import os
import secrets
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from bdpay.gateway.credentials import sha256_hex

__all__ = ["PostgresOtpSessionStore"]


class PostgresOtpSessionStore:
    """Durable OTP sessions with hashed tokens and single-use verification."""

    def __init__(self, connection_factory: Callable[[], Any], *, ttl_seconds: int = 300) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self._connect = connection_factory
        self._ttl = timedelta(seconds=ttl_seconds)

    def issue(self, customer_id: str, *, now: datetime) -> str:
        if not customer_id:
            raise ValueError("customer_id must be non-empty")
        token = os.urandom(16).hex()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO otp_sessions (
                    token_hash,
                    customer_id,
                    issued_at,
                    used,
                    attempt_count,
                    schema_version
                )
                VALUES (%s, %s, %s, FALSE, 0, 1)
                """,
                (sha256_hex(token), customer_id, now),
            )
            conn.commit()
        return token

    def verify(self, token: str, *, customer_id: str, now: datetime) -> bool:
        digest = sha256_hex(token)
        with self._connect() as conn:
            from psycopg.rows import dict_row

            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """
                    SELECT customer_id, issued_at, used
                    FROM otp_sessions
                    WHERE token_hash = %s
                    FOR UPDATE
                    """,
                    (digest,),
                )
                row = cur.fetchone()
                if row is None:
                    conn.commit()
                    return False

                issued_at = _aware(row["issued_at"])
                customer_matches = secrets.compare_digest(str(row["customer_id"]), str(customer_id))
                valid = bool(
                    customer_matches
                    and not row["used"]
                    and issued_at is not None
                    and now - issued_at <= self._ttl
                )
                cur.execute(
                    """
                    UPDATE otp_sessions
                    SET used = CASE WHEN %s THEN TRUE ELSE used END,
                        attempt_count = attempt_count + 1
                    WHERE token_hash = %s
                    """,
                    (valid, digest),
                )
                conn.commit()
                return valid


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None or dt.tzinfo is not None:
        return dt
    return dt.replace(tzinfo=UTC)

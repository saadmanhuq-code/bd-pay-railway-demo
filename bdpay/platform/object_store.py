"""Shared Postgres-backed binary object store."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from typing import Any

from bdpay.platform.errors import ConflictError

__all__ = ["PostgresObjectStore"]


class PostgresObjectStore:
    """Write-once object store keyed by deterministic pointers."""

    def __init__(self, connection_factory: Callable[[], Any]) -> None:
        self._connect = connection_factory

    def put(self, key: str, data: bytes) -> str:
        if not key:
            raise ValueError("object key must be non-empty")
        if not isinstance(data, bytes):
            raise TypeError(f"object store data must be bytes, got {type(data).__name__}")
        digest = hashlib.sha256(data).hexdigest()
        with self._connect() as conn:
            inserted = conn.execute(
                """
                INSERT INTO object_blobs (object_key, data, sha256_hex, size_bytes)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (object_key) DO NOTHING
                RETURNING sha256_hex
                """,
                (key, data, digest, len(data)),
            ).fetchone()
            if inserted is None:
                row = conn.execute(
                    "SELECT sha256_hex FROM object_blobs WHERE object_key = %s",
                    (key,),
                ).fetchone()
                if row is None or row[0] != digest:
                    raise ConflictError(
                        f"object {key!r} already exists with different bytes",
                        code="object_conflict",
                    )
            conn.commit()
        return key

    def get(self, key: str) -> bytes | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT data FROM object_blobs WHERE object_key = %s",
                (key,),
            ).fetchone()
        if row is None:
            return None
        return bytes(row[0])

    def exists(self, key: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM object_blobs WHERE object_key = %s",
                (key,),
            ).fetchone()
        return row is not None

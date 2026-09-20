"""Durable MFS rail-handle store (CONN-02 / BP-G1).

bKash ``paymentID``, Nagad ``paymentReferenceId``, and Rocket ``sessionkey``
must survive process restart. Without them, ``query_status`` / IPN recovery
returns ``not_received`` for a payment that may already have succeeded at the
provider.

Pattern mirrors NPSB ``PostgresStanStore``: Protocol + in-memory default +
Postgres impl keyed by ``(connector_id, connector_ref)``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from bdpay.connectors.mfs.spec12_ids import make_spec12_id
from bdpay.platform.clock import Clock

__all__ = [
    "InMemoryMfsHandleStore",
    "MfsPaymentHandle",
    "MfsHandleStore",
    "PostgresMfsHandleStore",
]

ConnectionFactory = Callable[[], Any]


@dataclass(frozen=True)
class MfsPaymentHandle:
    """One ``connector_mfs_payment_handles`` row."""

    handle_id: str
    connector_id: str
    connector_ref: str
    rail_handle: str
    created_at: datetime
    updated_at: datetime
    schema_version: int = 1


@runtime_checkable
class MfsHandleStore(Protocol):
    """``connector_ref`` → provider rail handle (paymentID / paymentReferenceId / sessionkey)."""

    def put(
        self,
        connector_id: str,
        connector_ref: str,
        rail_handle: str,
        *,
        clock: Clock,
    ) -> MfsPaymentHandle: ...

    def get(self, connector_id: str, connector_ref: str) -> str | None: ...

    def get_row(self, connector_id: str, connector_ref: str) -> MfsPaymentHandle | None: ...


class InMemoryMfsHandleStore:
    """Process-local handle map. Pass a shared ``backing`` dict to simulate restart."""

    def __init__(
        self,
        backing: dict[tuple[str, str], MfsPaymentHandle] | None = None,
    ) -> None:
        self._rows: dict[tuple[str, str], MfsPaymentHandle] = (
            backing if backing is not None else {}
        )

    def put(
        self,
        connector_id: str,
        connector_ref: str,
        rail_handle: str,
        *,
        clock: Clock,
    ) -> MfsPaymentHandle:
        key = (connector_id, connector_ref)
        now = clock.now()
        existing = self._rows.get(key)
        if existing is not None and existing.rail_handle == rail_handle:
            return existing
        row = MfsPaymentHandle(
            handle_id=make_spec12_id(
                "mhndl",
                {"connector_id": connector_id, "connector_ref": connector_ref},
            ),
            connector_id=connector_id,
            connector_ref=connector_ref,
            rail_handle=rail_handle,
            created_at=existing.created_at if existing is not None else now,
            updated_at=now,
        )
        self._rows[key] = row
        return row

    def get(self, connector_id: str, connector_ref: str) -> str | None:
        row = self._rows.get((connector_id, connector_ref))
        return None if row is None else row.rail_handle

    def get_row(self, connector_id: str, connector_ref: str) -> MfsPaymentHandle | None:
        return self._rows.get((connector_id, connector_ref))


class PostgresMfsHandleStore:
    """Postgres-backed MFS rail handles (migration ``0161_mfs_payment_handles``)."""

    def __init__(self, connection_factory: ConnectionFactory) -> None:
        self._connect = connection_factory

    @staticmethod
    def _to_row(row: Any) -> MfsPaymentHandle:
        return MfsPaymentHandle(
            handle_id=row["handle_id"],
            connector_id=row["connector_id"],
            connector_ref=row["connector_ref"],
            rail_handle=row["rail_handle"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            schema_version=int(row["schema_version"]),
        )

    def put(
        self,
        connector_id: str,
        connector_ref: str,
        rail_handle: str,
        *,
        clock: Clock,
    ) -> MfsPaymentHandle:
        from psycopg.rows import dict_row

        now = clock.now()
        handle_id = make_spec12_id(
            "mhndl",
            {"connector_id": connector_id, "connector_ref": connector_ref},
        )
        with self._connect() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """
                    INSERT INTO connector_mfs_payment_handles (
                        handle_id, connector_id, connector_ref, rail_handle,
                        created_at, updated_at, schema_version
                    ) VALUES (%s, %s, %s, %s, %s, %s, 1)
                    ON CONFLICT (connector_id, connector_ref) DO UPDATE SET
                        rail_handle = EXCLUDED.rail_handle,
                        updated_at = EXCLUDED.updated_at
                    RETURNING handle_id, connector_id, connector_ref, rail_handle,
                              created_at, updated_at, schema_version
                    """,
                    (handle_id, connector_id, connector_ref, rail_handle, now, now),
                )
                row = cur.fetchone()
            conn.commit()
        assert row is not None
        return self._to_row(row)

    def get(self, connector_id: str, connector_ref: str) -> str | None:
        row = self.get_row(connector_id, connector_ref)
        return None if row is None else row.rail_handle

    def get_row(self, connector_id: str, connector_ref: str) -> MfsPaymentHandle | None:
        from psycopg.rows import dict_row

        with self._connect() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """
                    SELECT handle_id, connector_id, connector_ref, rail_handle,
                           created_at, updated_at, schema_version
                    FROM connector_mfs_payment_handles
                    WHERE connector_id = %s AND connector_ref = %s
                    """,
                    (connector_id, connector_ref),
                )
                row = cur.fetchone()
        return None if row is None else self._to_row(row)

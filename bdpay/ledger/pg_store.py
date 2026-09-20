"""Postgres 16 LedgerStore (psycopg3, sync).

Ported from bd-pay-fable-pilot/src/bdpay_pilot/ledger/pg_store.py (verified
pilot), extended with audit_events and sum_balance_by_subtype, matching
db/migrations/0010_ledger_core.sql exactly.

Semantics are identical to InMemoryLedgerStore; Postgres additionally
enforces append-only via role privileges + RLS and sum-to-zero via the
deferred constraint trigger.

E2 (binding): the spec's chain-tip lock was ``SELECT ... FOR UPDATE NOWAIT``,
but every row-locking clause in Postgres requires UPDATE privilege — which
the spec's own append-only RLS/privilege model denies to ledger_app_role.
Appends are therefore serialized with a transaction-scoped advisory lock per
chain_domain (``pg_advisory_xact_lock``), which needs no UPDATE privilege, is
released automatically on commit/rollback, and provides the same
serialization.

``connect(dsn, role=...)`` supports the NOLOGIN application role pattern:
the migration creates ``ledger_app_role`` / ``ledger_ro`` as NOLOGIN roles
(no credentials in DDL) and the store issues ``SET ROLE`` after connecting,
so privilege + RLS enforcement applies to every statement on the connection.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from contextlib import AbstractContextManager
from datetime import datetime
from typing import Any

import psycopg
import psycopg.errors

from bdpay.ledger.errors import LedgerDuplicateError, LedgerDuplicateEventError
from bdpay.ledger.types import (
    AccountHoldRow,
    AccountRow,
    AuditEventRow,
    ChainEntryRow,
    ChainTip,
    CheckpointRow,
    JournalEntryRow,
    OutboxRow,
    PostingRow,
    TrialBalance,
    WriteGateState,
)

_CHAIN_COLUMNS = (
    "chain_domain, chain_index, entry_type, payload_hash, payload_pointer, "
    "prev_chain_hash, amount_minor_sum, chain_hash, is_checkpoint, "
    "checkpoint_sequence_in_domain, checkpoint_sig, checkpoint_public_key_b64, "
    "producer, produced_at, schema_version"
)

_ACCOUNT_COLUMNS = (
    "account_id, account_type, account_subtype, currency, owner_id, owner_type, "
    "parent_id, purpose_tag, is_system, created_at, closed_at, schema_version"
)

_JE_COLUMNS = (
    "entry_id, entry_index, reference_id, reference_type, entry_type, description, "
    "produced_by, produced_at, idempotency_key, schema_version"
)

_POSTING_COLUMNS = (
    "posting_id, entry_id, account_id, side, amount_minor, currency, produced_at, schema_version"
)

_CHECKPOINT_COLUMNS = (
    "checkpoint_id, chain_domain, checkpoint_sequence_in_domain, chain_index_from, "
    "chain_index_to, chain_hash_at_checkpoint, checkpoint_sig, checkpoint_public_key_b64, "
    "entries_in_segment, signed_at, schema_version"
)

_AUDIT_COLUMNS = (
    "event_id, entry_index, event_type, actor_id, actor_type, subject_type, subject_id, "
    "from_state, to_state, payload_hash, payload_pointer, payload_json, prev_chain_hash, "
    "chain_hash, pii_redacted, occurred_at, schema_version"
)

_HOLD_COLUMNS = (
    "hold_id, account_id, hold_reserve_account_id, source_account_id, reference_id, "
    "amount_minor, hold_reason, status, hold_opened_at, hold_expires_at, "
    "hold_closed_at, open_je_id, close_je_id, schema_version"
)

_OUTBOX_COLUMNS = (
    "event_id, topic, event_type, subject_type, subject_id, payload_json, "
    "producer, occurred_at, published, schema_version"
)


def _account_from(row: tuple[Any, ...]) -> AccountRow:
    return AccountRow(
        account_id=row[0],
        account_type=row[1],
        account_subtype=row[2],
        currency=row[3],
        owner_id=row[4],
        owner_type=row[5],
        parent_id=row[6],
        purpose_tag=row[7],
        is_system=row[8],
        created_at=row[9],
        closed_at=row[10],
        schema_version=row[11],
    )


def _chain_from(row: tuple[Any, ...]) -> ChainEntryRow:
    return ChainEntryRow(
        chain_domain=row[0],
        chain_index=row[1],
        entry_type=row[2],
        payload_hash=row[3],
        payload_pointer=row[4],
        prev_chain_hash=row[5],
        amount_minor_sum=row[6],
        chain_hash=row[7],
        is_checkpoint=row[8],
        checkpoint_sequence_in_domain=row[9],
        checkpoint_sig=row[10],
        checkpoint_public_key_b64=row[11],
        producer=row[12],
        produced_at=row[13],
        schema_version=row[14],
    )


def _je_from(row: tuple[Any, ...]) -> JournalEntryRow:
    return JournalEntryRow(
        entry_id=row[0],
        entry_index=row[1],
        reference_id=row[2],
        reference_type=row[3],
        entry_type=row[4],
        description=row[5],
        produced_by=row[6],
        produced_at=row[7],
        idempotency_key=row[8],
        schema_version=row[9],
    )


def _posting_from(row: tuple[Any, ...]) -> PostingRow:
    return PostingRow(
        posting_id=row[0],
        entry_id=row[1],
        account_id=row[2],
        side=row[3],
        amount_minor=row[4],
        currency=row[5],
        produced_at=row[6],
        schema_version=row[7],
    )


def _checkpoint_from(row: tuple[Any, ...]) -> CheckpointRow:
    return CheckpointRow(
        checkpoint_id=row[0],
        chain_domain=row[1],
        checkpoint_sequence_in_domain=row[2],
        chain_index_from=row[3],
        chain_index_to=row[4],
        chain_hash_at_checkpoint=row[5],
        checkpoint_sig=row[6],
        checkpoint_public_key_b64=row[7],
        entries_in_segment=row[8],
        signed_at=row[9],
        schema_version=row[10],
    )


def _audit_from(row: tuple[Any, ...]) -> AuditEventRow:
    return AuditEventRow(
        event_id=row[0],
        entry_index=row[1],
        event_type=row[2],
        actor_id=row[3],
        actor_type=row[4],
        subject_type=row[5],
        subject_id=row[6],
        from_state=row[7],
        to_state=row[8],
        payload_hash=row[9],
        payload_pointer=row[10],
        payload_json=row[11],
        prev_chain_hash=row[12],
        chain_hash=row[13],
        pii_redacted=row[14],
        occurred_at=row[15],
        schema_version=row[16],
    )


def _hold_from(row: tuple[Any, ...]) -> AccountHoldRow:
    return AccountHoldRow(
        hold_id=row[0],
        account_id=row[1],
        hold_reserve_account_id=row[2],
        source_account_id=row[3],
        reference_id=row[4],
        amount_minor=row[5],
        hold_reason=row[6],
        status=row[7],
        hold_opened_at=row[8],
        hold_expires_at=row[9],
        hold_closed_at=row[10],
        open_je_id=row[11],
        close_je_id=row[12],
        schema_version=row[13],
    )


def _outbox_from(row: tuple[Any, ...]) -> OutboxRow:
    return OutboxRow(
        event_id=row[0],
        topic=row[1],
        event_type=row[2],
        subject_type=row[3],
        subject_id=row[4],
        payload_json=row[5],
        producer=row[6],
        occurred_at=row[7],
        published=row[8],
        schema_version=row[9],
    )


class _PgTx:
    """Unit of work bound to an open Postgres transaction."""

    def __init__(self, conn: psycopg.Connection[tuple[Any, ...]]) -> None:
        self._conn = conn

    def savepoint(self) -> AbstractContextManager[Any]:
        """Nested SAVEPOINT so duplicate-key failures stay local."""
        return self._conn.transaction()

    # --- reads (inside the tx, so staged writes are visible) ---

    def get_account(self, account_id: str) -> AccountRow | None:
        cur = self._conn.execute(
            f"SELECT {_ACCOUNT_COLUMNS} FROM ledger.accounts WHERE account_id = %s",
            (account_id,),
        )
        row = cur.fetchone()
        return _account_from(row) if row else None

    def find_entry_id_by_idempotency_key(self, idempotency_key: str) -> str | None:
        cur = self._conn.execute(
            "SELECT entry_id FROM ledger.journal_entries WHERE idempotency_key = %s",
            (idempotency_key,),
        )
        row = cur.fetchone()
        return row[0] if row else None

    def journal_entry_exists(self, entry_id: str) -> bool:
        cur = self._conn.execute(
            "SELECT 1 FROM ledger.journal_entries WHERE entry_id = %s", (entry_id,)
        )
        return cur.fetchone() is not None

    def lock_chain_tip(self, chain_domain: str) -> ChainTip | None:
        # Serialize appenders per domain for the duration of this transaction (E2).
        self._conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended('bdpay.ledger_chain.' || %s, 0))",
            (chain_domain,),
        )
        cur = self._conn.execute(
            "SELECT chain_index, chain_hash FROM ledger.ledger_chain "
            "WHERE chain_domain = %s ORDER BY chain_index DESC LIMIT 1",
            (chain_domain,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return ChainTip(chain_index=row[0], chain_hash=row[1])

    def write_gate_state(self) -> WriteGateState:
        return _gate_state(self._conn)

    def get_account_hold(self, hold_id: str, *, lock: bool = False) -> AccountHoldRow | None:
        query = f"SELECT {_HOLD_COLUMNS} FROM ledger.account_holds WHERE hold_id = %s"
        if lock:
            query += " FOR UPDATE"
        cur = self._conn.execute(query, (hold_id,))
        row = cur.fetchone()
        return _hold_from(row) if row else None

    def get_open_account_hold_by_reference(self, reference_id: str) -> AccountHoldRow | None:
        cur = self._conn.execute(
            f"SELECT {_HOLD_COLUMNS} FROM ledger.account_holds "
            "WHERE reference_id = %s AND status = 'OPEN' "
            "ORDER BY hold_opened_at, hold_id LIMIT 1",
            (reference_id,),
        )
        row = cur.fetchone()
        return _hold_from(row) if row else None

    def get_balance(self, account_id: str, *, lock: bool = False) -> int:
        if lock:
            self._conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended('bdpay.ledger_account.' || %s, 0))",
                (account_id,),
            )
        query = "SELECT balance_minor FROM ledger.account_balances WHERE account_id = %s"
        cur = self._conn.execute(query, (account_id,))
        row = cur.fetchone()
        return int(row[0]) if row else 0

    def lock_account_hold_reference(self, reference_id: str) -> None:
        self._conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended('bdpay.account_hold.' || %s, 0))",
            (reference_id,),
        )

    # --- mutations (append-only) ---

    def insert_account(self, row: AccountRow) -> None:
        self._conn.execute(
            "INSERT INTO ledger.accounts (account_id, account_type, account_subtype, currency,"
            " owner_id, owner_type, parent_id, purpose_tag, is_system, created_at, closed_at,"
            " schema_version)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                row.account_id,
                row.account_type,
                row.account_subtype,
                row.currency,
                row.owner_id,
                row.owner_type,
                row.parent_id,
                row.purpose_tag,
                row.is_system,
                row.created_at,
                row.closed_at,
                row.schema_version,
            ),
        )

    def insert_journal_entry(self, row: JournalEntryRow) -> int:
        # Wrap the insert in a nested transaction/savepoint so a unique
        # violation rolls back only this block, leaving any caller-managed
        # outer transaction usable for the idempotent lookup below.
        try:
            with self._conn.transaction():
                cur = self._conn.execute(
                    "INSERT INTO ledger.journal_entries (entry_id, reference_id,"
                    " reference_type, entry_type, description, produced_by, produced_at,"
                    " idempotency_key, schema_version)"
                    " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING entry_index",
                    (
                        row.entry_id,
                        row.reference_id,
                        row.reference_type,
                        row.entry_type,
                        row.description,
                        row.produced_by,
                        row.produced_at,
                        row.idempotency_key,
                        row.schema_version,
                    ),
                )
                result = cur.fetchone()
                assert result is not None  # RETURNING always yields a row on success
                return int(result[0])
        except psycopg.errors.UniqueViolation as exc:
            # The service layer prechecks the idempotency key, but two workers
            # can race and both pass the precheck.  Look up the already-committed
            # entry so the contract exception carries the correct entry_id.
            existing = self.find_entry_id_by_idempotency_key(row.idempotency_key)
            if existing is None:
                raise
            raise LedgerDuplicateError(row.idempotency_key, existing) from exc

    def insert_posting(self, row: PostingRow) -> None:
        self._conn.execute(
            "INSERT INTO ledger.postings (posting_id, entry_id, account_id, side, amount_minor,"
            " currency, produced_at, schema_version) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                row.posting_id,
                row.entry_id,
                row.account_id,
                row.side,
                row.amount_minor,
                row.currency,
                row.produced_at,
                row.schema_version,
            ),
        )

    def insert_chain_entry(self, row: ChainEntryRow) -> None:
        self._conn.execute(
            "INSERT INTO ledger.ledger_chain (chain_domain, chain_index, entry_type,"
            " payload_hash, payload_pointer, prev_chain_hash, amount_minor_sum, chain_hash,"
            " is_checkpoint, checkpoint_sequence_in_domain, checkpoint_sig,"
            " checkpoint_public_key_b64, producer, produced_at, schema_version)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                row.chain_domain,
                row.chain_index,
                row.entry_type,
                row.payload_hash,
                row.payload_pointer,
                row.prev_chain_hash,
                row.amount_minor_sum,
                row.chain_hash,
                row.is_checkpoint,
                row.checkpoint_sequence_in_domain,
                row.checkpoint_sig,
                row.checkpoint_public_key_b64,
                row.producer,
                row.produced_at,
                row.schema_version,
            ),
        )

    def insert_checkpoint(self, row: CheckpointRow) -> None:
        self._conn.execute(
            "INSERT INTO ledger.ledger_checkpoints (checkpoint_id, chain_domain,"
            " checkpoint_sequence_in_domain, chain_index_from, chain_index_to,"
            " chain_hash_at_checkpoint, checkpoint_sig, checkpoint_public_key_b64,"
            " entries_in_segment, signed_at, schema_version)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                row.checkpoint_id,
                row.chain_domain,
                row.checkpoint_sequence_in_domain,
                row.chain_index_from,
                row.chain_index_to,
                row.chain_hash_at_checkpoint,
                row.checkpoint_sig,
                row.checkpoint_public_key_b64,
                row.entries_in_segment,
                row.signed_at,
                row.schema_version,
            ),
        )

    def insert_outbox(self, row: OutboxRow) -> None:
        try:
            self._conn.execute(
                "INSERT INTO ledger.outbox (event_id, topic, event_type, subject_type, subject_id,"
                " payload_json, producer, occurred_at, published, schema_version)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    row.event_id,
                    row.topic,
                    row.event_type,
                    row.subject_type,
                    row.subject_id,
                    row.payload_json,
                    row.producer,
                    row.occurred_at,
                    row.published,
                    row.schema_version,
                ),
            )
        except psycopg.errors.UniqueViolation as exc:
            raise LedgerDuplicateEventError(row.event_id) from exc

    def insert_audit_event(self, row: AuditEventRow) -> int:
        try:
            cur = self._conn.execute(
                "INSERT INTO ledger.audit_events (event_id, event_type, actor_id, actor_type,"
                " subject_type, subject_id, from_state, to_state, payload_hash, payload_pointer,"
                " payload_json, prev_chain_hash, chain_hash, pii_redacted, occurred_at,"
                " schema_version)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
                " RETURNING entry_index",
                (
                    row.event_id,
                    row.event_type,
                    row.actor_id,
                    row.actor_type,
                    row.subject_type,
                    row.subject_id,
                    row.from_state,
                    row.to_state,
                    row.payload_hash,
                    row.payload_pointer,
                    row.payload_json,
                    row.prev_chain_hash,
                    row.chain_hash,
                    row.pii_redacted,
                    row.occurred_at,
                    row.schema_version,
                ),
            )
            result = cur.fetchone()
            assert result is not None
            return int(result[0])
        except psycopg.errors.UniqueViolation as exc:
            raise LedgerDuplicateEventError(row.event_id) from exc

    def insert_account_hold(self, row: AccountHoldRow) -> None:
        self._conn.execute(
            "INSERT INTO ledger.account_holds (hold_id, account_id,"
            " hold_reserve_account_id, source_account_id, reference_id, amount_minor,"
            " hold_reason, status, hold_opened_at, hold_expires_at, hold_closed_at,"
            " open_je_id, close_je_id, schema_version)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                row.hold_id,
                row.account_id,
                row.hold_reserve_account_id,
                row.source_account_id,
                row.reference_id,
                row.amount_minor,
                row.hold_reason,
                row.status,
                row.hold_opened_at,
                row.hold_expires_at,
                row.hold_closed_at,
                row.open_je_id,
                row.close_je_id,
                row.schema_version,
            ),
        )

    def update_account_hold(self, row: AccountHoldRow) -> None:
        self._conn.execute(
            "UPDATE ledger.account_holds SET status = %s, hold_closed_at = %s,"
            " close_je_id = %s, schema_version = %s WHERE hold_id = %s",
            (
                row.status,
                row.hold_closed_at,
                row.close_je_id,
                row.schema_version,
                row.hold_id,
            ),
        )

    def engage_write_gate(self, reason: str, engaged_at: datetime) -> None:
        self._conn.execute(
            "INSERT INTO ledger.write_gate (reason, engaged_at) VALUES (%s, %s)",
            (reason, engaged_at),
        )


def _gate_state(conn: psycopg.Connection[tuple[Any, ...]]) -> WriteGateState:
    cur = conn.execute(
        "SELECT reason, engaged_at FROM ledger.write_gate WHERE cleared_at IS NULL"
        " ORDER BY gate_id DESC LIMIT 1"
    )
    row = cur.fetchone()
    if row is None:
        return WriteGateState(engaged=False)
    return WriteGateState(engaged=True, reason=row[0], engaged_at=row[1])


class PostgresLedgerStore:
    """Sync psycopg3 store over an autocommit connection.

    Reads outside ``transaction()`` are single-statement autocommit; the
    atomic bundle runs inside ``conn.transaction()`` (BEGIN ... COMMIT), and
    the deferred sum-to-zero constraint trigger fires at COMMIT.
    """

    def __init__(
        self,
        conn: psycopg.Connection[tuple[Any, ...]],
        *,
        role: str | None = None,
    ) -> None:
        if not conn.autocommit:
            raise ValueError(
                "PostgresLedgerStore requires an autocommit connection; "
                "transactions are scoped explicitly via transaction()"
            )
        self._conn = conn
        self._role = role

    @classmethod
    def connect(cls, dsn: str, *, role: str | None = None) -> PostgresLedgerStore:
        conn = psycopg.connect(dsn, autocommit=True, options="-c TimeZone=UTC")
        if role is not None:
            # NOLOGIN application-role pattern: enforce the role's privileges
            # + RLS for every statement on this connection.
            conn.execute(psycopg.sql.SQL("SET ROLE {}").format(psycopg.sql.Identifier(role)))
        return cls(conn, role=role)

    def close(self) -> None:
        self._conn.close()

    def ping(self) -> bool:
        """Readiness probe (deploy-rehearsal fix, 2026-06-13): round-trip the
        store's OWN connection. A stopped database — or the stale held
        connection a database bounce leaves behind (psycopg3 does not
        auto-reconnect; the DR runbook restarts the app tier after a
        failover) — raises here, which the gateway ready() handler reports
        as ``ledger: degraded`` with HTTP 503. The previous constant-true
        probe reported ready with the database down (observed live in the
        2026-06-13 DR drill)."""
        self._conn.execute("SELECT 1")
        return True

    @contextlib.contextmanager
    def _role_scope(self, conn: psycopg.Connection[tuple[Any, ...]]) -> Iterator[None]:
        if self._role is None:
            yield
            return
        conn.execute(
            psycopg.sql.SQL("SET LOCAL ROLE {}").format(psycopg.sql.Identifier(self._role))
        )
        try:
            yield
        finally:
            conn.execute("RESET ROLE")

    @contextlib.contextmanager
    def transaction(self, conn: Any | None = None) -> Iterator[_PgTx]:
        if conn is not None:
            with self._role_scope(conn):
                yield _PgTx(conn)
            return
        with self._conn.transaction():
            yield _PgTx(self._conn)

    # --- reads (committed state) ---

    def get_account(self, account_id: str) -> AccountRow | None:
        cur = self._conn.execute(
            f"SELECT {_ACCOUNT_COLUMNS} FROM ledger.accounts WHERE account_id = %s",
            (account_id,),
        )
        row = cur.fetchone()
        return _account_from(row) if row else None

    def get_balance(self, account_id: str) -> int:
        cur = self._conn.execute(
            "SELECT balance_minor FROM ledger.account_balances WHERE account_id = %s",
            (account_id,),
        )
        row = cur.fetchone()
        return int(row[0]) if row else 0

    def get_balance_as_of(self, account_id: str, as_of: datetime) -> int:
        account = self.get_account(account_id)
        if account is None:
            return 0
        cur = self._conn.execute(
            "SELECT COALESCE(SUM(CASE WHEN side = 'DEBIT' THEN amount_minor"
            " ELSE -amount_minor END), 0)"
            " FROM ledger.postings WHERE account_id = %s AND produced_at <= %s",
            (account_id, as_of),
        )
        row = cur.fetchone()
        assert row is not None
        debit_net = int(row[0])
        if account.account_type in ("ASSET", "EXPENSE"):
            return debit_net
        return -debit_net

    def sum_balance_by_subtype(self, account_subtype: str) -> int:
        cur = self._conn.execute(
            "SELECT COALESCE(SUM(b.balance_minor), 0)"
            " FROM ledger.account_balances b"
            " JOIN ledger.accounts a ON a.account_id = b.account_id"
            " WHERE a.account_subtype = %s",
            (account_subtype,),
        )
        row = cur.fetchone()
        assert row is not None
        return int(row[0])

    def trial_balance(self) -> TrialBalance:
        cur = self._conn.execute(
            "SELECT COALESCE(SUM(amount_minor) FILTER (WHERE side = 'DEBIT'), 0),"
            " COALESCE(SUM(amount_minor) FILTER (WHERE side = 'CREDIT'), 0)"
            " FROM ledger.postings"
        )
        row = cur.fetchone()
        assert row is not None
        return TrialBalance(total_debit_minor=int(row[0]), total_credit_minor=int(row[1]))

    def get_journal_entry(self, entry_id: str) -> JournalEntryRow | None:
        cur = self._conn.execute(
            f"SELECT {_JE_COLUMNS} FROM ledger.journal_entries WHERE entry_id = %s",
            (entry_id,),
        )
        row = cur.fetchone()
        return _je_from(row) if row else None

    def list_journal_entries(self, *, limit: int, offset: int = 0) -> list[JournalEntryRow]:
        cur = self._conn.execute(
            f"SELECT {_JE_COLUMNS} FROM ledger.journal_entries"
            " ORDER BY entry_index DESC LIMIT %s OFFSET %s",
            (limit, offset),
        )
        return [_je_from(row) for row in cur.fetchall()]

    def list_journal_entries_by_reference_and_type(
        self, reference_id: str, entry_type: str
    ) -> list[JournalEntryRow]:
        cur = self._conn.execute(
            f"SELECT {_JE_COLUMNS} FROM ledger.journal_entries"
            " WHERE reference_id = %s AND entry_type = %s"
            " ORDER BY entry_index",
            (reference_id, entry_type),
        )
        return [_je_from(row) for row in cur.fetchall()]

    def get_postings_for_entry(self, entry_id: str) -> list[PostingRow]:
        cur = self._conn.execute(
            f"SELECT {_POSTING_COLUMNS} FROM ledger.postings WHERE entry_id = %s"
            " ORDER BY posting_id",
            (entry_id,),
        )
        return [_posting_from(row) for row in cur.fetchall()]

    def count_journal_entries(self) -> int:
        return self._scalar_count("SELECT COUNT(*) FROM ledger.journal_entries")

    def count_postings(self) -> int:
        return self._scalar_count("SELECT COUNT(*) FROM ledger.postings")

    def count_outbox(self) -> int:
        return self._scalar_count("SELECT COUNT(*) FROM ledger.outbox")

    def list_outbox(self) -> list[OutboxRow]:
        cur = self._conn.execute(
            f"SELECT {_OUTBOX_COLUMNS} FROM ledger.outbox ORDER BY occurred_at, event_id"
        )
        return [_outbox_from(row) for row in cur.fetchall()]

    def get_outbox_event(self, event_id: str) -> OutboxRow | None:
        cur = self._conn.execute(
            f"SELECT {_OUTBOX_COLUMNS} FROM ledger.outbox WHERE event_id = %s", (event_id,)
        )
        row = cur.fetchone()
        return _outbox_from(row) if row else None

    def has_outbox_event(self, event_type: str, subject_id: str) -> bool:
        cur = self._conn.execute(
            "SELECT 1 FROM ledger.outbox WHERE event_type = %s AND subject_id = %s LIMIT 1",
            (event_type, subject_id),
        )
        return cur.fetchone() is not None

    def list_outbox_by_subject(self, event_type: str, subject_id: str) -> list[OutboxRow]:
        cur = self._conn.execute(
            f"SELECT {_OUTBOX_COLUMNS} FROM ledger.outbox"
            " WHERE event_type = %s AND subject_id = %s"
            " ORDER BY occurred_at, event_id",
            (event_type, subject_id),
        )
        return [_outbox_from(row) for row in cur.fetchall()]

    def count_audit_events(self) -> int:
        return self._scalar_count("SELECT COUNT(*) FROM ledger.audit_events")

    def get_audit_event(self, event_id: str) -> AuditEventRow | None:
        cur = self._conn.execute(
            f"SELECT {_AUDIT_COLUMNS} FROM ledger.audit_events WHERE event_id = %s",
            (event_id,),
        )
        row = cur.fetchone()
        return _audit_from(row) if row else None

    def get_account_hold(self, hold_id: str) -> AccountHoldRow | None:
        cur = self._conn.execute(
            f"SELECT {_HOLD_COLUMNS} FROM ledger.account_holds WHERE hold_id = %s",
            (hold_id,),
        )
        row = cur.fetchone()
        return _hold_from(row) if row else None

    def get_open_account_hold_by_reference(self, reference_id: str) -> AccountHoldRow | None:
        cur = self._conn.execute(
            f"SELECT {_HOLD_COLUMNS} FROM ledger.account_holds "
            "WHERE reference_id = %s AND status = 'OPEN' "
            "ORDER BY hold_opened_at, hold_id LIMIT 1",
            (reference_id,),
        )
        row = cur.fetchone()
        return _hold_from(row) if row else None

    def list_audit_events(
        self, subject_type: str | None = None, subject_id: str | None = None
    ) -> list[AuditEventRow]:
        clauses = []
        params: list[Any] = []
        if subject_type is not None:
            clauses.append("subject_type = %s")
            params.append(subject_type)
        if subject_id is not None:
            clauses.append("subject_id = %s")
            params.append(subject_id)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        cur = self._conn.execute(
            f"SELECT {_AUDIT_COLUMNS} FROM ledger.audit_events{where} ORDER BY entry_index",
            params,
        )
        return [_audit_from(row) for row in cur.fetchall()]

    def iter_chain(
        self, chain_domain: str, from_index: int = 0, to_index: int | None = None
    ) -> list[ChainEntryRow]:
        if to_index is None:
            cur = self._conn.execute(
                f"SELECT {_CHAIN_COLUMNS} FROM ledger.ledger_chain"
                " WHERE chain_domain = %s AND chain_index >= %s ORDER BY chain_index",
                (chain_domain, from_index),
            )
        else:
            cur = self._conn.execute(
                f"SELECT {_CHAIN_COLUMNS} FROM ledger.ledger_chain"
                " WHERE chain_domain = %s AND chain_index BETWEEN %s AND %s ORDER BY chain_index",
                (chain_domain, from_index, to_index),
            )
        return [_chain_from(row) for row in cur.fetchall()]

    def get_chain_entry(self, chain_domain: str, chain_index: int) -> ChainEntryRow | None:
        cur = self._conn.execute(
            f"SELECT {_CHAIN_COLUMNS} FROM ledger.ledger_chain"
            " WHERE chain_domain = %s AND chain_index = %s",
            (chain_domain, chain_index),
        )
        row = cur.fetchone()
        return _chain_from(row) if row else None

    def max_chain_index(self, chain_domain: str) -> int | None:
        cur = self._conn.execute(
            "SELECT MAX(chain_index) FROM ledger.ledger_chain WHERE chain_domain = %s",
            (chain_domain,),
        )
        row = cur.fetchone()
        return int(row[0]) if row and row[0] is not None else None

    def count_chain_entries(self, chain_domain: str) -> int:
        cur = self._conn.execute(
            "SELECT COUNT(*) FROM ledger.ledger_chain WHERE chain_domain = %s",
            (chain_domain,),
        )
        row = cur.fetchone()
        assert row is not None
        return int(row[0])

    def list_checkpoints(self, chain_domain: str) -> list[CheckpointRow]:
        cur = self._conn.execute(
            f"SELECT {_CHECKPOINT_COLUMNS} FROM ledger.ledger_checkpoints"
            " WHERE chain_domain = %s ORDER BY checkpoint_sequence_in_domain",
            (chain_domain,),
        )
        return [_checkpoint_from(row) for row in cur.fetchall()]

    def write_gate_state(self) -> WriteGateState:
        return _gate_state(self._conn)

    def engage_write_gate(self, reason: str, engaged_at: datetime) -> None:
        self._conn.execute(
            "INSERT INTO ledger.write_gate (reason, engaged_at) VALUES (%s, %s)",
            (reason, engaged_at),
        )

    def _scalar_count(self, sql: str) -> int:
        cur = self._conn.execute(sql)
        row = cur.fetchone()
        assert row is not None
        return int(row[0])

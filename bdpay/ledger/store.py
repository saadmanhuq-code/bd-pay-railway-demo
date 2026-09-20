"""LedgerStore protocol — the unit-of-work boundary shared by both stores.

Ported from bd-pay-fable-pilot/src/bdpay_pilot/ledger/store.py (verified
pilot), extended with the audit-event surface and the subtype balance sum the
TCSA monitor reads (spec/05).

The in-memory and Postgres stores implement EXACTLY these semantics:

- ``transaction()`` yields a LedgerTransaction; every mutation inside is
  atomic: all-or-nothing on context exit (commit) / exception (rollback).
- Journal, posting, chain, checkpoint, outbox, and audit tables are append-only
  from the application's point of view. Account holds are a small mutable FSM
  register: opening inserts the hold, release updates status/close metadata
  after appending the closing journal entry.
- Account balances are maintained transactionally on posting insert with the
  normal-balance rule (ASSET/EXPENSE debit-positive; others credit-positive).
- Per-entry sum-to-zero is re-checked at commit time (the Postgres deferred
  constraint trigger; mirrored by the in-memory commit hook) — defense in
  depth behind the Python-layer check.
"""

from __future__ import annotations

from abc import abstractmethod
from contextlib import AbstractContextManager
from datetime import datetime
from typing import Any, Protocol

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


class LedgerTransaction(Protocol):
    """Mutations + reads that must observe in-flight writes."""

    @abstractmethod
    def savepoint(self) -> AbstractContextManager[Any]:
        """Return a context manager that isolates the enclosed writes.

        For Postgres this is a SAVEPOINT so a duplicate-key error inside the
        block rolls back only the block, not a caller-managed outer transaction.
        For the in-memory store this is a no-op context manager because the
        staged-transaction model already isolates failures.
        """
        ...

    # --- reads (must see staged writes) ---
    def get_account(self, account_id: str) -> AccountRow | None: ...

    def find_entry_id_by_idempotency_key(self, idempotency_key: str) -> str | None: ...

    def journal_entry_exists(self, entry_id: str) -> bool: ...

    def lock_chain_tip(self, chain_domain: str) -> ChainTip | None: ...

    def write_gate_state(self) -> WriteGateState: ...

    def get_account_hold(self, hold_id: str, *, lock: bool = False) -> AccountHoldRow | None: ...

    def get_open_account_hold_by_reference(self, reference_id: str) -> AccountHoldRow | None: ...

    def get_balance(self, account_id: str, *, lock: bool = False) -> int: ...

    # --- mutations (append-only) ---
    def insert_account(self, row: AccountRow) -> None: ...

    def insert_journal_entry(
        self, row: JournalEntryRow
    ) -> int: ...  # returns store-assigned entry_index

    def insert_posting(self, row: PostingRow) -> None: ...

    def insert_chain_entry(self, row: ChainEntryRow) -> None: ...

    def insert_checkpoint(self, row: CheckpointRow) -> None: ...

    def insert_outbox(self, row: OutboxRow) -> None: ...

    def insert_audit_event(
        self, row: AuditEventRow
    ) -> int: ...  # returns store-assigned entry_index

    def insert_account_hold(self, row: AccountHoldRow) -> None: ...

    def update_account_hold(self, row: AccountHoldRow) -> None: ...

    def engage_write_gate(self, reason: str, engaged_at: datetime) -> None: ...


class LedgerStore(Protocol):
    """Store-level reads + the transaction factory."""

    def transaction(self, conn: Any | None = None) -> AbstractContextManager[LedgerTransaction]: ...

    # --- reads (committed state) ---
    def get_account(self, account_id: str) -> AccountRow | None: ...

    def get_balance(self, account_id: str) -> int: ...

    def get_balance_as_of(self, account_id: str, as_of: datetime) -> int: ...

    def sum_balance_by_subtype(self, account_subtype: str) -> int: ...

    def trial_balance(self) -> TrialBalance: ...

    def get_journal_entry(self, entry_id: str) -> JournalEntryRow | None: ...

    def list_journal_entries(self, *, limit: int, offset: int = 0) -> list[JournalEntryRow]: ...

    def list_journal_entries_by_reference_and_type(
        self, reference_id: str, entry_type: str
    ) -> list[JournalEntryRow]: ...

    def get_postings_for_entry(self, entry_id: str) -> list[PostingRow]: ...

    def count_journal_entries(self) -> int: ...

    def count_postings(self) -> int: ...

    def count_outbox(self) -> int: ...

    def list_outbox(self) -> list[OutboxRow]: ...

    def get_outbox_event(self, event_id: str) -> OutboxRow | None: ...

    def has_outbox_event(self, event_type: str, subject_id: str) -> bool: ...

    def list_outbox_by_subject(
        self, event_type: str, subject_id: str
    ) -> list[OutboxRow]: ...

    def count_audit_events(self) -> int: ...

    def get_audit_event(self, event_id: str) -> AuditEventRow | None: ...

    def get_account_hold(self, hold_id: str) -> AccountHoldRow | None: ...

    def get_open_account_hold_by_reference(self, reference_id: str) -> AccountHoldRow | None: ...

    def list_audit_events(
        self, subject_type: str | None = None, subject_id: str | None = None
    ) -> list[AuditEventRow]: ...

    def iter_chain(
        self, chain_domain: str, from_index: int = 0, to_index: int | None = None
    ) -> list[ChainEntryRow]: ...

    def get_chain_entry(self, chain_domain: str, chain_index: int) -> ChainEntryRow | None: ...

    def max_chain_index(self, chain_domain: str) -> int | None: ...

    def count_chain_entries(self, chain_domain: str) -> int: ...

    def list_checkpoints(self, chain_domain: str) -> list[CheckpointRow]: ...

    def write_gate_state(self) -> WriteGateState: ...

    def engage_write_gate(self, reason: str, engaged_at: datetime) -> None: ...

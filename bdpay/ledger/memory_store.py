"""In-memory LedgerStore with semantics identical to the Postgres store.

Ported from bd-pay-fable-pilot/src/bdpay_pilot/ledger/memory_store.py
(verified pilot), extended with audit-event storage and the subtype balance
sum (spec/05 TCSA monitor read).

- transaction(): staged-operations unit of work; commit applies atomically,
  any exception discards everything (mirrors a Postgres transaction).
- Commit re-validates per-entry sum-to-zero (mirror of the DEFERRABLE
  INITIALLY DEFERRED constraint trigger) and uniqueness/FK/CHECK rules
  (mirrors of the table constraints).
- Append-only by construction: the application protocol exposes no UPDATE or
  DELETE.  The ``dba_*`` methods are the test-DBA tamper path ONLY, mirroring
  the privileged Postgres role used by the tamper-evidence tests.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from bdpay.ledger.errors import LedgerDuplicateEventError, LedgerError
from bdpay.ledger.types import (
    HOLD_STATUSES,
    SIDES,
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


class MemoryConstraintError(LedgerError):
    """Mirror of a Postgres constraint violation (unique/FK/CHECK/trigger)."""


@dataclass
class _GateRow:
    reason: str
    engaged_at: datetime
    cleared_at: datetime | None = None


def _normal_balance_delta(account_type: str, side: str, amount_minor: int) -> int:
    if account_type in ("ASSET", "EXPENSE"):
        return amount_minor if side == "DEBIT" else -amount_minor
    return amount_minor if side == "CREDIT" else -amount_minor


class _MemTx:
    """Staged unit of work over the parent store."""

    def __init__(self, store: InMemoryLedgerStore) -> None:
        self._store = store
        self.accounts: dict[str, AccountRow] = {}
        self.journal: dict[str, JournalEntryRow] = {}
        self.journal_by_idem: dict[str, str] = {}
        self.postings: dict[str, PostingRow] = {}
        self.chain: dict[tuple[str, int], ChainEntryRow] = {}
        self.checkpoints: list[CheckpointRow] = []
        self.outbox: dict[str, OutboxRow] = {}
        self.audit: dict[str, AuditEventRow] = {}
        self.holds: dict[str, AccountHoldRow] = {}
        self.gate_rows: list[_GateRow] = []
        self._next_entry_index = store._entry_index_seq + 1
        self._next_audit_index = store._audit_index_seq + 1

    def savepoint(self) -> contextlib.AbstractContextManager[None]:
        return contextlib.nullcontext()

    # --- reads (staged-first) ---

    def get_account(self, account_id: str) -> AccountRow | None:
        return self.accounts.get(account_id) or self._store._accounts.get(account_id)

    def find_entry_id_by_idempotency_key(self, idempotency_key: str) -> str | None:
        return self.journal_by_idem.get(idempotency_key) or self._store._journal_by_idem.get(
            idempotency_key
        )

    def journal_entry_exists(self, entry_id: str) -> bool:
        return entry_id in self.journal or entry_id in self._store._journal

    def lock_chain_tip(self, chain_domain: str) -> ChainTip | None:
        staged = [idx for (dom, idx) in self.chain if dom == chain_domain]
        committed = self._store._chains.get(chain_domain, {})
        best_idx: int | None = None
        if staged:
            best_idx = max(staged)
        if committed:
            committed_max = max(committed)
            best_idx = committed_max if best_idx is None else max(best_idx, committed_max)
        if best_idx is None:
            return None
        row = self.chain.get((chain_domain, best_idx)) or committed[best_idx]
        return ChainTip(chain_index=row.chain_index, chain_hash=row.chain_hash)

    def write_gate_state(self) -> WriteGateState:
        for row in self.gate_rows:
            if row.cleared_at is None:
                return WriteGateState(engaged=True, reason=row.reason, engaged_at=row.engaged_at)
        return self._store.write_gate_state()

    def get_account_hold(self, hold_id: str, *, lock: bool = False) -> AccountHoldRow | None:
        return self.holds.get(hold_id) or self._store._holds.get(hold_id)

    def get_open_account_hold_by_reference(self, reference_id: str) -> AccountHoldRow | None:
        rows = [
            row
            for row in (*self._store._holds.values(), *self.holds.values())
            if row.reference_id == reference_id and row.status == "OPEN"
        ]
        return min(rows, key=lambda row: (row.hold_opened_at, row.hold_id), default=None)

    def get_balance(self, account_id: str, *, lock: bool = False) -> int:
        account = self.get_account(account_id)
        if account is None:
            return 0
        balance = self._store._balances.get(account_id, 0)
        for posting in self.postings.values():
            if posting.account_id == account_id:
                balance += _normal_balance_delta(
                    account.account_type,
                    posting.side,
                    posting.amount_minor,
                )
        return balance

    # --- mutations ---

    def insert_account(self, row: AccountRow) -> None:
        if self.get_account(row.account_id) is not None:
            raise MemoryConstraintError(f"duplicate account_id {row.account_id}")
        self.accounts[row.account_id] = row

    def insert_journal_entry(self, row: JournalEntryRow) -> int:
        if self.journal_entry_exists(row.entry_id):
            raise MemoryConstraintError(f"duplicate entry_id {row.entry_id}")
        if self.find_entry_id_by_idempotency_key(row.idempotency_key) is not None:
            raise MemoryConstraintError(f"duplicate idempotency_key {row.idempotency_key}")
        entry_index = self._next_entry_index
        self._next_entry_index += 1
        stored = JournalEntryRow(
            entry_id=row.entry_id,
            entry_index=entry_index,
            reference_id=row.reference_id,
            reference_type=row.reference_type,
            entry_type=row.entry_type,
            description=row.description,
            produced_by=row.produced_by,
            produced_at=row.produced_at,
            idempotency_key=row.idempotency_key,
            schema_version=row.schema_version,
        )
        self.journal[row.entry_id] = stored
        self.journal_by_idem[row.idempotency_key] = row.entry_id
        return entry_index

    def insert_posting(self, row: PostingRow) -> None:
        # Mirrors of the postings table CHECK constraints:
        if row.posting_id in self.postings or row.posting_id in self._store._postings:
            raise MemoryConstraintError(f"duplicate posting_id {row.posting_id}")
        if row.side not in SIDES:
            raise MemoryConstraintError(f"postings_side_check: {row.side!r}")
        if isinstance(row.amount_minor, bool) or not isinstance(row.amount_minor, int):
            raise MemoryConstraintError("postings amount_minor must be an int (BIGINT)")
        if row.amount_minor <= 0:
            raise MemoryConstraintError("postings_amount_positive: amount_minor must be > 0")
        if row.currency != "BDT":
            raise MemoryConstraintError(f"postings_currency_check: {row.currency!r}")
        if self.get_account(row.account_id) is None:
            raise MemoryConstraintError(f"FK violation: account {row.account_id} absent")
        if not self.journal_entry_exists(row.entry_id):
            raise MemoryConstraintError(f"FK violation: journal entry {row.entry_id} absent")
        self.postings[row.posting_id] = row

    def insert_chain_entry(self, row: ChainEntryRow) -> None:
        key = (row.chain_domain, row.chain_index)
        if key in self.chain or row.chain_index in self._store._chains.get(row.chain_domain, {}):
            raise MemoryConstraintError(f"duplicate chain entry {key}")
        if row.chain_domain not in ("MONEY", "AUDIT"):
            raise MemoryConstraintError(f"chain_domain_check: {row.chain_domain!r}")
        if row.is_checkpoint and (
            row.checkpoint_sig is None
            or row.checkpoint_public_key_b64 is None
            or row.checkpoint_sequence_in_domain is None
        ):
            raise MemoryConstraintError("checkpoint rows require sig + public key + sequence")
        if not row.is_checkpoint and (
            row.checkpoint_sig is not None
            or row.checkpoint_public_key_b64 is not None
            or row.checkpoint_sequence_in_domain is not None
        ):
            raise MemoryConstraintError("non-checkpoint rows must not carry checkpoint fields")
        self.chain[key] = row

    def insert_checkpoint(self, row: CheckpointRow) -> None:
        existing = [
            c
            for c in (*self._store._checkpoints.get(row.chain_domain, []), *self.checkpoints)
            if c.chain_domain == row.chain_domain
            and c.checkpoint_sequence_in_domain == row.checkpoint_sequence_in_domain
        ]
        if existing:
            raise MemoryConstraintError(
                f"duplicate checkpoint sequence {row.checkpoint_sequence_in_domain}"
            )
        if row.entries_in_segment <= 0:
            raise MemoryConstraintError("checkpoints_entries_positive")
        self.checkpoints.append(row)

    def insert_outbox(self, row: OutboxRow) -> None:
        if row.event_id in self.outbox or row.event_id in self._store._outbox:
            raise LedgerDuplicateEventError(row.event_id)
        self.outbox[row.event_id] = row

    def insert_audit_event(self, row: AuditEventRow) -> int:
        if row.event_id in self.audit or row.event_id in self._store._audit:
            raise LedgerDuplicateEventError(row.event_id)
        entry_index = self._next_audit_index
        self._next_audit_index += 1
        import dataclasses

        self.audit[row.event_id] = dataclasses.replace(row, entry_index=entry_index)
        return entry_index

    def insert_account_hold(self, row: AccountHoldRow) -> None:
        if self.get_account_hold(row.hold_id) is not None:
            raise MemoryConstraintError(f"duplicate hold_id {row.hold_id}")
        if self.get_account(row.account_id) is None:
            raise MemoryConstraintError(f"FK violation: hold account {row.account_id} absent")
        if self.get_account(row.hold_reserve_account_id) is None:
            raise MemoryConstraintError(
                f"FK violation: hold reserve {row.hold_reserve_account_id} absent"
            )
        if self.get_account(row.source_account_id) is None:
            raise MemoryConstraintError(f"FK violation: hold source {row.source_account_id} absent")
        if not self.journal_entry_exists(row.open_je_id):
            raise MemoryConstraintError(f"FK violation: open journal {row.open_je_id} absent")
        if row.status not in HOLD_STATUSES:
            raise MemoryConstraintError(f"holds_status_check: {row.status!r}")
        if isinstance(row.amount_minor, bool) or not isinstance(row.amount_minor, int):
            raise MemoryConstraintError("holds amount_minor must be an int (BIGINT)")
        if row.amount_minor <= 0:
            raise MemoryConstraintError("holds_amount_positive: amount_minor must be > 0")
        self.holds[row.hold_id] = row

    def update_account_hold(self, row: AccountHoldRow) -> None:
        current = self.get_account_hold(row.hold_id)
        if current is None:
            raise MemoryConstraintError(f"unknown hold_id {row.hold_id}")
        if row.status not in HOLD_STATUSES:
            raise MemoryConstraintError(f"holds_status_check: {row.status!r}")
        if row.close_je_id is not None and not self.journal_entry_exists(row.close_je_id):
            raise MemoryConstraintError(f"FK violation: close journal {row.close_je_id} absent")
        self.holds[row.hold_id] = row

    def engage_write_gate(self, reason: str, engaged_at: datetime) -> None:
        self.gate_rows.append(_GateRow(reason=reason, engaged_at=engaged_at))

    # --- commit-time validation (mirror of the deferred constraint trigger) ---

    def _deferred_balance_check(self) -> None:
        touched_entries = {p.entry_id for p in self.postings.values()}
        for entry_id in touched_entries:
            debit = credit = 0
            for p in self._all_postings_for_entry(entry_id):
                if p.side == "DEBIT":
                    debit += p.amount_minor
                else:
                    credit += p.amount_minor
            if debit != credit:
                raise MemoryConstraintError(
                    f"Ledger imbalance for entry_id={entry_id}: debit={debit} credit={credit}"
                )

    def _all_postings_for_entry(self, entry_id: str) -> Iterator[PostingRow]:
        for p in self._store._postings.values():
            if p.entry_id == entry_id:
                yield p
        for p in self.postings.values():
            if p.entry_id == entry_id:
                yield p

    def _apply(self) -> None:
        store = self._store
        store._accounts.update(self.accounts)
        store._journal.update(self.journal)
        store._journal_by_idem.update(self.journal_by_idem)
        for posting in self.postings.values():
            store._postings[posting.posting_id] = posting
            store._postings_by_entry.setdefault(posting.entry_id, []).append(posting.posting_id)
            account = store._accounts[posting.account_id]
            delta = _normal_balance_delta(account.account_type, posting.side, posting.amount_minor)
            store._balances[posting.account_id] = store._balances.get(posting.account_id, 0) + delta
        for (domain, index), chain_row in self.chain.items():
            store._chains.setdefault(domain, {})[index] = chain_row
        for checkpoint in self.checkpoints:
            store._checkpoints.setdefault(checkpoint.chain_domain, []).append(checkpoint)
        store._outbox.update(self.outbox)
        store._audit.update(self.audit)
        store._holds.update(self.holds)
        store._gate_rows.extend(self.gate_rows)
        store._entry_index_seq = self._next_entry_index - 1
        store._audit_index_seq = self._next_audit_index - 1


class InMemoryLedgerStore:
    """Fast store for tests; semantics mirror the Postgres store exactly."""

    def __init__(self) -> None:
        self._accounts: dict[str, AccountRow] = {}
        self._journal: dict[str, JournalEntryRow] = {}
        self._journal_by_idem: dict[str, str] = {}
        self._postings: dict[str, PostingRow] = {}
        self._postings_by_entry: dict[str, list[str]] = {}
        self._chains: dict[str, dict[int, ChainEntryRow]] = {}
        self._checkpoints: dict[str, list[CheckpointRow]] = {}
        self._outbox: dict[str, OutboxRow] = {}
        self._audit: dict[str, AuditEventRow] = {}
        self._holds: dict[str, AccountHoldRow] = {}
        self._balances: dict[str, int] = {}
        self._gate_rows: list[_GateRow] = []
        self._entry_index_seq = 0
        self._audit_index_seq = 0

    @contextlib.contextmanager
    def transaction(self, conn: Any | None = None) -> Iterator[_MemTx]:
        del conn
        tx = _MemTx(self)
        yield tx
        # Any exception above propagates and the staged tx is discarded
        # (rollback).  Reaching here means commit:
        tx._deferred_balance_check()
        tx._apply()

    # --- reads ---

    def get_account(self, account_id: str) -> AccountRow | None:
        return self._accounts.get(account_id)

    def get_balance(self, account_id: str) -> int:
        return self._balances.get(account_id, 0)

    def get_balance_as_of(self, account_id: str, as_of: datetime) -> int:
        account = self._accounts.get(account_id)
        if account is None:
            return 0
        balance = 0
        for posting in self._postings.values():
            if posting.account_id == account_id and posting.produced_at <= as_of:
                balance += _normal_balance_delta(
                    account.account_type, posting.side, posting.amount_minor
                )
        return balance

    def sum_balance_by_subtype(self, account_subtype: str) -> int:
        """Signed (normal-balance) sum over all accounts of a subtype."""
        total = 0
        for account in self._accounts.values():
            if account.account_subtype == account_subtype:
                total += self._balances.get(account.account_id, 0)
        return total

    def trial_balance(self) -> TrialBalance:
        debit = credit = 0
        for posting in self._postings.values():
            if posting.side == "DEBIT":
                debit += posting.amount_minor
            else:
                credit += posting.amount_minor
        return TrialBalance(total_debit_minor=debit, total_credit_minor=credit)

    def get_journal_entry(self, entry_id: str) -> JournalEntryRow | None:
        return self._journal.get(entry_id)

    def list_journal_entries(self, *, limit: int, offset: int = 0) -> list[JournalEntryRow]:
        return sorted(self._journal.values(), key=lambda r: r.entry_index, reverse=True)[
            offset : offset + limit
        ]

    def list_journal_entries_by_reference_and_type(
        self, reference_id: str, entry_type: str
    ) -> list[JournalEntryRow]:
        return sorted(
            (
                row
                for row in self._journal.values()
                if row.reference_id == reference_id and row.entry_type == entry_type
            ),
            key=lambda r: r.entry_index,
        )

    def get_postings_for_entry(self, entry_id: str) -> list[PostingRow]:
        return [
            self._postings[pid]
            for pid in self._postings_by_entry.get(entry_id, [])
            if pid in self._postings
        ]

    def count_journal_entries(self) -> int:
        return len(self._journal)

    def count_postings(self) -> int:
        return len(self._postings)

    def count_outbox(self) -> int:
        return len(self._outbox)

    def get_outbox_event(self, event_id: str) -> OutboxRow | None:
        return self._outbox.get(event_id)

    def has_outbox_event(self, event_type: str, subject_id: str) -> bool:
        return any(
            row.event_type == event_type and row.subject_id == subject_id
            for row in self._outbox.values()
        )

    def list_outbox_by_subject(self, event_type: str, subject_id: str) -> list[OutboxRow]:
        return sorted(
            (
                row
                for row in self._outbox.values()
                if row.event_type == event_type and row.subject_id == subject_id
            ),
            key=lambda r: (r.occurred_at, r.event_id),
        )

    def count_audit_events(self) -> int:
        return len(self._audit)

    def get_audit_event(self, event_id: str) -> AuditEventRow | None:
        return self._audit.get(event_id)

    def get_account_hold(self, hold_id: str) -> AccountHoldRow | None:
        return self._holds.get(hold_id)

    def get_open_account_hold_by_reference(self, reference_id: str) -> AccountHoldRow | None:
        rows = [
            row
            for row in self._holds.values()
            if row.reference_id == reference_id and row.status == "OPEN"
        ]
        return min(rows, key=lambda row: (row.hold_opened_at, row.hold_id), default=None)

    def list_audit_events(
        self, subject_type: str | None = None, subject_id: str | None = None
    ) -> list[AuditEventRow]:
        rows = [
            row
            for row in self._audit.values()
            if (subject_type is None or row.subject_type == subject_type)
            and (subject_id is None or row.subject_id == subject_id)
        ]
        return sorted(rows, key=lambda r: r.entry_index)

    def list_outbox(self) -> list[OutboxRow]:
        return sorted(self._outbox.values(), key=lambda r: (r.occurred_at, r.event_id))

    def iter_chain(
        self, chain_domain: str, from_index: int = 0, to_index: int | None = None
    ) -> list[ChainEntryRow]:
        domain = self._chains.get(chain_domain, {})
        indexes = sorted(
            idx for idx in domain if idx >= from_index and (to_index is None or idx <= to_index)
        )
        return [domain[idx] for idx in indexes]

    def get_chain_entry(self, chain_domain: str, chain_index: int) -> ChainEntryRow | None:
        return self._chains.get(chain_domain, {}).get(chain_index)

    def max_chain_index(self, chain_domain: str) -> int | None:
        domain = self._chains.get(chain_domain, {})
        return max(domain) if domain else None

    def count_chain_entries(self, chain_domain: str) -> int:
        return len(self._chains.get(chain_domain, {}))

    def list_checkpoints(self, chain_domain: str) -> list[CheckpointRow]:
        return sorted(
            self._checkpoints.get(chain_domain, []),
            key=lambda c: c.checkpoint_sequence_in_domain,
        )

    def write_gate_state(self) -> WriteGateState:
        for row in self._gate_rows:
            if row.cleared_at is None:
                return WriteGateState(engaged=True, reason=row.reason, engaged_at=row.engaged_at)
        return WriteGateState(engaged=False)

    def engage_write_gate(self, reason: str, engaged_at: datetime) -> None:
        self._gate_rows.append(_GateRow(reason=reason, engaged_at=engaged_at))

    # ------------------------------------------------------------------
    # TEST-DBA TAMPER PATH ONLY (mirror of the privileged Postgres role).
    # Production code never calls these; the tamper-evidence tests do.
    # ------------------------------------------------------------------

    def dba_replace_chain_entry(self, chain_domain: str, chain_index: int, **changes: Any) -> None:
        import dataclasses

        row = self._chains[chain_domain][chain_index]
        self._chains[chain_domain][chain_index] = dataclasses.replace(row, **changes)

    def dba_delete_chain_entry(self, chain_domain: str, chain_index: int) -> None:
        del self._chains[chain_domain][chain_index]

    def dba_replace_posting(self, posting_id: str, **changes: Any) -> None:
        import dataclasses

        row = self._postings[posting_id]
        self._postings[posting_id] = dataclasses.replace(row, **changes)

    def dba_delete_posting(self, posting_id: str) -> None:
        del self._postings[posting_id]

    def dba_delete_journal_entry(self, entry_id: str) -> None:
        del self._journal[entry_id]

    def dba_replace_journal_entry(self, entry_id: str, **changes: Any) -> None:
        import dataclasses

        row = self._journal[entry_id]
        self._journal[entry_id] = dataclasses.replace(row, **changes)

    def dba_insert_journal_entry(self, row: JournalEntryRow) -> None:
        self._entry_index_seq += 1
        import dataclasses

        stored = dataclasses.replace(row, entry_index=self._entry_index_seq)
        self._journal[stored.entry_id] = stored
        self._journal_by_idem[stored.idempotency_key] = stored.entry_id

    def dba_clear_write_gate(self, cleared_at: datetime) -> None:
        for row in self._gate_rows:
            if row.cleared_at is None:
                row.cleared_at = cleared_at

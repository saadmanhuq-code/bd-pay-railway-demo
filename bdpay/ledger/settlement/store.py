"""SettlementStore protocol + deterministic in-memory implementation (spec/04).

Storage pattern per IMPLEMENTATION.md: repository Protocol, in-memory
implementation for the unit suite, Postgres implementation in
``settlement/pg_store.py`` matching db/migrations/0011_settlement.sql.

Rows are immutable dataclasses; updates go through ``update_batch`` /
``update_instruction`` with a whitelisted change set (mirrors a column-list
UPDATE — the FSM tables are mutable state tables, not append-only ledgers).
"""

from __future__ import annotations

import dataclasses
from datetime import date
from typing import Protocol

from bdpay.ledger.errors import SettlementError
from bdpay.ledger.settlement.fees import FeeRule
from bdpay.ledger.settlement.types import (
    BATCH_STATES,
    INSTRUCTION_STATES,
    SettlementBatchRow,
    SettlementInstructionRow,
)

_BATCH_MUTABLE = frozenset(
    {
        "status",
        "instruction_count",
        "total_credit_minor",
        "total_debit_minor",
        "retry_count",
        "two_eyes_required",
        "approval_request_id",
        "held_at",
        "held_tcsa_snapshot_id",
        "sponsor_bank_ref",
        "recon_file_pointer",
        "recon_file_hash",
        "connector_ref",
        "cutoff_at",
        "dispatched_at",
        "confirmed_at",
        "failed_at",
        "cancelled_at",
        "fail_reason_code",
    }
)

_INSTRUCTION_MUTABLE = frozenset(
    {
        "batch_id",
        "status",
        "rail_transaction_id",
        "return_reason_code",
        "earliest_release_at",
        "delivery_hold_cleared",
        "aml_hold",
        "submitted_at",
        "settled_at",
        "returned_at",
        "failed_at",
    }
)


class SettlementStore(Protocol):
    """Persistence boundary for settlement batches, instructions, fee rules."""

    # --- batches ---
    def insert_batch(self, row: SettlementBatchRow) -> None: ...

    def get_batch(self, batch_id: str) -> SettlementBatchRow | None: ...

    def update_batch(self, batch_id: str, **changes: object) -> SettlementBatchRow: ...

    def persist_confirmation_evidence(
        self,
        batch_id: str,
        *,
        sponsor_bank_ref: str,
        rail_transaction_ids: dict[str, str],
    ) -> SettlementBatchRow:
        """Atomically persist the sponsor-bank confirmation marker and the
        per-instruction rail transaction ids before the money-moving journal
        entry.  Either all evidence is durable or none is, so the resume path
        never sees rail ids without the batch marker.
        """
        ...

    def find_open_batch(
        self, rail: str, cycle_date: date, session: str | None
    ) -> SettlementBatchRow | None: ...

    def list_batches(self, status: str | None = None) -> list[SettlementBatchRow]: ...

    # --- instructions ---
    def insert_instruction(self, row: SettlementInstructionRow) -> None: ...

    def get_instruction(self, instruction_id: str) -> SettlementInstructionRow | None: ...

    def update_instruction(
        self, instruction_id: str, **changes: object
    ) -> SettlementInstructionRow: ...

    def list_instructions(
        self,
        batch_id: str | None = None,
        status: str | None = None,
        payment_intent_id: str | None = None,
        unassigned_only: bool = False,
    ) -> list[SettlementInstructionRow]: ...

    def find_instruction_by_rail_txid(
        self, rail_transaction_id: str, *, direction: str | None = None
    ) -> SettlementInstructionRow | None:
        """Return the instruction matching rail_transaction_id.

        When direction is provided (spec/PT-G), the result is further filtered
        so a CREDIT recon line never matches a DEBIT instruction that happens to
        share the same rail transaction id (possible on rails that reuse ids
        across debit/credit legs).
        """
        ...

    def find_instructions_for_match(
        self,
        rail: str,
        amount_minor: int,
        date_from: date,
        date_to: date,
        *,
        direction: str | None = None,
    ) -> list[SettlementInstructionRow]:
        """Level-2 fuzzy match candidates: amount + rail + cycle-date window.

        When direction is provided (spec/PT-G), only instructions with a
        matching direction are returned, preventing a DEBIT instruction from
        matching a CREDIT recon line at the fuzzy level.
        """
        ...

    # --- fee rules ---
    def insert_fee_rule(self, rule: FeeRule) -> None: ...

    def list_fee_rules(self) -> list[FeeRule]: ...


def _checked_changes(allowed: frozenset[str], changes: dict[str, object]) -> dict[str, object]:
    unknown = set(changes) - allowed
    if unknown:
        raise SettlementError(f"non-updatable column(s) {sorted(unknown)}")
    return changes


class InMemorySettlementStore:
    """Deterministic in-memory store; semantics mirror the Postgres store."""

    def __init__(self) -> None:
        self._batches: dict[str, SettlementBatchRow] = {}
        self._instructions: dict[str, SettlementInstructionRow] = {}
        self._fee_rules: dict[str, FeeRule] = {}
        self._order: list[str] = []  # instruction insertion order (deterministic)

    # --- batches ---

    def insert_batch(self, row: SettlementBatchRow) -> None:
        if row.batch_id in self._batches:
            raise SettlementError(f"duplicate batch_id {row.batch_id}")
        if row.status not in BATCH_STATES:
            raise SettlementError(f"unknown batch status {row.status!r}")
        self._batches[row.batch_id] = row

    def get_batch(self, batch_id: str) -> SettlementBatchRow | None:
        return self._batches.get(batch_id)

    def update_batch(self, batch_id: str, **changes: object) -> SettlementBatchRow:
        row = self._batches.get(batch_id)
        if row is None:
            raise SettlementError(f"unknown batch {batch_id}")
        checked = _checked_changes(_BATCH_MUTABLE, changes)
        status = checked.get("status")
        if status is not None and status not in BATCH_STATES:
            raise SettlementError(f"unknown batch status {status!r}")
        updated = dataclasses.replace(row, **checked)  # type: ignore[arg-type]
        self._batches[batch_id] = updated
        return updated

    def persist_confirmation_evidence(
        self,
        batch_id: str,
        *,
        sponsor_bank_ref: str,
        rail_transaction_ids: dict[str, str],
    ) -> SettlementBatchRow:
        batch = self.get_batch(batch_id)
        if batch is None:
            raise SettlementError(f"unknown batch {batch_id}")
        if (
            batch.sponsor_bank_ref is not None
            and batch.sponsor_bank_ref != sponsor_bank_ref
        ):
            raise SettlementError(
                f"batch {batch_id}: sponsor_bank_ref cannot overwrite already-durable "
                f"confirmation evidence ({batch.sponsor_bank_ref!r} -> {sponsor_bank_ref!r})"
            )

        # Pre-validate every instruction so a bad id leaves no durable evidence.
        # A SUBMITTED row is updated; a SETTLED row means a concurrent worker
        # already finished the instruction and we must converge idempotently.
        for instruction_id, _txid in rail_transaction_ids.items():
            row = self._instructions.get(instruction_id)
            if row is None:
                raise SettlementError(f"unknown instruction {instruction_id}")
            if row.batch_id != batch_id:
                raise SettlementError(
                    f"instruction {instruction_id} is not a member of batch {batch_id}"
                )
            if row.status not in ("SUBMITTED", "SETTLED"):
                raise SettlementError(
                    f"instruction {instruction_id} is not a SUBMITTED member of batch {batch_id}"
                )

        batch = self.update_batch(batch_id, sponsor_bank_ref=sponsor_bank_ref)
        for instruction_id, txid in rail_transaction_ids.items():
            inst = self._instructions[instruction_id]
            if inst.status == "SUBMITTED" and inst.rail_transaction_id is None:
                self.update_instruction(instruction_id, rail_transaction_id=txid)
        return batch


    def find_open_batch(
        self, rail: str, cycle_date: date, session: str | None
    ) -> SettlementBatchRow | None:
        for row in self._batches.values():
            if (
                row.rail == rail
                and row.cycle_date == cycle_date
                and row.session == session
                and row.status == "OPEN"
            ):
                return row
        return None

    def list_batches(self, status: str | None = None) -> list[SettlementBatchRow]:
        rows = [r for r in self._batches.values() if status is None or r.status == status]
        return sorted(rows, key=lambda r: (r.created_at, r.batch_id))

    # --- instructions ---

    def insert_instruction(self, row: SettlementInstructionRow) -> None:
        if row.instruction_id in self._instructions:
            raise SettlementError(f"duplicate instruction_id {row.instruction_id}")
        if row.status not in INSTRUCTION_STATES:
            raise SettlementError(f"unknown instruction status {row.status!r}")
        if any(
            existing.connector_ref == row.connector_ref
            for existing in self._instructions.values()
        ):
            raise SettlementError(f"duplicate connector_ref {row.connector_ref}")
        if row.net_payout_minor != row.amount_minor - row.fee_minor:
            raise SettlementError("net_payout_minor must equal amount_minor - fee_minor")
        self._instructions[row.instruction_id] = row
        self._order.append(row.instruction_id)

    def get_instruction(self, instruction_id: str) -> SettlementInstructionRow | None:
        return self._instructions.get(instruction_id)

    def update_instruction(
        self, instruction_id: str, **changes: object
    ) -> SettlementInstructionRow:
        row = self._instructions.get(instruction_id)
        if row is None:
            raise SettlementError(f"unknown instruction {instruction_id}")
        checked = _checked_changes(_INSTRUCTION_MUTABLE, changes)
        status = checked.get("status")
        if status is not None and status not in INSTRUCTION_STATES:
            raise SettlementError(f"unknown instruction status {status!r}")
        updated = dataclasses.replace(row, **checked)  # type: ignore[arg-type]
        self._instructions[instruction_id] = updated
        return updated

    def list_instructions(
        self,
        batch_id: str | None = None,
        status: str | None = None,
        payment_intent_id: str | None = None,
        unassigned_only: bool = False,
    ) -> list[SettlementInstructionRow]:
        out = []
        for instruction_id in self._order:
            row = self._instructions[instruction_id]
            if batch_id is not None and row.batch_id != batch_id:
                continue
            if status is not None and row.status != status:
                continue
            if payment_intent_id is not None and row.payment_intent_id != payment_intent_id:
                continue
            if unassigned_only and row.batch_id is not None:
                continue
            out.append(row)
        return out

    def find_instruction_by_rail_txid(
        self, rail_transaction_id: str, *, direction: str | None = None
    ) -> SettlementInstructionRow | None:
        for row in self._instructions.values():
            if row.rail_transaction_id != rail_transaction_id:
                continue
            if row.status != "SETTLED":
                continue
            if direction is not None and row.direction != direction:
                continue
            return row
        return None

    def find_instructions_for_match(
        self,
        rail: str,
        amount_minor: int,
        date_from: date,
        date_to: date,
        *,
        direction: str | None = None,
    ) -> list[SettlementInstructionRow]:
        """Level-2 fuzzy match (spec/04): amount + rail + cycle-date window,
        SETTLED instructions only.

        When direction is provided (spec/PT-G), only instructions with a
        matching direction are returned.
        """
        out = []
        for instruction_id in self._order:
            row = self._instructions[instruction_id]
            if row.rail != rail or row.status != "SETTLED":
                continue
            if row.net_payout_minor != amount_minor:
                continue
            if direction is not None and row.direction != direction:
                continue
            if row.batch_id is None:
                continue
            batch = self._batches.get(row.batch_id)
            if batch is None:
                continue
            if date_from <= batch.cycle_date <= date_to:
                out.append(row)
        return out

    # --- fee rules ---

    def insert_fee_rule(self, rule: FeeRule) -> None:
        if rule.rule_id in self._fee_rules:
            raise SettlementError(f"duplicate fee rule {rule.rule_id}")
        self._fee_rules[rule.rule_id] = rule

    def list_fee_rules(self) -> list[FeeRule]:
        return sorted(self._fee_rules.values(), key=lambda r: r.rule_id)

"""NettingStore protocol + deterministic in-memory implementation (spec/19).

Storage pattern per IMPLEMENTATION.md: repository Protocol, in-memory
implementation for the unit suite, Postgres implementation in
``netting/pg_store.py`` matching db/migrations/0102_netting.sql.

The in-memory store mirrors the 0102 constraints exactly: content-addressed
primary keys, at most one live ``PASSED`` run per window
(``uidx_nrun_one_live``), ``UNIQUE (netting_run_id, participant_id)`` on
obligations, the ``nobl_direction_sign`` CHECK (enforced at row construction),
the ``nrun_passed_complete`` CHECK, and the unique ``connector_ref`` on
instructions.
"""

from __future__ import annotations

import dataclasses
from typing import Protocol

from bdpay.ledger.netting.errors import NettingError
from bdpay.ledger.netting.types import (
    NETTING_RUN_STATES,
    PSO_INSTRUCTION_TERMINAL_STATES,
    NettingObligationRow,
    NettingRunRow,
    PsoNettingInstructionRow,
)

__all__ = ["InMemoryNettingStore", "NettingStore"]

_RUN_MUTABLE = frozenset(
    {
        "status",
        "result_hash",
        "result_pointer",
        "total_gross_minor",
        "total_net_pay_minor",
        "failure_detail",
        "computed_at",
    }
)

_INSTRUCTION_MUTABLE = frozenset(
    {
        "status",
        "rail_transaction_id",
        "submitted_at",
        "settled_at",
        "failed_at",
        "cancelled_at",
    }
)


class NettingStore(Protocol):
    """Persistence boundary for netting runs, obligations, and PSO legs."""

    # --- runs ---
    def insert_run(self, row: NettingRunRow) -> None: ...

    def get_run(self, netting_run_id: str) -> NettingRunRow | None: ...

    def update_run(self, netting_run_id: str, **changes: object) -> NettingRunRow: ...

    def list_runs(self, settlement_window_id: str) -> list[NettingRunRow]: ...

    def live_passed_run(self, settlement_window_id: str) -> NettingRunRow | None: ...

    def count_unwind_runs(self, settlement_window_id: str) -> int: ...

    # --- obligations ---
    def insert_obligation(self, row: NettingObligationRow) -> None: ...

    def list_obligations(self, netting_run_id: str) -> list[NettingObligationRow]: ...

    # --- PSO netting legs (settlement_instructions, D-19-4 shape) ---
    def insert_instruction(self, row: PsoNettingInstructionRow) -> None: ...

    def get_instruction(self, instruction_id: str) -> PsoNettingInstructionRow | None: ...

    def update_instruction(
        self, instruction_id: str, **changes: object
    ) -> PsoNettingInstructionRow: ...

    def list_instructions(self, netting_run_id: str) -> list[PsoNettingInstructionRow]: ...


class InMemoryNettingStore:
    """Deterministic in-memory store; semantics mirror migration 0102."""

    def __init__(self) -> None:
        self._runs: dict[str, NettingRunRow] = {}
        self._run_order: list[str] = []
        self._obligations: dict[str, NettingObligationRow] = {}
        self._instructions: dict[str, PsoNettingInstructionRow] = {}
        self._instruction_order: list[str] = []

    # --- runs ---

    def insert_run(self, row: NettingRunRow) -> None:
        if row.netting_run_id in self._runs:
            raise NettingError(f"duplicate netting_run_id {row.netting_run_id}")
        if row.status not in NETTING_RUN_STATES:
            raise NettingError(f"unknown netting run status {row.status!r}")
        self._check_passed_complete(row)
        if row.status == "PASSED":
            self._check_one_live(row.settlement_window_id, exclude=row.netting_run_id)
        self._runs[row.netting_run_id] = row
        self._run_order.append(row.netting_run_id)

    def get_run(self, netting_run_id: str) -> NettingRunRow | None:
        return self._runs.get(netting_run_id)

    def update_run(self, netting_run_id: str, **changes: object) -> NettingRunRow:
        row = self._runs.get(netting_run_id)
        if row is None:
            raise NettingError(f"unknown netting run {netting_run_id}")
        unknown = set(changes) - (_RUN_MUTABLE | {"status"})
        if unknown:
            raise NettingError(f"non-updatable column(s) {sorted(unknown)}")
        updated = dataclasses.replace(row, **changes)  # type: ignore[arg-type]
        if updated.status not in NETTING_RUN_STATES:
            raise NettingError(f"unknown netting run status {updated.status!r}")
        self._check_passed_complete(updated)
        if updated.status == "PASSED":
            self._check_one_live(updated.settlement_window_id, exclude=netting_run_id)
        self._runs[netting_run_id] = updated
        return updated

    def list_runs(self, settlement_window_id: str) -> list[NettingRunRow]:
        return [
            self._runs[rid]
            for rid in self._run_order
            if self._runs[rid].settlement_window_id == settlement_window_id
        ]

    def live_passed_run(self, settlement_window_id: str) -> NettingRunRow | None:
        for rid in self._run_order:
            row = self._runs[rid]
            if row.settlement_window_id == settlement_window_id and row.status == "PASSED":
                return row
        return None

    def count_unwind_runs(self, settlement_window_id: str) -> int:
        """Number of unwind re-runs recorded for the window (D-19-2 rounds)."""
        return sum(
            1
            for rid in self._run_order
            if self._runs[rid].settlement_window_id == settlement_window_id
            and self._runs[rid].excluded_participant_id is not None
        )

    def _check_one_live(self, settlement_window_id: str, *, exclude: str) -> None:
        for rid, row in self._runs.items():
            if (
                rid != exclude
                and row.settlement_window_id == settlement_window_id
                and row.status == "PASSED"
            ):
                raise NettingError(
                    f"uidx_nrun_one_live violated: window {settlement_window_id} "
                    f"already has live PASSED run {rid}"
                )

    @staticmethod
    def _check_passed_complete(row: NettingRunRow) -> None:
        if row.status == "PASSED" and (
            row.result_hash is None
            or row.result_pointer is None
            or row.total_net_pay_minor is None
        ):
            raise NettingError(
                "nrun_passed_complete violated: PASSED runs require result_hash, "
                "result_pointer, and total_net_pay_minor"
            )

    # --- obligations ---

    def insert_obligation(self, row: NettingObligationRow) -> None:
        if row.obligation_id in self._obligations:
            raise NettingError(f"duplicate obligation_id {row.obligation_id}")
        if row.netting_run_id not in self._runs:
            raise NettingError(f"FK violation: netting run {row.netting_run_id} absent")
        for existing in self._obligations.values():
            if (
                existing.netting_run_id == row.netting_run_id
                and existing.participant_id == row.participant_id
            ):
                raise NettingError(
                    f"one obligation per (run, participant): {row.participant_id}"
                )
        self._obligations[row.obligation_id] = row

    def list_obligations(self, netting_run_id: str) -> list[NettingObligationRow]:
        rows = [
            row for row in self._obligations.values() if row.netting_run_id == netting_run_id
        ]
        return sorted(rows, key=lambda r: r.participant_id)

    # --- instructions ---

    def insert_instruction(self, row: PsoNettingInstructionRow) -> None:
        if row.instruction_id in self._instructions:
            raise NettingError(f"duplicate instruction_id {row.instruction_id}")
        if any(
            existing.connector_ref == row.connector_ref
            for existing in self._instructions.values()
        ):
            raise NettingError(f"duplicate connector_ref {row.connector_ref}")
        if row.fee_minor != 0:
            raise NettingError("sinst_kind_shape violated: PSO_NETTING rows carry fee_minor=0")
        if row.mcc_category != "OTHER":
            raise NettingError("sinst_kind_shape violated: PSO_NETTING rows carry mcc 'OTHER'")
        self._instructions[row.instruction_id] = row
        self._instruction_order.append(row.instruction_id)

    def get_instruction(self, instruction_id: str) -> PsoNettingInstructionRow | None:
        return self._instructions.get(instruction_id)

    def update_instruction(
        self, instruction_id: str, **changes: object
    ) -> PsoNettingInstructionRow:
        row = self._instructions.get(instruction_id)
        if row is None:
            raise NettingError(f"unknown instruction {instruction_id}")
        unknown = set(changes) - _INSTRUCTION_MUTABLE
        if unknown:
            raise NettingError(f"non-updatable column(s) {sorted(unknown)}")
        if row.status in PSO_INSTRUCTION_TERMINAL_STATES and "status" in changes:
            raise NettingError(
                f"instruction {instruction_id} is terminal ({row.status}); refusing update"
            )
        updated = dataclasses.replace(row, **changes)  # type: ignore[arg-type]
        self._instructions[instruction_id] = updated
        return updated

    def list_instructions(self, netting_run_id: str) -> list[PsoNettingInstructionRow]:
        out = [
            self._instructions[iid]
            for iid in self._instruction_order
            if self._instructions[iid].netting_run_id == netting_run_id
        ]
        return sorted(out, key=lambda r: r.participant_id)

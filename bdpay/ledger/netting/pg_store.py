"""Postgres NettingStore matching db/migrations/0102_netting.sql.

Netting legs live in ``ledger.settlement_instructions`` with the D-19-4
``instruction_kind='PSO_NETTING'`` shape — one table, one recon surface
(spec/04 Level-1 matching keys unchanged). Runs/obligations are append-only;
status advances only through the engine's FSM guards.
"""

from __future__ import annotations

import json
from typing import Any

import psycopg

from bdpay.ledger.netting.errors import NettingError
from bdpay.ledger.netting.types import (
    NettingObligationRow,
    NettingRunRow,
    PsoNettingInstructionRow,
)

__all__ = ["PostgresNettingStore"]

_RUN_COLUMNS = (
    "netting_run_id, settlement_window_id, status, participant_count, inputs_hash, "
    "result_hash, result_pointer, total_gross_minor, total_net_pay_minor, "
    "excluded_participant_id, supersedes_netting_run_id, failure_detail, "
    "computed_at, created_at, schema_version"
)

_OBLIGATION_COLUMNS = (
    "obligation_id, netting_run_id, participant_id, net_position_minor, direction, "
    "obligation_minor, settlement_instruction_id, schema_version"
)

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
    {"status", "rail_transaction_id", "submitted_at", "settled_at", "failed_at", "cancelled_at"}
)

#: ledger.settlement_instructions columns read back into the PSO row shape.
_INSTRUCTION_SELECT = (
    "instruction_id, netting_run_id, participant_id, direction, amount_minor, "
    "currency, rail, beneficiary_account_ref, high_value, earliest_release_at, "
    "latest_release_at, status, connector_ref, rail_transaction_id, created_at, "
    "submitted_at, settled_at, failed_at, produced_by, instruction_kind, "
    "fee_minor, mcc_category, schema_version"
)


def _run_row(raw: tuple[Any, ...]) -> NettingRunRow:
    values = list(raw)
    if isinstance(values[11], str):
        values[11] = json.loads(values[11])
    return NettingRunRow(*values)


def _instruction_row(raw: tuple[Any, ...]) -> PsoNettingInstructionRow:
    (
        instruction_id,
        netting_run_id,
        participant_id,
        direction,
        amount_minor,
        currency,
        rail,
        beneficiary_account_ref,
        high_value,
        earliest_release_at,
        latest_release_at,
        status,
        connector_ref,
        rail_transaction_id,
        created_at,
        submitted_at,
        settled_at,
        failed_at,
        produced_by,
        instruction_kind,
        fee_minor,
        mcc_category,
        schema_version,
    ) = raw
    return PsoNettingInstructionRow(
        instruction_id=instruction_id,
        netting_run_id=netting_run_id,
        participant_id=participant_id,
        direction=direction,
        amount_minor=amount_minor,
        currency=currency.strip() if isinstance(currency, str) else currency,
        rail=rail,
        counterparty_account_token=beneficiary_account_ref,
        high_value=high_value,
        earliest_release_at=earliest_release_at,
        latest_release_at=latest_release_at,
        status=status,
        connector_ref=connector_ref,
        rail_transaction_id=rail_transaction_id,
        created_at=created_at,
        submitted_at=submitted_at,
        settled_at=settled_at,
        failed_at=failed_at,
        cancelled_at=None,  # spec/04 DDL carries no cancelled_at column
        produced_by=produced_by,
        instruction_kind=instruction_kind,
        fee_minor=fee_minor,
        mcc_category=mcc_category,
        schema_version=schema_version,
    )


class PostgresNettingStore:
    """Sync psycopg3 store over an autocommit connection (ledger schema)."""

    def __init__(self, conn: psycopg.Connection[tuple[Any, ...]]) -> None:
        if not conn.autocommit:
            raise ValueError("PostgresNettingStore requires an autocommit connection")
        self._conn = conn

    @classmethod
    def connect(cls, dsn: str, *, role: str | None = None) -> PostgresNettingStore:
        conn = psycopg.connect(dsn, autocommit=True, options="-c TimeZone=UTC")
        if role is not None:
            conn.execute(psycopg.sql.SQL("SET ROLE {}").format(psycopg.sql.Identifier(role)))
        return cls(conn)

    def close(self) -> None:
        self._conn.close()

    # --- runs ---

    def insert_run(self, row: NettingRunRow) -> None:
        self._conn.execute(
            f"INSERT INTO ledger.netting_runs ({_RUN_COLUMNS})"
            f" VALUES ({', '.join(['%s'] * 15)})",
            (
                row.netting_run_id,
                row.settlement_window_id,
                row.status,
                row.participant_count,
                row.inputs_hash,
                row.result_hash,
                row.result_pointer,
                row.total_gross_minor,
                row.total_net_pay_minor,
                row.excluded_participant_id,
                row.supersedes_netting_run_id,
                json.dumps(row.failure_detail) if row.failure_detail is not None else None,
                row.computed_at,
                row.created_at,
                row.schema_version,
            ),
        )

    def get_run(self, netting_run_id: str) -> NettingRunRow | None:
        cur = self._conn.execute(
            f"SELECT {_RUN_COLUMNS} FROM ledger.netting_runs WHERE netting_run_id = %s",
            (netting_run_id,),
        )
        raw = cur.fetchone()
        return _run_row(raw) if raw else None

    def update_run(self, netting_run_id: str, **changes: object) -> NettingRunRow:
        unknown = set(changes) - _RUN_MUTABLE
        if unknown:
            raise NettingError(f"non-updatable column(s) {sorted(unknown)}")
        if changes:
            columns = sorted(changes)
            sets = ", ".join(f"{column} = %s" for column in columns)
            params: list[Any] = [
                json.dumps(changes[column])
                if column == "failure_detail" and changes[column] is not None
                else changes[column]
                for column in columns
            ]
            params.append(netting_run_id)
            cur = self._conn.execute(
                f"UPDATE ledger.netting_runs SET {sets} WHERE netting_run_id = %s",  # noqa: S608
                params,
            )
            if cur.rowcount != 1:
                raise NettingError(f"unknown netting run {netting_run_id}")
        updated = self.get_run(netting_run_id)
        assert updated is not None
        return updated

    def list_runs(self, settlement_window_id: str) -> list[NettingRunRow]:
        cur = self._conn.execute(
            f"SELECT {_RUN_COLUMNS} FROM ledger.netting_runs"
            " WHERE settlement_window_id = %s ORDER BY created_at, netting_run_id",
            (settlement_window_id,),
        )
        return [_run_row(raw) for raw in cur.fetchall()]

    def live_passed_run(self, settlement_window_id: str) -> NettingRunRow | None:
        cur = self._conn.execute(
            f"SELECT {_RUN_COLUMNS} FROM ledger.netting_runs"
            " WHERE settlement_window_id = %s AND status = 'PASSED' LIMIT 1",
            (settlement_window_id,),
        )
        raw = cur.fetchone()
        return _run_row(raw) if raw else None

    def count_unwind_runs(self, settlement_window_id: str) -> int:
        cur = self._conn.execute(
            "SELECT COUNT(*) FROM ledger.netting_runs"
            " WHERE settlement_window_id = %s AND excluded_participant_id IS NOT NULL",
            (settlement_window_id,),
        )
        raw = cur.fetchone()
        return int(raw[0]) if raw else 0

    # --- obligations ---

    def insert_obligation(self, row: NettingObligationRow) -> None:
        self._conn.execute(
            f"INSERT INTO ledger.netting_obligations ({_OBLIGATION_COLUMNS})"
            f" VALUES ({', '.join(['%s'] * 8)})",
            (
                row.obligation_id,
                row.netting_run_id,
                row.participant_id,
                row.net_position_minor,
                row.direction,
                row.obligation_minor,
                row.settlement_instruction_id,
                row.schema_version,
            ),
        )

    def list_obligations(self, netting_run_id: str) -> list[NettingObligationRow]:
        cur = self._conn.execute(
            f"SELECT {_OBLIGATION_COLUMNS} FROM ledger.netting_obligations"
            " WHERE netting_run_id = %s ORDER BY participant_id ASC",
            (netting_run_id,),
        )
        return [NettingObligationRow(*raw) for raw in cur.fetchall()]

    # --- PSO netting legs (ledger.settlement_instructions) ---

    def insert_instruction(self, row: PsoNettingInstructionRow) -> None:
        self._conn.execute(
            "INSERT INTO ledger.settlement_instructions ("
            " instruction_id, batch_id, payment_intent_id, merchant_id, direction,"
            " amount_minor, fee_minor, net_payout_minor, fee_rule_id, currency, rail,"
            " beneficiary_account_ref, mcc_category, high_value, earliest_release_at,"
            " latest_release_at, delivery_hold_cleared, aml_hold, status,"
            " rail_transaction_id, return_reason_code, connector_ref, created_at,"
            " submitted_at, settled_at, returned_at, failed_at, produced_by,"
            " schema_version, instruction_kind, participant_id, netting_run_id"
            f") VALUES ({', '.join(['%s'] * 32)})",
            (
                row.instruction_id,
                None,  # batch_id: netting legs dispatch per-window, not per-batch
                None,  # payment_intent_id (D-19-4 shape: NULL)
                None,  # merchant_id (D-19-4 shape: NULL)
                row.direction,
                row.amount_minor,
                row.fee_minor,
                row.net_payout_minor,
                None,  # fee_rule_id: no fee applies to a net obligation
                row.currency,
                row.rail,
                row.counterparty_account_token,
                row.mcc_category,
                row.high_value,
                row.earliest_release_at,
                row.latest_release_at,
                True,  # delivery_hold_cleared: release holds are merchant-payout semantics
                False,  # aml_hold
                row.status,
                row.rail_transaction_id,
                None,  # return_reason_code
                row.connector_ref,
                row.created_at,
                row.submitted_at,
                row.settled_at,
                None,  # returned_at
                row.failed_at,
                row.produced_by,
                row.schema_version,
                row.instruction_kind,
                row.participant_id,
                row.netting_run_id,
            ),
        )

    def get_instruction(self, instruction_id: str) -> PsoNettingInstructionRow | None:
        cur = self._conn.execute(
            f"SELECT {_INSTRUCTION_SELECT} FROM ledger.settlement_instructions"
            " WHERE instruction_id = %s AND instruction_kind = 'PSO_NETTING'",
            (instruction_id,),
        )
        raw = cur.fetchone()
        return _instruction_row(raw) if raw else None

    def update_instruction(
        self, instruction_id: str, **changes: object
    ) -> PsoNettingInstructionRow:
        unknown = set(changes) - _INSTRUCTION_MUTABLE
        if unknown:
            raise NettingError(f"non-updatable column(s) {sorted(unknown)}")
        db_changes = {k: v for k, v in changes.items() if k != "cancelled_at"}
        if db_changes:
            columns = sorted(db_changes)
            sets = ", ".join(f"{column} = %s" for column in columns)
            params: list[Any] = [db_changes[column] for column in columns]
            params.append(instruction_id)
            cur = self._conn.execute(
                "UPDATE ledger.settlement_instructions SET "  # noqa: S608
                f"{sets} WHERE instruction_id = %s AND instruction_kind = 'PSO_NETTING'",
                params,
            )
            if cur.rowcount != 1:
                raise NettingError(f"unknown PSO netting instruction {instruction_id}")
        updated = self.get_instruction(instruction_id)
        assert updated is not None
        return updated

    def list_instructions(self, netting_run_id: str) -> list[PsoNettingInstructionRow]:
        cur = self._conn.execute(
            f"SELECT {_INSTRUCTION_SELECT} FROM ledger.settlement_instructions"
            " WHERE netting_run_id = %s AND instruction_kind = 'PSO_NETTING'"
            " ORDER BY participant_id ASC",
            (netting_run_id,),
        )
        return [_instruction_row(raw) for raw in cur.fetchall()]

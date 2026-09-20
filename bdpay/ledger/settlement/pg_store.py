"""Postgres SettlementStore (psycopg3, sync) — matches 0011_settlement.sql.

Same protocol semantics as InMemorySettlementStore. Updates are whitelisted
column sets (the FSM tables are mutable state tables; the money trail lives
in the append-only ledger, not here).
"""

from __future__ import annotations

from datetime import date
from typing import Any

import psycopg

from bdpay.ledger.errors import SettlementError
from bdpay.ledger.settlement.fees import FeeRule
from bdpay.ledger.settlement.types import SettlementBatchRow, SettlementInstructionRow

_BATCH_COLUMNS = (
    "batch_id, rail, cycle_date, session, status, instruction_count, total_credit_minor, "
    "total_debit_minor, currency, retry_count, two_eyes_required, approval_request_id, "
    "held_at, held_tcsa_snapshot_id, sponsor_bank_ref, recon_file_pointer, recon_file_hash, "
    "connector_ref, created_at, cutoff_at, dispatched_at, confirmed_at, failed_at, "
    "cancelled_at, fail_reason_code, produced_by, schema_version"
)

_INSTRUCTION_COLUMNS = (
    "instruction_id, batch_id, payment_intent_id, merchant_id, direction, amount_minor, "
    "fee_minor, net_payout_minor, fee_rule_id, currency, rail, beneficiary_account_ref, "
    "mcc_category, high_value, earliest_release_at, latest_release_at, delivery_hold_cleared, "
    "aml_hold, status, rail_transaction_id, return_reason_code, connector_ref, created_at, "
    "submitted_at, settled_at, returned_at, failed_at, produced_by, schema_version"
)

_FEE_RULE_COLUMNS = (
    "rule_id, rule_name, merchant_id, method, mcc, fee_type, rate_bps, flat_amount_minor, "
    "regulated, valid_from, valid_to, created_by, approved_by, created_at, schema_version"
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


def _batch_from(row: tuple[Any, ...]) -> SettlementBatchRow:
    return SettlementBatchRow(*row)


def _instruction_from(row: tuple[Any, ...]) -> SettlementInstructionRow:
    return SettlementInstructionRow(*row)


def _fee_rule_from(row: tuple[Any, ...]) -> FeeRule:
    return FeeRule(*row)


class PostgresSettlementStore:
    """Sync psycopg3 store over an autocommit connection (ledger schema)."""

    def __init__(self, conn: psycopg.Connection[tuple[Any, ...]]) -> None:
        if not conn.autocommit:
            raise ValueError("PostgresSettlementStore requires an autocommit connection")
        self._conn = conn

    @classmethod
    def connect(cls, dsn: str, *, role: str | None = None) -> PostgresSettlementStore:
        conn = psycopg.connect(dsn, autocommit=True, options="-c TimeZone=UTC")
        if role is not None:
            conn.execute(psycopg.sql.SQL("SET ROLE {}").format(psycopg.sql.Identifier(role)))
        return cls(conn)

    def close(self) -> None:
        self._conn.close()

    # --- batches ---

    def insert_batch(self, row: SettlementBatchRow) -> None:
        self._conn.execute(
            f"INSERT INTO ledger.settlement_batches ({_BATCH_COLUMNS})"
            f" VALUES ({', '.join(['%s'] * 27)})",
            (
                row.batch_id,
                row.rail,
                row.cycle_date,
                row.session,
                row.status,
                row.instruction_count,
                row.total_credit_minor,
                row.total_debit_minor,
                row.currency,
                row.retry_count,
                row.two_eyes_required,
                row.approval_request_id,
                row.held_at,
                row.held_tcsa_snapshot_id,
                row.sponsor_bank_ref,
                row.recon_file_pointer,
                row.recon_file_hash,
                row.connector_ref,
                row.created_at,
                row.cutoff_at,
                row.dispatched_at,
                row.confirmed_at,
                row.failed_at,
                row.cancelled_at,
                row.fail_reason_code,
                row.produced_by,
                row.schema_version,
            ),
        )

    def get_batch(self, batch_id: str) -> SettlementBatchRow | None:
        cur = self._conn.execute(
            f"SELECT {_BATCH_COLUMNS} FROM ledger.settlement_batches WHERE batch_id = %s",
            (batch_id,),
        )
        row = cur.fetchone()
        return _batch_from(row) if row else None

    def update_batch(self, batch_id: str, **changes: object) -> SettlementBatchRow:
        self._update("settlement_batches", "batch_id", batch_id, _BATCH_MUTABLE, changes)
        updated = self.get_batch(batch_id)
        assert updated is not None
        return updated

    def persist_confirmation_evidence(
        self,
        batch_id: str,
        *,
        sponsor_bank_ref: str,
        rail_transaction_ids: dict[str, str],
    ) -> SettlementBatchRow:
        with self._conn.transaction():
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

            # Pre-validate membership/status so a bad payload leaves no
            # evidence behind.  A SUBMITTED row is updated; a SETTLED row means a
            # concurrent worker already finished the instruction and we converge
            # idempotently.
            for instruction_id in rail_transaction_ids:
                row = self.get_instruction(instruction_id)
                if row is None:
                    raise SettlementError(f"unknown instruction {instruction_id}")
                if row.batch_id != batch_id:
                    raise SettlementError(
                        f"instruction {instruction_id} is not a member of batch {batch_id}"
                    )
                if row.status not in ("SUBMITTED", "SETTLED"):
                    raise SettlementError(
                        f"instruction {instruction_id} is not a SUBMITTED member "
                        f"of batch {batch_id}"
                    )

            # Conditional updates make the evidence durable race-safely: if two
            # confirm() calls race, the later one sees the already-persisted value
            # and treats the write as idempotent instead of overwriting it.
            cur = self._conn.execute(
                "UPDATE ledger.settlement_batches"
                " SET sponsor_bank_ref = %s"
                " WHERE batch_id = %s"
                " AND (sponsor_bank_ref IS NULL OR sponsor_bank_ref = %s)",
                (sponsor_bank_ref, batch_id, sponsor_bank_ref),
            )
            if cur.rowcount != 1:
                raise SettlementError(
                    f"batch {batch_id}: sponsor_bank_ref conflict during update"
                )
            for instruction_id, txid in rail_transaction_ids.items():
                # A rowcount of 0 means the rail tx id is already durable (either
                # the same value or a different one); in both cases the existing
                # proof must be preserved and the supplied value must not be
                # treated as durable evidence.
                self._conn.execute(
                    "UPDATE ledger.settlement_instructions"
                    " SET rail_transaction_id = %s"
                    " WHERE instruction_id = %s AND batch_id = %s AND status = 'SUBMITTED'"
                    " AND (rail_transaction_id IS NULL OR rail_transaction_id = %s)",
                    (txid, instruction_id, batch_id, txid),
                )

            updated = self.get_batch(batch_id)
            assert updated is not None
            return updated

    def find_open_batch(
        self, rail: str, cycle_date: date, session: str | None
    ) -> SettlementBatchRow | None:
        cur = self._conn.execute(
            f"SELECT {_BATCH_COLUMNS} FROM ledger.settlement_batches"
            " WHERE rail = %s AND cycle_date = %s AND session IS NOT DISTINCT FROM %s"
            " AND status = 'OPEN'",
            (rail, cycle_date, session),
        )
        row = cur.fetchone()
        return _batch_from(row) if row else None

    def list_batches(self, status: str | None = None) -> list[SettlementBatchRow]:
        if status is None:
            cur = self._conn.execute(
                f"SELECT {_BATCH_COLUMNS} FROM ledger.settlement_batches"
                " ORDER BY created_at, batch_id"
            )
        else:
            cur = self._conn.execute(
                f"SELECT {_BATCH_COLUMNS} FROM ledger.settlement_batches WHERE status = %s"
                " ORDER BY created_at, batch_id",
                (status,),
            )
        return [_batch_from(row) for row in cur.fetchall()]

    # --- instructions ---

    def insert_instruction(self, row: SettlementInstructionRow) -> None:
        self._conn.execute(
            f"INSERT INTO ledger.settlement_instructions ({_INSTRUCTION_COLUMNS})"
            f" VALUES ({', '.join(['%s'] * 29)})",
            (
                row.instruction_id,
                row.batch_id,
                row.payment_intent_id,
                row.merchant_id,
                row.direction,
                row.amount_minor,
                row.fee_minor,
                row.net_payout_minor,
                row.fee_rule_id,
                row.currency,
                row.rail,
                row.beneficiary_account_ref,
                row.mcc_category,
                row.high_value,
                row.earliest_release_at,
                row.latest_release_at,
                row.delivery_hold_cleared,
                row.aml_hold,
                row.status,
                row.rail_transaction_id,
                row.return_reason_code,
                row.connector_ref,
                row.created_at,
                row.submitted_at,
                row.settled_at,
                row.returned_at,
                row.failed_at,
                row.produced_by,
                row.schema_version,
            ),
        )

    def get_instruction(self, instruction_id: str) -> SettlementInstructionRow | None:
        cur = self._conn.execute(
            f"SELECT {_INSTRUCTION_COLUMNS} FROM ledger.settlement_instructions"
            " WHERE instruction_id = %s",
            (instruction_id,),
        )
        row = cur.fetchone()
        return _instruction_from(row) if row else None

    def update_instruction(
        self, instruction_id: str, **changes: object
    ) -> SettlementInstructionRow:
        self._update(
            "settlement_instructions",
            "instruction_id",
            instruction_id,
            _INSTRUCTION_MUTABLE,
            changes,
        )
        updated = self.get_instruction(instruction_id)
        assert updated is not None
        return updated

    def list_instructions(
        self,
        batch_id: str | None = None,
        status: str | None = None,
        payment_intent_id: str | None = None,
        unassigned_only: bool = False,
    ) -> list[SettlementInstructionRow]:
        clauses: list[str] = []
        params: list[Any] = []
        if batch_id is not None:
            clauses.append("batch_id = %s")
            params.append(batch_id)
        if status is not None:
            clauses.append("status = %s")
            params.append(status)
        if payment_intent_id is not None:
            clauses.append("payment_intent_id = %s")
            params.append(payment_intent_id)
        if unassigned_only:
            clauses.append("batch_id IS NULL")
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        cur = self._conn.execute(
            f"SELECT {_INSTRUCTION_COLUMNS} FROM ledger.settlement_instructions{where}"
            " ORDER BY created_at, instruction_id",
            params,
        )
        return [_instruction_from(row) for row in cur.fetchall()]

    def find_instruction_by_rail_txid(
        self, rail_transaction_id: str, *, direction: str | None = None
    ) -> SettlementInstructionRow | None:
        # spec/PT-G: include direction predicate when provided so a CREDIT recon
        # line never matches a DEBIT instruction that shares the same rail_transaction_id
        # (possible on rails that reuse IDs across debit/credit legs).
        # Only SETTLED rows are eligible: rail_transaction_id may be persisted
        # earlier as durable confirmation evidence while the instruction is still
        # SUBMITTED, and reconciliation must not match before money has moved.
        if direction is not None:
            cur = self._conn.execute(
                f"SELECT {_INSTRUCTION_COLUMNS} FROM ledger.settlement_instructions"
                " WHERE rail_transaction_id = %s AND direction = %s AND status = 'SETTLED'",
                (rail_transaction_id, direction),
            )
        else:
            cur = self._conn.execute(
                f"SELECT {_INSTRUCTION_COLUMNS} FROM ledger.settlement_instructions"
                " WHERE rail_transaction_id = %s AND status = 'SETTLED'",
                (rail_transaction_id,),
            )
        row = cur.fetchone()
        return _instruction_from(row) if row else None

    def find_instructions_for_match(
        self,
        rail: str,
        amount_minor: int,
        date_from: date,
        date_to: date,
        *,
        direction: str | None = None,
    ) -> list[SettlementInstructionRow]:
        # spec/PT-G: direction predicate prevents a CREDIT recon line from matching
        # a DEBIT instruction (or vice-versa) at the fuzzy level.
        # Only SETTLED rows are eligible (see find_instruction_by_rail_txid).
        base = (
            f"SELECT {_INSTRUCTION_COLUMNS.replace('instruction_id', 'si.instruction_id')}"
            " FROM ledger.settlement_instructions si"
            " JOIN ledger.settlement_batches sb ON si.batch_id = sb.batch_id"
            " WHERE si.net_payout_minor = %s AND si.rail = %s"
            " AND sb.cycle_date BETWEEN %s AND %s AND si.status = 'SETTLED'"
        )
        params: list[object] = [amount_minor, rail, date_from, date_to]
        if direction is not None:
            base += " AND si.direction = %s"
            params.append(direction)
        base += " ORDER BY si.created_at, si.instruction_id"
        cur = self._conn.execute(base, params)
        return [_instruction_from(row) for row in cur.fetchall()]

    # --- fee rules ---

    def insert_fee_rule(self, rule: FeeRule) -> None:
        self._conn.execute(
            f"INSERT INTO ledger.fee_rules ({_FEE_RULE_COLUMNS})"
            f" VALUES ({', '.join(['%s'] * 15)})",
            (
                rule.rule_id,
                rule.rule_name,
                rule.merchant_id,
                rule.method,
                rule.mcc,
                rule.fee_type,
                rule.rate_bps,
                rule.flat_amount_minor,
                rule.regulated,
                rule.valid_from,
                rule.valid_to,
                rule.created_by,
                rule.approved_by,
                rule.created_at,
                rule.schema_version,
            ),
        )

    def list_fee_rules(self) -> list[FeeRule]:
        cur = self._conn.execute(
            f"SELECT {_FEE_RULE_COLUMNS} FROM ledger.fee_rules ORDER BY rule_id"
        )
        return [_fee_rule_from(row) for row in cur.fetchall()]

    # --- helpers ---

    def _update(
        self,
        table: str,
        key_column: str,
        key: str,
        allowed: frozenset[str],
        changes: dict[str, object],
    ) -> None:
        unknown = set(changes) - allowed
        if unknown:
            raise SettlementError(f"non-updatable column(s) {sorted(unknown)}")
        if not changes:
            return
        columns = sorted(changes)
        sets = ", ".join(f"{column} = %s" for column in columns)
        params: list[Any] = [changes[column] for column in columns]
        params.append(key)
        cur = self._conn.execute(
            f"UPDATE ledger.{table} SET {sets} WHERE {key_column} = %s",  # noqa: S608
            params,
        )
        if cur.rowcount != 1:
            raise SettlementError(f"unknown {table} key {key}")

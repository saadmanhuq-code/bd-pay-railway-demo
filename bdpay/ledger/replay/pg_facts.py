"""Postgres WindowFactsPort — the binding D-19-6 attribution queries.

Selection is ``journal_entries.metadata->>'settlement_window_id'`` (column
added additively by migration 0103 — errata E-S19R-02), ordered by
``entry_index`` — never by timestamp, never from the trigger-maintained
``position_accounts`` mirror (D-19-6).

Table seams owned by other spec/19 lanes (read-only here):

- ``ledger.netting_runs`` / ``ledger.settlement_instructions`` — PSO-2
  (netting) lane; the table names are constructor parameters so the wiring
  lane pins the final qualified names without code change.
- Settlement-account tokens — onboarding facts (spec/08 + spec/19 PSO-1).
  The token query is a constructor parameter for the same reason; the
  default reads the latest VERIFIED ``SETTLEMENT_ACCOUNT_DETAILS`` document
  row (spec/19 PSO-1 ``participant_documents``).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from bdpay.ledger.replay.errors import ReplayError
from bdpay.ledger.replay.facts import ObjectStorePort
from bdpay.ledger.replay.types import AttributedEntry, NettingRunView
from bdpay.ledger.types import ChainEntryRow, JournalEntryRow, PostingRow

__all__ = ["PostgresWindowFacts"]

_DEFAULT_TOKEN_SQL = (
    "SELECT metadata->>'settlement_account_token' FROM participant_documents"
    " WHERE participant_id = %s AND document_class = 'SETTLEMENT_ACCOUNT_DETAILS'"
    " AND review_status = 'VERIFIED' ORDER BY verified_at DESC LIMIT 1"
)

_JE_COLUMNS = (
    "entry_id, entry_index, reference_id, reference_type, entry_type, description,"
    " produced_by, produced_at, idempotency_key, schema_version"
)
_POSTING_COLUMNS = (
    "posting_id, entry_id, account_id, side, amount_minor, currency, produced_at, schema_version"
)
_CHAIN_COLUMNS = (
    "chain_domain, chain_index, entry_type, payload_hash, payload_pointer, prev_chain_hash,"
    " amount_minor_sum, chain_hash, is_checkpoint, checkpoint_sequence_in_domain,"
    " checkpoint_sig, checkpoint_public_key_b64, producer, produced_at, schema_version"
)


class PostgresWindowFacts:
    """WindowFactsPort over psycopg3 (ledger schema + spec/19 lane tables)."""

    def __init__(
        self,
        connection_factory: Callable[[], Any],
        *,
        object_store: ObjectStorePort,
        netting_runs_table: str = "ledger.netting_runs",
        instructions_table: str = "ledger.settlement_instructions",
        windows_table: str = "ledger.settlement_windows",
        settlement_account_token_sql: str = _DEFAULT_TOKEN_SQL,
    ) -> None:
        self._connect = connection_factory
        self._objects = object_store
        self._netting_runs = netting_runs_table
        self._instructions = instructions_table
        self._windows = windows_table
        self._token_sql = settlement_account_token_sql

    def _fetchall(self, sql: str, params: tuple) -> list[tuple[Any, ...]]:
        with self._connect() as conn:
            cur = conn.execute(sql, params)
            return cur.fetchall()

    def _fetchone(self, sql: str, params: tuple) -> tuple[Any, ...] | None:
        rows = self._fetchall(sql, params)
        return rows[0] if rows else None

    # -- WindowFactsPort -----------------------------------------------------

    def get_window(self, settlement_window_id: str) -> Mapping[str, object] | None:
        row = self._fetchone(
            f"SELECT window_id, dns_session, window_date FROM {self._windows}"  # noqa: S608
            " WHERE window_id = %s",
            (settlement_window_id,),
        )
        if row is None:
            return None
        return {
            "settlement_window_id": row[0],
            "dns_session": row[1],
            "window_date": row[2].isoformat(),
        }

    def latest_netting_run(self, settlement_window_id: str) -> NettingRunView | None:
        row = self._fetchone(
            "SELECT netting_run_id, settlement_window_id, status, inputs_hash, result_hash,"
            " result_pointer, participant_count, total_gross_minor, total_net_pay_minor,"
            " excluded_participant_id, supersedes_netting_run_id"
            f" FROM {self._netting_runs}"  # noqa: S608
            " WHERE settlement_window_id = %s AND status <> 'SUPERSEDED'"
            " ORDER BY created_at DESC, netting_run_id DESC LIMIT 1",
            (settlement_window_id,),
        )
        if row is None:
            return None
        return NettingRunView(
            netting_run_id=row[0],
            settlement_window_id=row[1],
            status=row[2],
            inputs_hash=row[3],
            result_hash=row[4],
            result_pointer=row[5],
            participant_count=row[6],
            total_gross_minor=row[7],
            total_net_pay_minor=row[8],
            excluded_participant_id=row[9],
            supersedes_netting_run_id=row[10],
        )

    def result_file(self, result_pointer: str) -> bytes | None:
        return self._objects.get(result_pointer)

    def _entries_where(self, where_sql: str, params: tuple) -> list[AttributedEntry]:
        entry_rows = self._fetchall(
            f"SELECT {_JE_COLUMNS}, COALESCE(metadata->>'settlement_window_id', '')"
            f" FROM ledger.journal_entries WHERE {where_sql}"  # noqa: S608
            " ORDER BY entry_index ASC",
            params,
        )
        out: list[AttributedEntry] = []
        for row in entry_rows:
            entry = JournalEntryRow(*row[:-1])
            postings = tuple(
                PostingRow(*p)
                for p in self._fetchall(
                    f"SELECT {_POSTING_COLUMNS} FROM ledger.postings"
                    " WHERE entry_id = %s ORDER BY posting_id ASC",
                    (entry.entry_id,),
                )
            )
            out.append(
                AttributedEntry(entry=entry, postings=postings, settlement_window_id=row[-1])
            )
        return out

    def window_entries(self, settlement_window_id: str) -> list[AttributedEntry]:
        # D-19-6 verbatim: selection by the metadata attribution, entry_index order.
        return self._entries_where(
            "metadata->>'settlement_window_id' = %s", (settlement_window_id,)
        )

    def participant_for_account(self, account_id: str) -> str | None:
        row = self._fetchone(
            "SELECT owner_id FROM ledger.accounts"
            " WHERE account_id = %s AND account_subtype = 'PARTICIPANT_POSITION'",
            (account_id,),
        )
        return row[0] if row else None

    def position_entries_in_range(
        self, from_entry_index: int, to_entry_index: int
    ) -> list[tuple[str, str | None]]:
        rows = self._fetchall(
            "SELECT DISTINCT je.entry_id, je.entry_index,"
            " NULLIF(je.metadata->>'settlement_window_id', '')"
            " FROM ledger.journal_entries je"
            " JOIN ledger.postings p ON p.entry_id = je.entry_id"
            " JOIN ledger.accounts a ON a.account_id = p.account_id"
            " WHERE a.account_subtype = 'PARTICIPANT_POSITION'"
            "   AND je.entry_index BETWEEN %s AND %s"
            " ORDER BY je.entry_index ASC",
            (from_entry_index, to_entry_index),
        )
        return [(row[0], row[2]) for row in rows]

    def entries_for_reference(self, reference_id: str) -> list[AttributedEntry]:
        return self._entries_where("reference_id = %s", (reference_id,))

    def netting_instructions(self, netting_run_id: str) -> list[Mapping[str, object]]:
        rows = self._fetchall(
            "SELECT instruction_id, participant_id, netting_run_id, instruction_kind,"
            " direction, amount_minor, net_payout_minor, fee_minor, rail,"
            " beneficiary_account_ref, status, connector_ref, rail_transaction_id"
            f" FROM {self._instructions}"  # noqa: S608
            " WHERE netting_run_id = %s AND instruction_kind = 'PSO_NETTING'"
            " ORDER BY participant_id ASC",
            (netting_run_id,),
        )
        return [
            {
                "instruction_id": row[0],
                "participant_id": row[1],
                "netting_run_id": row[2],
                "instruction_kind": row[3],
                "direction": row[4],
                "amount_minor": row[5],
                "net_payout_minor": row[6],
                "fee_minor": row[7],
                "rail": row[8],
                "beneficiary_account_ref": row[9],
                "status": row[10],
                "connector_ref": row[11],
                "rail_transaction_id": row[12],
            }
            for row in rows
        ]

    def settlement_account_token(self, participant_id: str) -> str:
        row = self._fetchone(self._token_sql, (participant_id,))
        if row is None or not row[0]:
            raise ReplayError(
                f"no settlement-account token recorded for {participant_id} (fail closed)"
            )
        return str(row[0])

    def chain_index_for_entry(self, entry_id: str) -> int | None:
        # The payload_pointer embeds the entry id (deep-verify parse contract).
        row = self._fetchone(
            "SELECT c.chain_index FROM ledger.ledger_chain c"
            " WHERE c.chain_domain = 'MONEY'"
            "   AND c.payload_pointer LIKE %s",
            (f"%/{entry_id}.json.enc",),
        )
        return row[0] if row else None

    def chain_entry_at(self, chain_index: int) -> ChainEntryRow | None:
        row = self._fetchone(
            f"SELECT {_CHAIN_COLUMNS} FROM ledger.ledger_chain"
            " WHERE chain_domain = 'MONEY' AND chain_index = %s",
            (chain_index,),
        )
        return ChainEntryRow(*row) if row else None

    def window_audit_history(self, settlement_window_id: str) -> list[Mapping[str, object]]:
        from bdpay.ledger.hash_chain import to_canonical_ts

        rows = self._fetchall(
            "SELECT event_type, from_state, to_state, occurred_at FROM ledger.audit_events"
            " WHERE subject_type = 'SettlementWindow' AND subject_id = %s"
            " ORDER BY entry_index ASC",
            (settlement_window_id,),
        )
        return [
            {
                "event_type": row[0],
                "from_state": row[1],
                "to_state": row[2],
                "occurred_at": to_canonical_ts(row[3]),
            }
            for row in rows
        ]

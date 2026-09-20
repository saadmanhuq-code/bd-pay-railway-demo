"""Ledger-facts read surface for replay (spec/03 modes 1-3 + spec/19 WINDOW_REPLAY).

Everything the replay service consumes comes through these ports — ledger
facts only. The window-facts port is the D-19-6 attribution seam:

- Postgres implementation (``bdpay.ledger.replay.pg_facts``): the binding
  ``journal_entries.metadata->>'settlement_window_id'`` query verbatim
  (column added additively by migration 0103 — errata E-S19R-02).
- In-memory implementation (:class:`LedgerWindowFacts`): reads journal
  entries / postings / chain rows / audit events from the REAL
  :class:`bdpay.ledger.store.LedgerStore` (the same rows the chain seals) and
  carries the writer-supplied attribution map that mirrors the metadata
  column until the core row type gains the field at fold-in.

Enumeration is anchored on the MONEY hash chain: chain rows are walked in
``chain_index`` order and resolved to journal entries through their sealed
``payload_pointer`` — so the read order is the chain's order, never a
timestamp's.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Protocol, runtime_checkable

from bdpay.ledger.hash_chain import to_canonical_ts
from bdpay.ledger.payloads import entry_id_from_pointer
from bdpay.ledger.replay.errors import ReplayError
from bdpay.ledger.replay.types import AttributedEntry, NettingRunView
from bdpay.ledger.store import LedgerStore
from bdpay.ledger.types import ChainEntryRow, JournalEntryRow, PostingRow

__all__ = [
    "InMemoryObjectStore",
    "LedgerWindowFacts",
    "ObjectStorePort",
    "WindowFactsPort",
    "enumerate_money_entries",
]

_MONEY = "MONEY"


@runtime_checkable
class ObjectStorePort(Protocol):
    """Where netting result files and sealed bundle ZIPs live (spec/16 shape)."""

    def put(self, key: str, data: bytes) -> None: ...

    def get(self, key: str) -> bytes | None: ...

    def exists(self, key: str) -> bool: ...


class InMemoryObjectStore:
    """Deterministic in-memory object store (spec/16 dossier pattern)."""

    def __init__(self) -> None:
        self._objects: dict[str, bytes] = {}

    def put(self, key: str, data: bytes) -> None:
        self._objects[key] = bytes(data)

    def get(self, key: str) -> bytes | None:
        return self._objects.get(key)

    def exists(self, key: str) -> bool:
        return key in self._objects


def enumerate_money_entries(
    store: LedgerStore,
) -> Iterator[tuple[ChainEntryRow, JournalEntryRow, tuple[PostingRow, ...]]]:
    """Walk the MONEY chain in ``chain_index`` order, resolving each sealed
    row to its journal entry + postings. Genesis is skipped. A chain row whose
    entry is missing fails closed (the deep verify would catch it too)."""
    for row in store.iter_chain(_MONEY):
        if row.chain_index == 0:
            continue
        entry_id = entry_id_from_pointer(row.payload_pointer)
        if entry_id is None:
            raise ReplayError(
                f"MONEY chain row {row.chain_index} has an unparseable payload_pointer"
            )
        entry = store.get_journal_entry(entry_id)
        if entry is None:
            raise ReplayError(
                f"journal entry {entry_id} referenced by chain row {row.chain_index} is missing"
            )
        postings = tuple(store.get_postings_for_entry(entry_id))
        yield row, entry, postings


@runtime_checkable
class WindowFactsPort(Protocol):
    """The WINDOW_REPLAY read surface — ledger facts only (D-19-6 selection)."""

    def get_window(self, settlement_window_id: str) -> Mapping[str, object] | None:
        """``{"settlement_window_id", "dns_session", "window_date"}`` or None."""
        ...

    def latest_netting_run(self, settlement_window_id: str) -> NettingRunView | None:
        """The latest non-superseded run for the window (normally exactly one
        PASSED row — ``uidx_nrun_one_live``)."""
        ...

    def result_file(self, result_pointer: str) -> bytes | None: ...

    def window_entries(self, settlement_window_id: str) -> list[AttributedEntry]:
        """All journal entries attributed to the window (D-19-6), with their
        postings, ordered by ``entry_index`` ASC — never by timestamp."""
        ...

    def participant_for_account(self, account_id: str) -> str | None:
        """The owning participant_id if the account is a
        ``PARTICIPANT_POSITION`` account, else None."""
        ...

    def position_entries_in_range(
        self, from_entry_index: int, to_entry_index: int
    ) -> list[tuple[str, str | None]]:
        """``(entry_id, attributed_window_or_None)`` for every entry in the
        inclusive ``entry_index`` range whose postings touch a
        ``PARTICIPANT_POSITION`` account — the D-19-6 completeness check."""
        ...

    def entries_for_reference(self, reference_id: str) -> list[AttributedEntry]:
        """Entries by journal ``reference_id`` (dispute-bundle member 3);
        attribution may be empty for non-window entries."""
        ...

    def netting_instructions(self, netting_run_id: str) -> list[Mapping[str, object]]:
        """The run's ``PSO_NETTING`` settlement-instruction rows (D-19-4
        shape; ``raw_response_hash`` pointers only, never raw responses)."""
        ...

    def settlement_account_token(self, participant_id: str) -> str:
        """The participant's vault-tokenized settlement-account reference
        (raw account numbers never appear — spec/14 posture)."""
        ...

    def chain_index_for_entry(self, entry_id: str) -> int | None: ...

    def chain_entry_at(self, chain_index: int) -> ChainEntryRow | None: ...

    def window_audit_history(self, settlement_window_id: str) -> list[Mapping[str, object]]:
        """The window's audit-event state history (bundle member 2), ordered
        by audit ``entry_index``."""
        ...


class LedgerWindowFacts:
    """In-memory :class:`WindowFactsPort` over the real ledger store.

    Journal entries, postings, chain rows, accounts, and audit events are
    READ from the :class:`LedgerStore` (the chain-sealed facts). The
    registries the other spec/19 lanes own (windows, netting runs,
    instructions, settlement-account tokens) plus the D-19-6 attribution map
    are supplied by the writer — exactly what the Postgres implementation
    reads from those lanes' tables.
    """

    def __init__(self, store: LedgerStore, *, object_store: ObjectStorePort) -> None:
        self._store = store
        self._objects = object_store
        self._attribution: dict[str, str] = {}  # entry_id -> settlement_window_id
        self._windows: dict[str, dict[str, object]] = {}
        self._runs: dict[str, list[NettingRunView]] = {}
        self._instructions: dict[str, list[dict[str, object]]] = {}
        self._tokens: dict[str, str] = {}

    # -- writer-side registration (mirrors the owning lanes' tables) --------

    def attribute_entry(self, entry_id: str, settlement_window_id: str) -> None:
        """Record the D-19-6 attribution for an entry (written by the posting
        path in the same transaction; mirrored here until fold-in)."""
        existing = self._attribution.get(entry_id)
        if existing is not None and existing != settlement_window_id:
            raise ReplayError(
                f"entry {entry_id} is already attributed to {existing}; "
                "attribution is write-once (D-19-6)"
            )
        self._attribution[entry_id] = settlement_window_id

    def register_window(
        self, settlement_window_id: str, *, dns_session: str, window_date: str
    ) -> None:
        self._windows[settlement_window_id] = {
            "settlement_window_id": settlement_window_id,
            "dns_session": dns_session,
            "window_date": window_date,
        }

    def register_netting_run(self, run: NettingRunView) -> None:
        self._runs.setdefault(run.settlement_window_id, []).append(run)

    def register_instruction(self, netting_run_id: str, row: Mapping[str, object]) -> None:
        self._instructions.setdefault(netting_run_id, []).append(dict(row))

    def register_settlement_account_token(self, participant_id: str, token: str) -> None:
        self._tokens[participant_id] = token

    def put_result_file(self, pointer: str, data: bytes) -> None:
        self._objects.put(pointer, data)

    # -- WindowFactsPort -----------------------------------------------------

    def get_window(self, settlement_window_id: str) -> Mapping[str, object] | None:
        row = self._windows.get(settlement_window_id)
        return dict(row) if row is not None else None

    def latest_netting_run(self, settlement_window_id: str) -> NettingRunView | None:
        runs = [
            run
            for run in self._runs.get(settlement_window_id, [])
            if run.status != "SUPERSEDED"
        ]
        return runs[-1] if runs else None

    def result_file(self, result_pointer: str) -> bytes | None:
        return self._objects.get(result_pointer)

    def window_entries(self, settlement_window_id: str) -> list[AttributedEntry]:
        out: list[AttributedEntry] = []
        for _row, entry, postings in enumerate_money_entries(self._store):
            if self._attribution.get(entry.entry_id) == settlement_window_id:
                out.append(
                    AttributedEntry(
                        entry=entry,
                        postings=postings,
                        settlement_window_id=settlement_window_id,
                    )
                )
        return out  # chain order == entry_index order

    def participant_for_account(self, account_id: str) -> str | None:
        account = self._store.get_account(account_id)
        if account is None or account.account_subtype != "PARTICIPANT_POSITION":
            return None
        return account.owner_id

    def position_entries_in_range(
        self, from_entry_index: int, to_entry_index: int
    ) -> list[tuple[str, str | None]]:
        out: list[tuple[str, str | None]] = []
        for _row, entry, postings in enumerate_money_entries(self._store):
            if not (from_entry_index <= entry.entry_index <= to_entry_index):
                continue
            if any(self.participant_for_account(p.account_id) is not None for p in postings):
                out.append((entry.entry_id, self._attribution.get(entry.entry_id)))
        return out

    def entries_for_reference(self, reference_id: str) -> list[AttributedEntry]:
        out: list[AttributedEntry] = []
        for _row, entry, postings in enumerate_money_entries(self._store):
            if entry.reference_id == reference_id:
                out.append(
                    AttributedEntry(
                        entry=entry,
                        postings=postings,
                        settlement_window_id=self._attribution.get(entry.entry_id, ""),
                    )
                )
        return out

    def netting_instructions(self, netting_run_id: str) -> list[Mapping[str, object]]:
        return [dict(row) for row in self._instructions.get(netting_run_id, [])]

    def settlement_account_token(self, participant_id: str) -> str:
        token = self._tokens.get(participant_id)
        if token is None:
            raise ReplayError(
                f"no settlement-account token registered for {participant_id} "
                "(onboarding fact missing; fail closed)"
            )
        return token

    def chain_index_for_entry(self, entry_id: str) -> int | None:
        for row, entry, _postings in enumerate_money_entries(self._store):
            if entry.entry_id == entry_id:
                return row.chain_index
        return None

    def chain_entry_at(self, chain_index: int) -> ChainEntryRow | None:
        return self._store.get_chain_entry(_MONEY, chain_index)

    def window_audit_history(self, settlement_window_id: str) -> list[Mapping[str, object]]:
        rows = self._store.list_audit_events(
            subject_type="SettlementWindow", subject_id=settlement_window_id
        )
        return [
            {
                "event_type": row.event_type,
                "from_state": row.from_state,
                "to_state": row.to_state,
                "occurred_at": to_canonical_ts(row.occurred_at),
            }
            for row in sorted(rows, key=lambda r: r.entry_index)
        ]

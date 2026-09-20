"""Per-window position derivation from LEDGER FACTS ONLY (spec/19 D-19-6).

The netting integrity cross-check and the WINDOW_REPLAY service re-derive
participant positions from ``journal_entries`` + ``postings`` selected by
window attribution, ordered by ``entry_index`` — never by timestamp, never
from the trigger-maintained ``position_accounts`` mirror.

Attribution (binding resolution, errata N19-1): the built spec/03 schema
carries no ``journal_entries.metadata`` column (E1-precedent DDL), so the
window attribution is encoded in existing ledger facts written by the posting
path in the same transaction:

    reference_type = 'SETTLEMENT'
    reference_id   = <settlement_window_id>
    entry_type    IN ('position_updated', 'payment_reversed')

``settlement_batch_close`` entries are the netting OUTPUT (sealed at compute
time) and are excluded from the derivation input by entry type.

``StoreLedgerFacts`` works over ANY ``LedgerStore`` (in-memory or Postgres)
using only the protocol read surface: it walks the MONEY hash chain (the
deterministic, gap-checked enumeration of every journal entry) and resolves
each entry through its payload pointer — the same resolution the E5 deep
verifier uses.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from bdpay.ledger.netting.errors import NettingError
from bdpay.ledger.netting.types import POSITION_FACT_ENTRY_TYPES, ParticipantPosition
from bdpay.ledger.payloads import entry_id_from_pointer
from bdpay.ledger.service import MONEY_DOMAIN
from bdpay.ledger.store import LedgerStore
from bdpay.ledger.types import ChainTip, JournalEntryRow, PostingRow

__all__ = ["LedgerFactsPort", "PositionEntryFact", "StoreLedgerFacts", "WindowDerivation"]


@dataclass(frozen=True, slots=True)
class PositionEntryFact:
    """One window-attributed journal entry with its position-account postings."""

    entry_id: str
    entry_index: int
    entry_type: str
    postings: tuple[PostingRow, ...]  # only the PARTICIPANT_POSITION legs
    participant_by_posting: tuple[str, ...]  # owner_id per posting, same order


@dataclass(frozen=True, slots=True)
class WindowDerivation:
    """The full step-1b read: derived positions + gross volume + the facts."""

    positions: tuple[ParticipantPosition, ...]  # participant_id ASC
    total_gross_minor: int  # sum of |position posting amounts|
    entries: tuple[PositionEntryFact, ...]  # ordered by entry_index
    chain_tip: ChainTip  # MONEY-domain tip in the same read


class LedgerFactsPort(Protocol):
    """Read-only derivation surface the netting engine and replay consume."""

    def derive_window(self, settlement_window_id: str) -> WindowDerivation: ...


class StoreLedgerFacts:
    """``LedgerFactsPort`` over the ``LedgerStore`` protocol read surface."""

    def __init__(self, store: LedgerStore) -> None:
        self._store = store

    def derive_window(self, settlement_window_id: str) -> WindowDerivation:
        """spec/19 algorithm step 1b — derivation from ledger facts only.

        Credit counts positive, debit negative (matches the position-account
        semantics); results are grouped by the position account's ``owner_id``
        (the participant) and returned in participant_id ASC order.
        """
        store = self._store
        max_index = store.max_chain_index(MONEY_DOMAIN)
        if max_index is None:
            raise NettingError(
                "MONEY chain is empty — no ledger facts exist to derive from"
            )
        tip_row = store.get_chain_entry(MONEY_DOMAIN, max_index)
        if tip_row is None:
            raise NettingError(f"MONEY chain tip row {max_index} unreadable (fail closed)")
        tip = ChainTip(chain_index=tip_row.chain_index, chain_hash=tip_row.chain_hash)

        facts: list[PositionEntryFact] = []
        totals: dict[str, int] = {}
        gross = 0
        for chain_row in store.iter_chain(MONEY_DOMAIN, 0, max_index):
            if chain_row.chain_index == 0:  # genesis carries no journal entry
                continue
            entry_id = entry_id_from_pointer(chain_row.payload_pointer)
            if entry_id is None:
                continue  # non-ledger pointer shape (not a journal entry row)
            entry = store.get_journal_entry(entry_id)
            if entry is None:
                raise NettingError(
                    f"chain row {chain_row.chain_index} references missing journal "
                    f"entry {entry_id} (fail closed — run verify_chain)"
                )
            if not self._is_window_fact(entry, settlement_window_id):
                continue
            postings = store.get_postings_for_entry(entry_id)
            position_legs: list[PostingRow] = []
            owners: list[str] = []
            for posting in sorted(postings, key=lambda p: p.posting_id):
                account = store.get_account(posting.account_id)
                if account is None:
                    raise NettingError(
                        f"posting {posting.posting_id} references missing account "
                        f"{posting.account_id} (fail closed)"
                    )
                if account.account_subtype != "PARTICIPANT_POSITION":
                    continue
                if not account.owner_id:
                    raise NettingError(
                        f"PARTICIPANT_POSITION account {account.account_id} has no "
                        "owner_id — cannot attribute the movement to a participant"
                    )
                position_legs.append(posting)
                owners.append(account.owner_id)
                signed = (
                    posting.amount_minor
                    if posting.side == "CREDIT"
                    else -posting.amount_minor
                )
                totals[account.owner_id] = totals.get(account.owner_id, 0) + signed
                gross += posting.amount_minor
            if position_legs:
                facts.append(
                    PositionEntryFact(
                        entry_id=entry.entry_id,
                        entry_index=entry.entry_index,
                        entry_type=entry.entry_type,
                        postings=tuple(position_legs),
                        participant_by_posting=tuple(owners),
                    )
                )

        facts.sort(key=lambda f: f.entry_index)
        positions = tuple(
            ParticipantPosition(participant_id=pid, net_position_minor=totals[pid])
            for pid in sorted(totals)
        )
        return WindowDerivation(
            positions=positions,
            total_gross_minor=gross,
            entries=tuple(facts),
            chain_tip=tip,
        )

    @staticmethod
    def _is_window_fact(entry: JournalEntryRow, settlement_window_id: str) -> bool:
        return (
            entry.reference_type == "SETTLEMENT"
            and entry.reference_id == settlement_window_id
            and entry.entry_type in POSITION_FACT_ENTRY_TYPES
        )

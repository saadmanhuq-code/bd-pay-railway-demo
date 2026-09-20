"""Window-position reads + the D-19-6 attributed posting path (spec/19 PSO-2).

Three pieces:

1. ``NettingPositionStore`` — the spec/05 ``PositionStore`` surface plus the
   one read netting needs (``list_window_positions``, the algorithm step-1a
   "maintained" side). Provided as ADDITIVE subclasses of the spec/05 stores
   so nothing in ``bdpay/ledger/position.py`` is redefined.

2. ``ParticipantDirectory`` — the engine's lookup for participant status
   (the suspended-participant guard) and the tokenized settlement-account
   reference used in instruction plans. The kernel's onboarding service is
   the production implementation; ``StaticParticipantDirectory`` is the
   deterministic in-memory one.

3. ``PositionPostingService`` — THE posting path of D-19-6: every journal
   entry that touches a ``PARTICIPANT_POSITION`` account is written with the
   window attribution (``reference_type='SETTLEMENT'``,
   ``reference_id=<window_id>``) in the same ledger transaction. It also
   applies the application-side maintained mirror (``PositionService``) when
   one is wired — on Postgres the migration 0102 triggers maintain the mirror
   instead, and the composition root passes ``maintained_mirror=None``.
"""

from __future__ import annotations

from typing import Protocol

from bdpay.ledger.netting.errors import NdcParticipantNotActiveError, NettingError
from bdpay.ledger.netting.facts import PositionEntryFact
from bdpay.ledger.netting.types import PRODUCER, participant_position_account_id
from bdpay.ledger.position import (
    InMemoryPositionStore,
    PositionAccountRow,
    PositionService,
    PostgresPositionStore,
    SettlementWindowRow,
)
from bdpay.ledger.service import LedgerService
from bdpay.ledger.types import JournalEntrySpec, PostingSpec
from bdpay.platform.ids import make_id

__all__ = [
    "InMemoryNettingPositionStore",
    "NettingPositionStore",
    "ParticipantDirectory",
    "PositionPostingService",
    "PostgresNettingPositionStore",
    "StaticParticipantDirectory",
]


class NettingPositionStore(Protocol):
    """spec/05 PositionStore surface + the netting window read (additive)."""

    # --- the netting addition ---
    def list_window_positions(self, settlement_window_id: str) -> list[PositionAccountRow]: ...

    # --- spec/05 PositionStore members the engine touches ---
    def get_window(self, window_id: str) -> SettlementWindowRow | None: ...

    def update_window(self, window_id: str, **changes: object) -> SettlementWindowRow: ...

    def get_position_account(
        self, participant_id: str, window_id: str
    ) -> PositionAccountRow | None: ...

    def update_position_account(
        self, position_account_id: str, **changes: object
    ) -> PositionAccountRow: ...

    def active_cap(self, participant_id: str): ...


class InMemoryNettingPositionStore(InMemoryPositionStore):
    """Deterministic in-memory store with the window-position read."""

    def list_window_positions(self, settlement_window_id: str) -> list[PositionAccountRow]:
        rows = [
            row
            for row in self._positions.values()
            if row.settlement_window_id == settlement_window_id
        ]
        return sorted(rows, key=lambda r: r.participant_id)


class PostgresNettingPositionStore(PostgresPositionStore):
    """Postgres store with the window-position read (participant_id ASC)."""

    def list_window_positions(self, settlement_window_id: str) -> list[PositionAccountRow]:
        cur = self._conn.execute(
            "SELECT position_account_id, participant_id, settlement_window_id,"
            " net_position_minor, currency, net_debit_cap_minor, debit_count,"
            " credit_count, last_movement_at, window_state, created_at, schema_version"
            " FROM ledger.position_accounts WHERE settlement_window_id = %s"
            " ORDER BY participant_id ASC",
            (settlement_window_id,),
        )
        return [PositionAccountRow(*row) for row in cur.fetchall()]


class ParticipantDirectory(Protocol):
    """Participant lookups the netting engine needs (kernel-owned data)."""

    def kyb_status(self, participant_id: str) -> str | None: ...

    def settlement_account_token(self, participant_id: str) -> str | None: ...


class StaticParticipantDirectory:
    """Deterministic in-memory directory (tests + simulator compositions)."""

    def __init__(
        self,
        *,
        statuses: dict[str, str] | None = None,
        account_tokens: dict[str, str] | None = None,
    ) -> None:
        self._statuses = dict(statuses or {})
        self._tokens = dict(account_tokens or {})

    def set_status(self, participant_id: str, kyb_status: str) -> None:
        self._statuses[participant_id] = kyb_status

    def remove_status(self, participant_id: str) -> None:
        """Drop the row (models a participant with no kernel record)."""
        self._statuses.pop(participant_id, None)

    def kyb_status(self, participant_id: str) -> str | None:
        return self._statuses.get(participant_id)

    def settlement_account_token(self, participant_id: str) -> str | None:
        return self._tokens.get(participant_id)


class PositionPostingService:
    """The D-19-6 attributed posting path for PSO position movements.

    ``post_transfer`` writes ONE balanced journal entry (DEBIT the paying
    participant's position account, CREDIT the receiving participant's) with
    the window attribution; the suspended-participant guard refuses BEFORE
    anything is written (mirror of the 0102 trigger guard — refusal order:
    suspension, then NDC, then the ledger write).

    ``post_unwind_reversal`` writes the D-19-2 offsetting entry for one
    original window entry (sides swapped, never deletion) and applies the
    maintained-mirror deltas directly — the unwind runs after cutoff, so the
    mid-window accumulation path does not apply.
    """

    def __init__(
        self,
        *,
        ledger: LedgerService,
        store: NettingPositionStore,
        directory: ParticipantDirectory,
        maintained_mirror: PositionService | None,
        producer: str = PRODUCER,
    ) -> None:
        self._ledger = ledger
        self._store = store
        self._directory = directory
        self._mirror = maintained_mirror
        self._producer = producer

    # ------------------------------------------------------------------
    # Forward window facts (ACCUMULATING_POSITIONS)
    # ------------------------------------------------------------------

    def post_transfer(
        self,
        *,
        settlement_window_id: str,
        from_participant_id: str,
        to_participant_id: str,
        amount_minor: int,
        source_payment_id: str,
    ) -> str:
        """One inter-participant position movement; returns the entry id."""
        if from_participant_id == to_participant_id:
            raise NettingError("a position transfer needs two distinct participants")
        window = self._require_window(settlement_window_id)
        if window.status != "ACCUMULATING_POSITIONS":
            raise NettingError(
                f"window {settlement_window_id} is {window.status}; position facts "
                "post only while ACCUMULATING_POSITIONS (refusal-first)"
            )
        for participant_id in (from_participant_id, to_participant_id):
            self._require_active(participant_id)
        if self._mirror is not None:
            # Refusal order mirrors the DB trigger: the debit-side NDC check
            # runs (and fails closed, writing the breach log) before any
            # ledger fact is written.
            self._mirror.post_debit(
                from_participant_id,
                amount_minor,
                source_payment_id=source_payment_id,
            )
            self._mirror.post_credit(to_participant_id, amount_minor)
        return self._ledger.post_journal_entry(
            JournalEntrySpec(
                reference_id=settlement_window_id,  # D-19-6 attribution
                reference_type="SETTLEMENT",
                entry_type="position_updated",
                description=(
                    f"PSO position movement {source_payment_id} "
                    f"({from_participant_id} -> {to_participant_id})"
                ),
                produced_by=self._producer,
                idempotency_key=make_id(
                    "idem",
                    {
                        "settlement_window_id": settlement_window_id,
                        "source_payment_id": source_payment_id,
                        "from": from_participant_id,
                        "to": to_participant_id,
                        "amount_minor": amount_minor,
                    },
                ),
                postings=(
                    PostingSpec(
                        account_id=participant_position_account_id(from_participant_id),
                        side="DEBIT",
                        amount_minor=amount_minor,
                    ),
                    PostingSpec(
                        account_id=participant_position_account_id(to_participant_id),
                        side="CREDIT",
                        amount_minor=amount_minor,
                    ),
                ),
            )
        )

    # ------------------------------------------------------------------
    # D-19-2 unwind reversals (post-cutoff; operator-approved path)
    # ------------------------------------------------------------------

    def post_unwind_reversal(
        self,
        *,
        settlement_window_id: str,
        fact: PositionEntryFact,
        excluded_participant_id: str,
    ) -> str:
        """Offsetting entry for one original window entry (sides swapped)."""
        window = self._require_window(settlement_window_id)
        if window.status not in ("CUTOFF_REACHED", "SETTLEMENT_FAILED"):
            raise NettingError(
                f"unwind reversals post only on CUTOFF_REACHED/SETTLEMENT_FAILED "
                f"windows; {settlement_window_id} is {window.status}"
            )
        if fact.entry_type == "payment_reversed":
            raise NettingError(
                f"entry {fact.entry_id} is already a reversal; offsetting a "
                "reversal is denied (one unwind round, D-19-2)"
            )
        entry_id = self._ledger.post_journal_entry(
            JournalEntrySpec(
                reference_id=settlement_window_id,  # attribution: reversal is a window fact
                reference_type="SETTLEMENT",
                entry_type="payment_reversed",
                description=(
                    f"PSO unwind offset of {fact.entry_id} "
                    f"(excluded participant {excluded_participant_id})"
                ),
                produced_by=self._producer,
                idempotency_key=make_id(
                    "idem",
                    {
                        "unwind_of_entry": fact.entry_id,
                        "settlement_window_id": settlement_window_id,
                        "excluded_participant_id": excluded_participant_id,
                    },
                ),
                postings=tuple(
                    PostingSpec(
                        account_id=posting.account_id,
                        side="CREDIT" if posting.side == "DEBIT" else "DEBIT",
                        amount_minor=posting.amount_minor,
                    )
                    for posting in fact.postings
                ),
            )
        )
        # Maintained-mirror deltas (in-memory composition). On Postgres the
        # 0102 trigger reversal path applies the identical deltas; the engine's
        # derived-vs-maintained integrity check fails closed on any divergence.
        if self._mirror is not None:
            for posting, participant_id in zip(
                fact.postings, fact.participant_by_posting, strict=True
            ):
                row = self._store.get_position_account(participant_id, settlement_window_id)
                if row is None:
                    raise NettingError(
                        f"no position account for {participant_id} in window "
                        f"{settlement_window_id} (cannot apply unwind delta)"
                    )
                # Reversal swaps the side: an original DEBIT comes back as a
                # CREDIT (position increases), an original CREDIT as a DEBIT.
                delta = posting.amount_minor if posting.side == "DEBIT" else -posting.amount_minor
                self._store.update_position_account(
                    row.position_account_id,
                    net_position_minor=row.net_position_minor + delta,
                )
        return entry_id

    # ------------------------------------------------------------------
    # Guards
    # ------------------------------------------------------------------

    def _require_active(self, participant_id: str) -> None:
        status = self._directory.kyb_status(participant_id)
        if status is not None and status != "ACTIVE":
            raise NdcParticipantNotActiveError(participant_id, status)
        # A missing directory row does NOT refuse: cross-package integrity is
        # application-enforced (E20 posture) and pre-PSO ledger flows carry no
        # participants rows at all (errata N19-4).

    def _require_window(self, settlement_window_id: str) -> SettlementWindowRow:
        window = self._store.get_window(settlement_window_id)
        if window is None:
            raise NettingError(f"unknown settlement window {settlement_window_id}")
        return window

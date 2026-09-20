"""SettlementEngine (spec/04) — PSP-mode merchant payout settlement.

Implements the binding FSM 1 (SettlementBatch) and FSM 2
(SettlementInstruction) transition tables refusal-first, the 5-working-day
payout mandate with delivery-verification holds, the synchronous TCSA
dispatch gate (fails CLOSED on absent/stale/blocked snapshots), and the
``settlement_batch_open`` / ``settlement_batch_close`` journal entries — all
money movement via ``LedgerService.post_journal_entry`` (sole writer rule).

Every transition writes one audit_events row (via
``LedgerService.write_audit_event``) and the spec/04 outbox event (via
``LedgerService.emit_event``); money-moving transitions additionally write
the journal bundle. Denied transitions raise and write nothing.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from bdpay.ledger.calendar_ext import add_working_days, business_day_start_utc
from bdpay.ledger.errors import (
    LedgerDuplicateError,
    SettlementError,
    SettlementGuardError,
    SettlementInvalidTransitionError,
)
from bdpay.ledger.service import LedgerService
from bdpay.ledger.settlement.beftn import BeftnBatchHandoff, build_handoff
from bdpay.ledger.settlement.store import SettlementStore
from bdpay.ledger.settlement.types import (
    BATCH_DISPATCH_TIMEOUT_MINUTES,
    BATCH_TRANSITIONS,
    DIRECTIONS,
    DISPATCH_TWO_EYES_THRESHOLD_MINOR,
    HIGH_VALUE_THRESHOLD_MINOR,
    INSTRUCTION_TRANSITIONS,
    MAX_RETRIES,
    MCC_CATEGORIES,
    PRODUCER,
    RAILS,
    RETRY_TWO_EYES_THRESHOLD_MINOR,
    SettlementBatchRow,
    SettlementInstructionRow,
)
from bdpay.ledger.types import AuditEventSpec, JournalEntrySpec, PostingSpec
from bdpay.platform.clock import Clock
from bdpay.platform.ids import make_id
from bdpay.platform.scheduler import DHAKA_TZ, BangladeshBankCalendar

__all__ = ["SettlementEngine", "TcsaGateResult"]


@dataclass(frozen=True, slots=True)
class TcsaGateResult:
    """Result of the synchronous TCSA dispatch gate (spec/04 FSM 1).

    ``clear=False`` always means: do not dispatch (fails closed).
    ``stale=True`` distinguishes the monitor-down case for the distinct
    ops alert the spec requires.
    """

    clear: bool
    snapshot_id: str | None = None
    shortfall_minor: int = 0
    stale: bool = False


class SettlementEngine:
    """settlement-engine@1 — logical sub-service inside the ledger package."""

    def __init__(
        self,
        store: SettlementStore,
        ledger: LedgerService,
        *,
        clock: Clock,
        calendar: BangladeshBankCalendar | None = None,
        tcsa_gate: Callable[[], TcsaGateResult],
        transit_accounts: dict[str, str],
        merchant_settlement_resolver: Callable[[str], str],
        tcsa_account_id: str,
        alert: Callable[[str, dict], None] | None = None,
        producer: str = PRODUCER,
    ) -> None:
        self._store = store
        self._ledger = ledger
        self._clock = clock
        self._calendar = calendar or BangladeshBankCalendar()
        self._tcsa_gate = tcsa_gate
        self._transit_accounts = dict(transit_accounts)
        self._merchant_account = merchant_settlement_resolver
        self._tcsa_account_id = tcsa_account_id
        self._alert = alert or (lambda _kind, _payload: None)
        self._producer = producer

    # ------------------------------------------------------------------
    # FSM plumbing — refusal-first
    # ------------------------------------------------------------------

    def _batch_transition(self, batch: SettlementBatchRow, trigger: str, to_state: str) -> None:
        allowed = BATCH_TRANSITIONS.get((batch.status, trigger))
        if allowed is None or to_state not in allowed:
            raise SettlementInvalidTransitionError(
                f"batch {batch.batch_id}: {batch.status} --[{trigger}]--> {to_state} "
                "is not in the spec/04 FSM 1 table (denied)"
            )

    def _instruction_transition(
        self, instruction: SettlementInstructionRow, trigger: str, to_state: str
    ) -> None:
        allowed = INSTRUCTION_TRANSITIONS.get((instruction.status, trigger))
        if allowed is None or to_state not in allowed:
            raise SettlementInvalidTransitionError(
                f"instruction {instruction.instruction_id}: {instruction.status} "
                f"--[{trigger}]--> {to_state} is not in the spec/04 FSM 2 table (denied)"
            )

    def _audit_batch(
        self,
        batch_id: str,
        from_state: str | None,
        to_state: str,
        detail: dict,
        *,
        occurred_at: datetime | None = None,
    ) -> None:
        self._ledger.write_audit_event(
            AuditEventSpec(
                event_type="SETTLEMENT_BATCH_STATE_TRANSITION",
                actor_id=self._producer,
                actor_type="SERVICE",
                subject_type="SettlementBatch",
                subject_id=batch_id,
                from_state=from_state,
                to_state=to_state,
                payload=detail,
            ),
            occurred_at=occurred_at,
        )

    def _audit_instruction(
        self,
        instruction_id: str,
        from_state: str | None,
        to_state: str,
        detail: dict,
        *,
        occurred_at: datetime | None = None,
    ) -> None:
        self._ledger.write_audit_event(
            AuditEventSpec(
                event_type="SETTLEMENT_INSTRUCTION_STATE_TRANSITION",
                actor_id=self._producer,
                actor_type="SERVICE",
                subject_type="SettlementInstruction",
                subject_id=instruction_id,
                from_state=from_state,
                to_state=to_state,
                payload=detail,
            ),
            occurred_at=occurred_at,
        )

    def _emit(
        self,
        event_type: str,
        subject_type: str,
        subject_id: str,
        payload: dict,
        *,
        occurred_at: datetime | None = None,
    ) -> None:
        self._ledger.emit_event(
            event_type=event_type,
            subject_type=subject_type,
            subject_id=subject_id,
            payload=payload,
            topic="settlement.events",
            producer=self._producer,
            occurred_at=occurred_at,
        )

    def _post_batch_open_reversals(self, batch_id: str) -> None:
        """Reverse every settlement_batch_open JE posted for this batch.

        Called ONLY when a batch reaches terminal-CANCELLED state
        (operator_cancel or max_retries_exceeded) to unstage the float that
        was moved into TCSA at assign_due_instructions time.  Must NOT be
        called at _fail_batch (FAILED is non-terminal; the batch may retry
        and confirm, and confirm() posts settlement_batch_close which is the
        intended TCSA discharge — calling this first would double-credit
        TCSA and push it negative).

        Uses JE-lookup (not instruction iteration) so that we reverse exactly
        the posted JEs — no more, no less — regardless of how the instruction
        table looks at cancel time.  Each reversal is keyed on the open JE's
        entry_id, making the "already reversed" check explicit (query first,
        don't rely only on the idempotency backstop) and stable against
        instruction-set mutations.

        Direction: DR transit_account / CR TCSA (exact inverse of batch_open
        which is DR TCSA / CR transit_account).
        """
        # Fetch the set of already-posted reversals for this batch so we can
        # skip JEs that are already covered (explicit check — not just idem key).
        existing_reversals = self._ledger.list_journal_entries_by_reference_and_type(
            batch_id, "settlement_batch_open_reversal"
        )
        already_reversed: set[str] = {je.idempotency_key for je in existing_reversals}

        # Iterate the POSTED settlement_batch_open JEs for this batch; these
        # are the authoritative record of what float was staged, regardless of
        # the current instruction-table state.
        open_jes = self._ledger.list_journal_entries_by_reference_and_type(
            batch_id, "settlement_batch_open"
        )
        for open_je in open_jes:
            # Derive the reversal idempotency key from the open JE's entry_id
            # (stable: one reversal per posted open JE, regardless of retry).
            reversal_idem_key = make_id(
                "idem",
                {
                    "open_entry_id": open_je.entry_id,
                    "entry_type": "settlement_batch_open_reversal",
                },
            )
            # Skip JEs that are already reversed (explicit pre-check).
            if reversal_idem_key in already_reversed:
                continue
            # Build the reversal postings by flipping EVERY posting from the
            # open JE (DEBIT→CREDIT, CREDIT→DEBIT).  This makes the reversal
            # a true inversion regardless of the posting count or structure,
            # and ensures the trial balance holds for any valid open JE shape.
            open_postings = self._ledger.get_postings_for_entry(open_je.entry_id)
            if not open_postings:
                raise SettlementError(
                    f"settlement_batch_open JE {open_je.entry_id} has no postings "
                    f"(cannot invert for reversal)"
                )
            reversal_postings = tuple(
                PostingSpec(
                    account_id=p.account_id,
                    side="CREDIT" if p.side == "DEBIT" else "DEBIT",
                    amount_minor=p.amount_minor,
                )
                for p in open_postings
            )
            self._ledger.post_journal_entry(
                JournalEntrySpec(
                    reference_id=batch_id,
                    reference_type="SETTLEMENT",
                    entry_type="settlement_batch_open_reversal",
                    description=(
                        "settlement batch open reversed on terminal cancellation "
                        f"(batch {batch_id}, open_je {open_je.entry_id})"
                    ),
                    produced_by=self._producer,
                    idempotency_key=reversal_idem_key,
                    postings=reversal_postings,
                )
            )

    # ------------------------------------------------------------------
    # Release scheduling — the 5-working-day mandate (spec/04)
    # ------------------------------------------------------------------

    def calculate_earliest_release(
        self,
        succeeded_at: datetime,
        mcc_category: str,
        delivery_confirmed_at: datetime | None,
    ) -> datetime:
        """spec/04 ``calculate_earliest_release`` over the BD calendar."""
        if mcc_category not in MCC_CATEGORIES:
            raise SettlementError(f"unknown mcc_category {mcc_category!r}")
        local_date = succeeded_at.astimezone(DHAKA_TZ).date()
        five_wd = add_working_days(self._calendar, local_date, 5)
        if mcc_category == "OTHER":
            return succeeded_at  # no hold; next available batch
        hold_days = 5 if mcc_category == "DAILY_ESSENTIAL" else 7
        if delivery_confirmed_at is not None:
            hold_end = add_working_days(
                self._calendar, delivery_confirmed_at.astimezone(DHAKA_TZ).date(), 0
            )
        else:
            hold_end = add_working_days(self._calendar, local_date, hold_days)
        effective_release = min(hold_end, five_wd)  # hard 5-WD cap
        return business_day_start_utc(effective_release)

    def latest_release_at(self, succeeded_at: datetime) -> datetime:
        """``add_working_days(succeeded_at, 5)`` — the immutable statutory cap."""
        local_date = succeeded_at.astimezone(DHAKA_TZ).date()
        return business_day_start_utc(add_working_days(self._calendar, local_date, 5))

    # ------------------------------------------------------------------
    # Instruction intake (consumes payment_intent.succeeded)
    # ------------------------------------------------------------------

    def register_succeeded_payment(
        self,
        *,
        payment_intent_id: str,
        merchant_id: str,
        amount_minor: int,
        fee_minor: int,
        rail: str,
        beneficiary_account_ref: str,
        mcc_category: str,
        succeeded_at: datetime,
        fee_rule_id: str | None = None,
        delivery_confirmed_at: datetime | None = None,
        direction: str = "CREDIT",
    ) -> str:
        """Create the QUEUED SettlementInstruction for a succeeded payment."""
        if rail not in RAILS:
            raise SettlementError(f"unknown rail {rail!r}")
        if direction not in DIRECTIONS:
            raise SettlementError(f"unknown direction {direction!r}")
        if mcc_category not in MCC_CATEGORIES:
            raise SettlementError(f"unknown mcc_category {mcc_category!r}")
        if amount_minor <= 0 or fee_minor < 0 or fee_minor > amount_minor:
            raise SettlementError(
                f"invalid amounts: amount_minor={amount_minor} fee_minor={fee_minor}"
            )
        net = amount_minor - fee_minor
        instruction_id = make_id(
            "sinst",
            {
                "payment_intent_id": payment_intent_id,
                "merchant_id": merchant_id,
                "amount_minor": amount_minor,
                "rail": rail,
                "direction": direction,
            },
        )
        if self._store.get_instruction(instruction_id) is not None:
            return instruction_id  # content-addressed idempotent intake
        now = self._now()
        earliest = self.calculate_earliest_release(
            succeeded_at, mcc_category, delivery_confirmed_at
        )
        latest = self.latest_release_at(succeeded_at)
        if earliest > latest:
            earliest = latest
        row = SettlementInstructionRow(
            instruction_id=instruction_id,
            batch_id=None,
            payment_intent_id=payment_intent_id,
            merchant_id=merchant_id,
            direction=direction,
            amount_minor=amount_minor,
            fee_minor=fee_minor,
            net_payout_minor=net,
            fee_rule_id=fee_rule_id,
            currency="BDT",
            rail=rail,
            beneficiary_account_ref=beneficiary_account_ref,
            mcc_category=mcc_category,
            high_value=amount_minor >= HIGH_VALUE_THRESHOLD_MINOR,
            earliest_release_at=earliest,
            latest_release_at=latest,
            delivery_hold_cleared=(mcc_category == "OTHER" or delivery_confirmed_at is not None),
            aml_hold=False,
            status="QUEUED",
            rail_transaction_id=None,
            return_reason_code=None,
            connector_ref=instruction_id,
            created_at=now,
            submitted_at=None,
            settled_at=None,
            returned_at=None,
            failed_at=None,
            produced_by=self._producer,
        )
        self._store.insert_instruction(row)
        self._audit_instruction(instruction_id, None, "QUEUED", {"rail": rail})
        return instruction_id

    def set_aml_hold(self, payment_intent_id: str, hold: bool) -> int:
        """Consume payment_intent.aml_hold_placed / _released (spec/04)."""
        count = 0
        for row in self._store.list_instructions(
            payment_intent_id=payment_intent_id, status="QUEUED"
        ):
            self._store.update_instruction(row.instruction_id, aml_hold=hold)
            count += 1
        return count

    def confirm_delivery(self, payment_intent_id: str, confirmed_at: datetime) -> int:
        """Consume payment_intent.delivery_confirmed: clear holds, re-evaluate."""
        count = 0
        for row in self._store.list_instructions(
            payment_intent_id=payment_intent_id, status="QUEUED"
        ):
            earliest = self.calculate_earliest_release(
                row.created_at, row.mcc_category, confirmed_at
            )
            self._store.update_instruction(
                row.instruction_id,
                delivery_hold_cleared=True,
                earliest_release_at=min(earliest, row.latest_release_at),
            )
            count += 1
        return count

    # ------------------------------------------------------------------
    # Batch accumulation
    # ------------------------------------------------------------------

    def _get_or_create_open_batch(
        self, rail: str, cycle_date: date, session: str | None
    ) -> SettlementBatchRow:
        existing = self._store.find_open_batch(rail, cycle_date, session)
        if existing is not None:
            return existing
        now = self._now()
        batch_id = make_id(
            "sbatch",
            {"rail": rail, "cycle_date": cycle_date.isoformat(), "session": session},
        )
        row = SettlementBatchRow(
            batch_id=batch_id,
            rail=rail,
            cycle_date=cycle_date,
            session=session,
            status="OPEN",
            instruction_count=0,
            total_credit_minor=0,
            total_debit_minor=0,
            currency="BDT",
            retry_count=0,
            two_eyes_required=False,
            approval_request_id=None,
            held_at=None,
            held_tcsa_snapshot_id=None,
            sponsor_bank_ref=None,
            recon_file_pointer=None,
            recon_file_hash=None,
            connector_ref=None,
            created_at=now,
            cutoff_at=None,
            dispatched_at=None,
            confirmed_at=None,
            failed_at=None,
            cancelled_at=None,
            fail_reason_code=None,
            produced_by=self._producer,
        )
        self._store.insert_batch(row)
        return row

    def _releasable(self, row: SettlementInstructionRow, now: datetime) -> bool:
        if row.status != "QUEUED" or row.batch_id is not None:
            return False
        if row.aml_hold:
            # MLPA freeze outranks the payout mandate (spec/04 cut-last note).
            return False
        if now >= row.latest_release_at:
            return True  # statutory deadline: released unconditionally
        return row.delivery_hold_cleared and now >= row.earliest_release_at

    def assign_due_instructions(self, now: datetime | None = None) -> list[str]:
        """Add every releasable instruction to its rail's OPEN batch.

        Writes the ``settlement_batch_open`` journal entry per assignment
        (DR rail in-transit / CR merchant settlement, spec/04 FSM 1 note —
        per-assignment so the entry stays balanced incrementally; errata
        LA-L10) and updates the batch totals.
        """
        now = now or self._now()
        cycle_date = now.astimezone(DHAKA_TZ).date()
        assigned: list[str] = []
        for row in self._store.list_instructions(status="QUEUED", unassigned_only=True):
            if not self._releasable(row, now):
                continue
            statutory = now >= row.latest_release_at and not (
                row.delivery_hold_cleared and now >= row.earliest_release_at
            )
            session = None
            if row.rail == "beftn_batch_v2":
                beftn_session = self._calendar.beftn_session(now)
                session = {
                    "morning": "morning",
                    "afternoon": "afternoon",
                    "end_of_day": "eod",
                }.get(beftn_session.value if beftn_session else "", "eod")
            batch = self._get_or_create_open_batch(row.rail, cycle_date, session)
            transit_account = self._transit_accounts.get(row.rail)
            if transit_account is None:
                raise SettlementError(f"no in-transit account configured for rail {row.rail!r}")
            # MONEY-03: the merchant payout obligation is created EXACTLY ONCE,
            # at capture (spec/02 payment_captured CR merchant_settlement net).
            # settlement_batch_open must NOT re-credit merchant_settlement (that
            # double-counted the net: capture CR + batch_open CR, only one DR at
            # close -> merchant_settlement stuck at +net forever). Instead it
            # stages the already-collected customer float out of the rail transit
            # account into the sponsor-bank trust position (TCSA), so that
            # settlement_batch_close (DR merchant_settlement / CR sponsor_bank_tcsa)
            # lands BOTH merchant_settlement and TCSA back at zero — the net is
            # credited once (capture) and discharged once (close). Without this
            # TCSA debit, close would credit a never-debited TCSA into a negative
            # asset balance.
            self._ledger.post_journal_entry(
                JournalEntrySpec(
                    reference_id=batch.batch_id,
                    reference_type="SETTLEMENT",
                    entry_type="settlement_batch_open",
                    description="settlement batch accumulation (rail float staged to TCSA)",
                    produced_by=self._producer,
                    idempotency_key=make_id(
                        "idem",
                        {
                            "batch_id": batch.batch_id,
                            "instruction_id": row.instruction_id,
                            "entry_type": "settlement_batch_open",
                        },
                    ),
                    postings=(
                        PostingSpec(
                            account_id=self._tcsa_account_id,
                            side="DEBIT",
                            amount_minor=row.net_payout_minor,
                        ),
                        PostingSpec(
                            account_id=transit_account,
                            side="CREDIT",
                            amount_minor=row.net_payout_minor,
                        ),
                    ),
                )
            )
            updated_batch = self._store.update_batch(
                batch.batch_id,
                instruction_count=batch.instruction_count + 1,
                total_credit_minor=batch.total_credit_minor
                + (row.net_payout_minor if row.direction == "CREDIT" else 0),
                total_debit_minor=batch.total_debit_minor
                + (row.net_payout_minor if row.direction == "DEBIT" else 0),
            )
            changes: dict[str, object] = {"batch_id": batch.batch_id}
            if statutory:
                changes["delivery_hold_cleared"] = True
            self._store.update_instruction(row.instruction_id, **changes)
            if statutory:
                self._audit_instruction(
                    row.instruction_id,
                    "QUEUED",
                    "QUEUED",
                    {"released_at_statutory_deadline": True},
                )
            if updated_batch.total_credit_minor > DISPATCH_TWO_EYES_THRESHOLD_MINOR:
                self._store.update_batch(batch.batch_id, two_eyes_required=True)
            assigned.append(row.instruction_id)
        return assigned

    # ------------------------------------------------------------------
    # FSM 1 triggers
    # ------------------------------------------------------------------

    def cutoff(self, batch_id: str, now: datetime | None = None) -> SettlementBatchRow:
        """``batch_cutoff_reached``: OPEN -> PENDING_DISPATCH | CANCELLED."""
        batch = self._require_batch(batch_id)
        now = now or self._now()
        if batch.instruction_count >= 1:
            self._batch_transition(batch, "batch_cutoff_reached", "PENDING_DISPATCH")
            updated = self._store.update_batch(batch_id, status="PENDING_DISPATCH", cutoff_at=now)
            self._audit_batch(batch_id, "OPEN", "PENDING_DISPATCH", {"trigger": "cutoff"})
            self._emit(
                "settlement_batch.pending_dispatch",
                "SettlementBatch",
                batch_id,
                {
                    "rail": batch.rail,
                    "cycle_date": batch.cycle_date.isoformat(),
                    "session": batch.session,
                    "instruction_count": batch.instruction_count,
                    "total_credit_minor": batch.total_credit_minor,
                },
            )
            return updated
        self._batch_transition(batch, "batch_cutoff_reached", "CANCELLED")
        updated = self._store.update_batch(
            batch_id, status="CANCELLED", cancelled_at=now, fail_reason_code="EMPTY_BATCH"
        )
        self._audit_batch(batch_id, "OPEN", "CANCELLED", {"reason": "EMPTY_BATCH"})
        self._emit(
            "settlement_batch.cancelled",
            "SettlementBatch",
            batch_id,
            {"rail": batch.rail, "reason": "EMPTY_BATCH"},
        )
        return updated

    def grant_approval(self, batch_id: str, approval_request_id: str) -> SettlementBatchRow:
        """``approval_granted``: PENDING_DISPATCH -> PENDING_DISPATCH (unblocks)."""
        batch = self._require_batch(batch_id)
        self._batch_transition(batch, "approval_granted", "PENDING_DISPATCH")
        updated = self._store.update_batch(
            batch_id, approval_request_id=approval_request_id, two_eyes_required=False
        )
        self._audit_batch(
            batch_id,
            "PENDING_DISPATCH",
            "PENDING_DISPATCH",
            {"approval_request_id": approval_request_id},
        )
        return updated

    def try_dispatch(
        self, batch_id: str, now: datetime | None = None
    ) -> tuple[SettlementBatchRow, BeftnBatchHandoff | None]:
        """``dispatch_triggered``: PENDING_DISPATCH -> DISPATCHED | HELD.

        The TCSA dispatch gate runs synchronously, immediately before the
        handoff is built; absence of data fails CLOSED, never open.
        """
        batch = self._require_batch(batch_id)
        now = now or self._now()
        if batch.two_eyes_required:
            raise SettlementGuardError(
                f"batch {batch_id} requires two-eyes approval before dispatch"
            )
        # The transition must exist before the gate decides which branch.
        self._batch_transition(batch, "dispatch_triggered", "DISPATCHED")
        gate = self._tcsa_gate()
        if not gate.clear:
            self._batch_transition(batch, "dispatch_triggered", "HELD")
            updated = self._store.update_batch(
                batch_id,
                status="HELD",
                held_at=now,
                held_tcsa_snapshot_id=gate.snapshot_id,
            )
            self._audit_batch(
                batch_id,
                "PENDING_DISPATCH",
                "HELD",
                {
                    "held_tcsa_snapshot_id": gate.snapshot_id,
                    "shortfall_minor": gate.shortfall_minor,
                    "stale_snapshot": gate.stale,
                },
            )
            self._emit(
                "settlement_batch.held",
                "SettlementBatch",
                batch_id,
                {
                    "rail": batch.rail,
                    "held_tcsa_snapshot_id": gate.snapshot_id,
                    "shortfall_minor": gate.shortfall_minor,
                },
            )
            self._alert(
                "tcsa-monitor-down" if gate.stale else "tcsa-shortfall-hold",
                {"batch_id": batch_id, "shortfall_minor": gate.shortfall_minor},
            )
            return updated, None
        updated = self._store.update_batch(
            batch_id, status="DISPATCHED", dispatched_at=now, connector_ref=batch_id
        )
        submitted: list[SettlementInstructionRow] = []
        for row in self._store.list_instructions(batch_id=batch_id, status="QUEUED"):
            self._instruction_transition(row, "batch_dispatched", "SUBMITTED")
            self._store.update_instruction(row.instruction_id, status="SUBMITTED", submitted_at=now)
            self._audit_instruction(row.instruction_id, "QUEUED", "SUBMITTED", {})
            submitted.append(self._require_instruction(row.instruction_id))
        self._audit_batch(batch_id, "PENDING_DISPATCH", "DISPATCHED", {"rail": batch.rail})
        self._emit(
            "settlement_batch.dispatched",
            "SettlementBatch",
            batch_id,
            {
                "rail": batch.rail,
                "cycle_date": batch.cycle_date.isoformat(),
                "connector_ref": batch_id,
            },
        )
        handoff = build_handoff(updated, submitted) if batch.rail == "beftn_batch_v2" else None
        return updated, handoff

    def release_held(self, batch_id: str, now: datetime | None = None) -> SettlementBatchRow:
        """``tcsa_shortfall_resolved``: HELD -> PENDING_DISPATCH (gate re-checked)."""
        batch = self._require_batch(batch_id)
        now = now or self._now()
        self._batch_transition(batch, "tcsa_shortfall_resolved", "PENDING_DISPATCH")
        gate = self._tcsa_gate()
        if not gate.clear:
            raise SettlementGuardError(
                f"batch {batch_id} stays HELD: payout initiation is still blocked"
            )
        updated = self._store.update_batch(batch_id, status="PENDING_DISPATCH")
        self._audit_batch(batch_id, "HELD", "PENDING_DISPATCH", {"released_at": now.isoformat()})
        self._emit(
            "settlement_batch.released",
            "SettlementBatch",
            batch_id,
            {
                "rail": batch.rail,
                "released_at": now.isoformat(),
                "releasing_tcsa_snapshot_id": gate.snapshot_id,
            },
        )
        return updated

    def confirm(
        self,
        batch_id: str,
        *,
        sponsor_bank_ref: str | None = None,
        rail_transaction_ids: dict[str, str],
        now: datetime | None = None,
    ) -> SettlementBatchRow:
        """``sponsor_bank_confirmed``: DISPATCHED -> CONFIRMED (money moves).

        Crash-safe resume semantics:
        - A DISPATCHED batch whose confirmation payload was persisted before
          the close JE (process crashed before phase 1) completes phases 1-3
          on the next resume.
        - A DISPATCHED batch whose ``settlement_batch_close`` JE was already
          posted (process crashed between phase 1 and phase 2) completes
          phases 2 and 3 without double-posting the JE.
        - A CONFIRMED batch with still-SUBMITTED instructions (process crashed
          mid-phase 3) completes only the remaining instructions.
        - Missing batch/instruction audit and outbox events are reconciled
          idempotently once the batch reaches CONFIRMED.
        """
        batch = self._require_batch(batch_id)
        now = now or self._now()
        resume_mode = batch.status == "CONFIRMED"
        if resume_mode:
            sponsor_bank_ref = sponsor_bank_ref or batch.sponsor_bank_ref
        elif sponsor_bank_ref is None:
            # A previous attempt may have persisted the sponsor ref before the
            # close JE; allow the caller to omit it only when it is already durable.
            sponsor_bank_ref = batch.sponsor_bank_ref
            if sponsor_bank_ref is None:
                raise SettlementError(
                    f"batch {batch_id}: sponsor_bank_ref is required for a fresh confirmation"
                )
        if (
            batch.sponsor_bank_ref is not None
            and sponsor_bank_ref != batch.sponsor_bank_ref
        ):
            raise SettlementError(
                f"batch {batch_id}: sponsor_bank_ref cannot overwrite already-durable "
                f"confirmation evidence ({batch.sponsor_bank_ref!r} -> {sponsor_bank_ref!r})"
            )
        self._batch_transition(batch, "sponsor_bank_confirmed", "CONFIRMED")
        instructions = self._store.list_instructions(batch_id=batch_id, status="SUBMITTED")
        if not resume_mode:
            # Persist the confirmation payload BEFORE the money-moving journal entry.
            # This is done atomically through the settlement store so that the
            # sponsor-bank marker and per-instruction rail tx ids are either all
            # durable or all absent.  A partial write cannot hide rail evidence
            # from ``resume_confirmations()`` / ``timeout_check()``.
            assert sponsor_bank_ref is not None
            self._store.persist_confirmation_evidence(
                batch_id,
                sponsor_bank_ref=sponsor_bank_ref,
                rail_transaction_ids=rail_transaction_ids,
            )
            # Re-read so the settle loop sees the persisted rail tx ids.
            instructions = self._store.list_instructions(batch_id=batch_id, status="SUBMITTED")
        if instructions and not resume_mode:
            # Phase 1: post settlement_batch_close (DR merchant_settlement / CR TCSA).
            per_merchant: dict[str, int] = {}
            for row in instructions:
                per_merchant[row.merchant_id] = (
                    per_merchant.get(row.merchant_id, 0) + row.net_payout_minor
                )
            total = sum(per_merchant.values())
            postings = [
                PostingSpec(
                    account_id=self._merchant_account(merchant_id),
                    side="DEBIT",
                    amount_minor=amount,
                )
                for merchant_id, amount in sorted(per_merchant.items())
            ]
            postings.append(
                PostingSpec(account_id=self._tcsa_account_id, side="CREDIT", amount_minor=total)
            )
            close_idem_key = make_id(
                "idem", {"batch_id": batch_id, "entry_type": "settlement_batch_close"}
            )
            try:
                self._ledger.post_journal_entry(
                    JournalEntrySpec(
                        reference_id=batch_id,
                        reference_type="SETTLEMENT",
                        entry_type="settlement_batch_close",
                        description="settlement batch confirmed; merchant obligations cleared",
                        produced_by=self._producer,
                        idempotency_key=close_idem_key,
                        postings=tuple(postings),
                    )
                )
            except LedgerDuplicateError:
                # Crash window A: the close JE is already posted. Verify it exists
                # for this batch before treating phase 1 as complete; otherwise the
                # duplicate signal is for a different key collision and must fail.
                existing_close = self._ledger.list_journal_entries_by_reference_and_type(
                    batch_id, "settlement_batch_close"
                )
                if not existing_close:
                    raise
        if not resume_mode:
            # Phase 2: commit batch state to CONFIRMED.
            updated = self._store.update_batch(
                batch_id,
                status="CONFIRMED",
                confirmed_at=now,
                sponsor_bank_ref=sponsor_bank_ref,
            )
        else:
            updated = batch
        # Phase 3: settle every instruction still in SUBMITTED. Already-terminal
        # instructions are skipped by the query, making this loop idempotent per
        # instruction. A rail tx id that was persisted before the close JE (or
        # during an earlier partial confirmation) is durable money-movement proof
        # and must never be overwritten by a later caller-supplied value.
        for row in instructions:
            self._instruction_transition(row, "rail_confirmed", "SETTLED")
            settled_tx = row.rail_transaction_id or rail_transaction_ids.get(
                row.instruction_id
            )
            self._store.update_instruction(
                row.instruction_id,
                status="SETTLED",
                settled_at=now,
                rail_transaction_id=settled_tx,
            )
            self._audit_instruction(row.instruction_id, "SUBMITTED", "SETTLED", {}, occurred_at=now)
            self._emit(
                "settlement_instruction.settled",
                "SettlementInstruction",
                row.instruction_id,
                {
                    "batch_id": batch_id,
                    "merchant_id": row.merchant_id,
                    "net_payout_minor": row.net_payout_minor,
                    "rail_transaction_id": settled_tx,
                    "settled_at": now.isoformat(),
                },
                occurred_at=now,
            )
        # Repair any terminal instructions that were updated to SETTLED but
        # crashed before their audit/outbox events were emitted.
        if updated.status == "CONFIRMED":
            self._reconcile_settled_instruction_events(batch_id)
            self._reconcile_batch_confirmed_events(batch_id)
        # Cut-last item: trial balance must hold after money movement.
        self._ledger.trial_balance_assertion()
        return updated

    def resume_confirmations(self, now: datetime | None = None) -> list[str]:
        """Scheduled retry entry point for stuck settlement confirmations.

        Re-enters ``confirm()`` for batches stranded in the crash windows:
        - CONFIRMED batches with still-SUBMITTED instructions (phase 3 incomplete).
        - DISPATCHED batches whose confirmation evidence has already been
          persisted (``sponsor_bank_ref`` is set).  The close JE may or may not
          be posted yet; ``confirm()`` is idempotent for both.

        Also reconciles any terminal SETTLED instructions and CONFIRMED batches
        that are missing their audit/outbox events.

        Returns the list of batch_ids that were driven forward.
        """
        now = now or self._now()
        resumed: list[str] = []

        def _note(batch_id: str) -> None:
            if batch_id not in resumed:
                resumed.append(batch_id)

        # Crash window B: batch confirmed, instructions not yet terminal.
        for batch in self._store.list_batches(status="CONFIRMED"):
            pending = self._store.list_instructions(batch_id=batch.batch_id, status="SUBMITTED")
            if pending:
                self.confirm(
                    batch.batch_id,
                    sponsor_bank_ref=batch.sponsor_bank_ref,
                    rail_transaction_ids={},
                    now=now,
                )
                _note(batch.batch_id)
            # Repair missing terminal events.  Instruction-level events are
            # emitted before batch-level events, so if a batch is missing the
            # batch-level evidence we must reconcile instructions FIRST and then
            # the batch.  If the scheduler crashes after instruction repair but
            # before batch repair, the next tick will still see missing batch
            # evidence and rerun the idempotent instruction scan.  Healthy
            # batches (batch evidence already present) skip the per-instruction
            # scan entirely.
            if not self._batch_confirmed_events_exist(batch.batch_id):
                if self._reconcile_settled_instruction_events(batch.batch_id):
                    _note(batch.batch_id)
                if self._reconcile_batch_confirmed_events(batch.batch_id):
                    _note(batch.batch_id)
        # Crash window A: batch still dispatched but confirmation evidence has
        # already been persisted.  ``sponsor_bank_ref`` is the durable marker
        # that a confirmation is in flight; if it is set, the close JE may or
        # may not be posted yet, and ``confirm()`` is idempotent for both.
        for batch in self._store.list_batches(status="DISPATCHED"):
            close_jes = self._ledger.list_journal_entries_by_reference_and_type(
                batch.batch_id, "settlement_batch_close"
            )
            if batch.sponsor_bank_ref is not None:
                self.confirm(
                    batch.batch_id,
                    sponsor_bank_ref=batch.sponsor_bank_ref,
                    rail_transaction_ids={},
                    now=now,
                )
                _note(batch.batch_id)
            elif close_jes:
                # Pre-fix stranded state: a close JE exists but the durable
                # sponsor_bank_ref marker was never written.  This is not
                # recoverable without the sponsor reference, so alert ops.
                self._alert(
                    "settlement-unrecoverable-close-je-without-sponsor-ref",
                    {"batch_id": batch.batch_id},
                )
        return resumed

    def reject(
        self, batch_id: str, *, reason_code: str, now: datetime | None = None
    ) -> SettlementBatchRow:
        """``sponsor_bank_rejected``: DISPATCHED -> FAILED."""
        return self._fail_batch(batch_id, "sponsor_bank_rejected", reason_code, now)

    def timeout_check(self, now: datetime | None = None) -> list[str]:
        """``connector_timeout`` for DISPATCHED batches past the per-rail timeout.

        Batches that already have a durable confirmation marker
        (``sponsor_bank_ref``) are in the middle of ``confirm()`` and must be
        allowed to complete via ``resume_confirmations()`` rather than timed out.
        """
        now = now or self._now()
        failed: list[str] = []
        for batch in self._store.list_batches(status="DISPATCHED"):
            if batch.sponsor_bank_ref is not None:
                # Confirmation is in flight; resume_confirmations() will finish it.
                continue
            limit = BATCH_DISPATCH_TIMEOUT_MINUTES.get(batch.rail, 30)
            if batch.dispatched_at is not None and now - batch.dispatched_at > timedelta(
                minutes=limit
            ):
                self._fail_batch(batch.batch_id, "connector_timeout", "TIMEOUT_EXHAUSTED", now)
                failed.append(batch.batch_id)
        return failed

    def _fail_batch(
        self, batch_id: str, trigger: str, reason_code: str, now: datetime | None
    ) -> SettlementBatchRow:
        batch = self._require_batch(batch_id)
        now = now or self._now()
        if batch.status == "DISPATCHED" and batch.sponsor_bank_ref is not None:
            raise SettlementError(
                f"batch {batch_id} has durable confirmation evidence "
                f"(sponsor_bank_ref={batch.sponsor_bank_ref!r}); "
                "resume confirmations before failing"
            )
        self._batch_transition(batch, trigger, "FAILED")
        updated = self._store.update_batch(
            batch_id, status="FAILED", failed_at=now, fail_reason_code=reason_code
        )
        for row in self._store.list_instructions(batch_id=batch_id, status="SUBMITTED"):
            self._instruction_transition(row, "timeout_on_batch_fail", "FAILED")
            self._store.update_instruction(
                row.instruction_id,
                status="FAILED",
                failed_at=now,
                return_reason_code="BATCH_FAILED",
            )
            self._audit_instruction(
                row.instruction_id, "SUBMITTED", "FAILED", {"reason_code": "BATCH_FAILED"}
            )
            self._emit(
                "settlement_instruction.failed",
                "SettlementInstruction",
                row.instruction_id,
                {"reason_code": "BATCH_FAILED"},
            )
        self._audit_batch(batch_id, "DISPATCHED", "FAILED", {"reason_code": reason_code})
        self._emit(
            "settlement_batch.failed",
            "SettlementBatch",
            batch_id,
            {"rail": batch.rail, "fail_reason_code": reason_code, "retry_count": batch.retry_count},
        )
        return updated

    def manual_retry(
        self, batch_id: str, *, operator_approved: bool, now: datetime | None = None
    ) -> SettlementBatchRow:
        """``manual_retry_approved`` | ``max_retries_exceeded`` (FAILED batches)."""
        batch = self._require_batch(batch_id)
        now = now or self._now()
        max_retries = MAX_RETRIES.get(batch.rail, 3)
        if batch.retry_count >= max_retries:
            self._batch_transition(batch, "max_retries_exceeded", "CANCELLED")
            # spec/18 PT-C: unstage TCSA float BEFORE committing CANCELLED state
            # so that, if reversal posting fails, the batch remains in FAILED and
            # the operator can re-trigger manual_retry to re-enter this path.
            # Reversal is idempotent on the ledger (idem key prevents double-post)
            # so a partial-completion-then-crash retry is safe.
            self._post_batch_open_reversals(batch_id)
            updated = self._store.update_batch(
                batch_id,
                status="CANCELLED",
                cancelled_at=now,
                fail_reason_code="MAX_RETRIES_EXCEEDED",
            )
            self._audit_batch(batch_id, "FAILED", "CANCELLED", {"reason": "MAX_RETRIES_EXCEEDED"})
            self._emit(
                "settlement_batch.cancelled",
                "SettlementBatch",
                batch_id,
                {"rail": batch.rail, "reason": "MAX_RETRIES_EXCEEDED"},
            )
            self._alert("settlement-max-retries", {"batch_id": batch_id})
            self._ledger.trial_balance_assertion()
            return updated
        if not operator_approved:
            raise SettlementGuardError(f"batch {batch_id} retry requires operator approval")
        if batch.total_credit_minor > RETRY_TWO_EYES_THRESHOLD_MINOR:
            self._store.update_batch(batch_id, two_eyes_required=True)
        self._batch_transition(batch, "manual_retry_approved", "PENDING_DISPATCH")
        updated = self._store.update_batch(
            batch_id, status="PENDING_DISPATCH", retry_count=batch.retry_count + 1
        )
        self._audit_batch(
            batch_id, "FAILED", "PENDING_DISPATCH", {"retry_count": batch.retry_count + 1}
        )
        # PT-E fix: reset FAILED instructions to QUEUED so try_dispatch can
        # re-submit them.  Before this fix, _fail_batch() moved instructions
        # SUBMITTED→FAILED, but manual_retry left them in FAILED; try_dispatch
        # only queries status="QUEUED" (engine.py:645), so the retry dispatched
        # an empty batch — TCSA was never discharged and no money ever moved on
        # the retry path.  The FSM edge FAILED→QUEUED("batch_retry_reset") is
        # intentionally restricted to this call site.
        for row in self._store.list_instructions(batch_id=batch_id, status="FAILED"):
            self._instruction_transition(row, "batch_retry_reset", "QUEUED")
            self._store.update_instruction(
                row.instruction_id,
                status="QUEUED",
                failed_at=None,
                return_reason_code=None,
            )
            self._audit_instruction(
                row.instruction_id,
                "FAILED",
                "QUEUED",
                {"reason": "manual_retry_reset", "retry_count": batch.retry_count + 1},
            )
        self._emit(
            "settlement_batch.pending_dispatch",
            "SettlementBatch",
            batch_id,
            {"rail": batch.rail, "retry_count": batch.retry_count + 1},
        )
        return updated

    def cancel(
        self, batch_id: str, *, operator_approved: bool, now: datetime | None = None
    ) -> SettlementBatchRow:
        """``operator_cancel``: PENDING_DISPATCH | HELD -> CANCELLED."""
        if not operator_approved:
            raise SettlementGuardError(f"batch {batch_id} cancel requires operator approval")
        batch = self._require_batch(batch_id)
        now = now or self._now()
        # FSM-validate batch and all queued instructions up-front (read-only — no DB write yet).
        # Doing validation before any persistent write means a bad-state error surfaces cleanly
        # with nothing committed.
        self._batch_transition(batch, "operator_cancel", "CANCELLED")
        queued_instructions = self._store.list_instructions(batch_id=batch_id, status="QUEUED")
        for row in queued_instructions:
            self._instruction_transition(row, "batch_cancelled", "CANCELLED")
        # spec/18 PT-C crash-safety ordering (finding-1 fix):
        # Reversal JEs are the FIRST persistent write.  If reversal posting raises,
        # zero state has been committed — batch stays PENDING_DISPATCH, instructions
        # stay QUEUED — and the operator can re-trigger cancel() cleanly.
        # Reversal is idempotent (open_entry_id idem key prevents double-post), so
        # a partial-completion-then-crash retry is safe.
        # Cross-store atomicity (finding-2, pre-existing engine-wide gap): a process
        # crash AFTER this call but BEFORE update_batch() leaves TCSA reversed but
        # batch still dispatchable.  This is filed as Task #31 (settlement engine
        # atomic-cancel refactor) and requires a shared-transaction wrapper across
        # LedgerStore + SettlementStore — out of scope for the JE-lookup change.
        self._post_batch_open_reversals(batch_id)
        # Now that TCSA float is safely reversed, persist instruction state and batch state.
        for row in queued_instructions:
            self._store.update_instruction(row.instruction_id, status="CANCELLED")
            self._audit_instruction(row.instruction_id, "QUEUED", "CANCELLED", {})
        updated = self._store.update_batch(batch_id, status="CANCELLED", cancelled_at=now)
        self._audit_batch(batch_id, batch.status, "CANCELLED", {"reason": "operator_cancel"})
        self._emit(
            "settlement_batch.cancelled",
            "SettlementBatch",
            batch_id,
            {"rail": batch.rail, "reason": "operator_cancel"},
        )
        self._ledger.trial_balance_assertion()
        return updated

    # ------------------------------------------------------------------
    # FSM 2 individual-instruction triggers (gross rails / returns)
    # ------------------------------------------------------------------

    def on_rail_confirmed(
        self, instruction_id: str, rail_transaction_id: str, now: datetime | None = None
    ) -> SettlementInstructionRow:
        row = self._require_instruction(instruction_id)
        now = now or self._now()
        self._instruction_transition(row, "rail_confirmed", "SETTLED")
        updated = self._store.update_instruction(
            instruction_id,
            status="SETTLED",
            settled_at=now,
            rail_transaction_id=rail_transaction_id,
        )
        self._audit_instruction(instruction_id, "SUBMITTED", "SETTLED", {})
        self._emit(
            "settlement_instruction.settled",
            "SettlementInstruction",
            instruction_id,
            {
                "batch_id": row.batch_id,
                "merchant_id": row.merchant_id,
                "net_payout_minor": row.net_payout_minor,
                "rail_transaction_id": rail_transaction_id,
                "settled_at": now.isoformat(),
            },
        )
        return updated

    def on_rail_returned(
        self, instruction_id: str, return_reason_code: str, now: datetime | None = None
    ) -> SettlementInstructionRow:
        row = self._require_instruction(instruction_id)
        now = now or self._now()
        self._instruction_transition(row, "rail_returned", "RETURNED")
        updated = self._store.update_instruction(
            instruction_id,
            status="RETURNED",
            returned_at=now,
            return_reason_code=return_reason_code,
        )
        self._audit_instruction(
            instruction_id, "SUBMITTED", "RETURNED", {"return_reason_code": return_reason_code}
        )
        self._emit(
            "settlement_instruction.returned",
            "SettlementInstruction",
            instruction_id,
            {
                "return_reason_code": return_reason_code,
                "payment_intent_id": row.payment_intent_id,
            },
        )
        return updated

    def on_rail_rejected(
        self, instruction_id: str, error_code: str, now: datetime | None = None
    ) -> SettlementInstructionRow:
        row = self._require_instruction(instruction_id)
        now = now or self._now()
        self._instruction_transition(row, "rail_hard_rejected", "FAILED")
        updated = self._store.update_instruction(
            instruction_id, status="FAILED", failed_at=now, return_reason_code=error_code
        )
        self._audit_instruction(instruction_id, "SUBMITTED", "FAILED", {"reason_code": error_code})
        self._emit(
            "settlement_instruction.failed",
            "SettlementInstruction",
            instruction_id,
            {"reason_code": error_code},
        )
        return updated

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _outbox_payloads_semantically_equal(event_type: str, a: dict, b: dict) -> bool:
        """Compare payload fields that identify the event, ignoring replay-only
        timestamps such as ``settled_at`` / ``confirmed_at`` that differ only
        because a legacy event used a different ``occurred_at``.
        """
        if event_type == "settlement_instruction.settled":
            keys = ("batch_id", "merchant_id", "net_payout_minor", "rail_transaction_id")
        elif event_type == "settlement_batch.confirmed":
            keys = ("rail", "cycle_date", "sponsor_bank_ref")
        else:
            return a == b
        return all(a.get(k) == b.get(k) for k in keys)

    def _has_equivalent_audit_event(
        self, spec: AuditEventSpec, *, occurred_at: datetime | None = None
    ) -> bool:
        """Return True if the exact deterministic audit event exists, or if a
        legacy event of the same type/state/subject with an equivalent payload
        is already durable.
        """
        if self._ledger.audit_event_exists(spec, occurred_at=occurred_at):
            return True
        for ev in self._ledger.store.list_audit_events(
            subject_type=spec.subject_type, subject_id=spec.subject_id
        ):
            if ev.event_type != spec.event_type:
                continue
            if ev.from_state != (spec.from_state or ""):
                continue
            if ev.to_state != (spec.to_state or ""):
                continue
            try:
                stored_payload = json.loads(ev.payload_json)
            except Exception:  # pragma: no cover - malformed payload should not block repair
                continue
            if stored_payload == spec.payload:
                return True
        return False

    def _has_equivalent_outbox_event(
        self,
        event_type: str,
        subject_type: str,
        subject_id: str,
        payload: dict,
        occurred_at: datetime,
    ) -> bool:
        """Return True if the exact deterministic outbox event exists, or if a
        legacy event of the same type/subject with an equivalent semantic payload
        is already durable.  The legacy fallback ignores ``occurred_at`` and
        timestamp-only payload differences so that stale malformed events cannot
        suppress repair of the correct evidence.
        """
        if self._ledger.outbox_event_exists(
            event_type=event_type,
            subject_type=subject_type,
            subject_id=subject_id,
            payload=payload,
            occurred_at=occurred_at,
        ):
            return True
        for ev in self._ledger.store.list_outbox_by_subject(event_type, subject_id):
            try:
                stored_payload = json.loads(ev.payload_json).get("payload", {})
            except Exception:  # pragma: no cover - malformed payload should not block repair
                continue
            if self._outbox_payloads_semantically_equal(event_type, stored_payload, payload):
                return True
        return False

    def _batch_confirmed_events_exist(self, batch_id: str) -> bool:
        """Return True if both the audit and outbox CONFIRMED events for the
        batch are already durable.  The batch-level events are emitted after all
        instruction events, so their presence guarantees instruction events are
        present too.
        """
        batch = self._require_batch(batch_id)
        if batch.status != "CONFIRMED" or batch.confirmed_at is None:
            return False
        audit_spec = AuditEventSpec(
            event_type="SETTLEMENT_BATCH_STATE_TRANSITION",
            actor_id=self._producer,
            actor_type="SERVICE",
            subject_type="SettlementBatch",
            subject_id=batch_id,
            from_state="DISPATCHED",
            to_state="CONFIRMED",
            payload={"sponsor_bank_ref": batch.sponsor_bank_ref},
        )
        outbox_payload = {
            "rail": batch.rail,
            "cycle_date": batch.cycle_date.isoformat(),
            "confirmed_at": batch.confirmed_at.isoformat(),
            "sponsor_bank_ref": batch.sponsor_bank_ref,
        }
        return self._has_equivalent_audit_event(
            audit_spec, occurred_at=batch.confirmed_at
        ) and self._has_equivalent_outbox_event(
            "settlement_batch.confirmed",
            "SettlementBatch",
            batch_id,
            outbox_payload,
            occurred_at=batch.confirmed_at,
        )

    def _reconcile_settled_instruction_events(self, batch_id: str) -> bool:
        """Emit missing audit/outbox events for terminal SETTLED instructions.

        Used to repair the crash window where ``update_instruction`` succeeded
        but the process died before the audit/outbox events were written.  Each
        replayed event is timestamped with the instruction's own ``settled_at``
        so the derived event id is deterministic and a second reconciliation
        finds the existing event and skips it.
        """
        repaired = False
        for row in self._store.list_instructions(batch_id=batch_id, status="SETTLED"):
            if row.settled_at is None:
                continue
            audit_spec = AuditEventSpec(
                event_type="SETTLEMENT_INSTRUCTION_STATE_TRANSITION",
                actor_id=self._producer,
                actor_type="SERVICE",
                subject_type="SettlementInstruction",
                subject_id=row.instruction_id,
                from_state="SUBMITTED",
                to_state="SETTLED",
                payload={},
            )
            has_audit = self._has_equivalent_audit_event(
                audit_spec, occurred_at=row.settled_at
            )
            outbox_payload = {
                "batch_id": batch_id,
                "merchant_id": row.merchant_id,
                "net_payout_minor": row.net_payout_minor,
                "rail_transaction_id": row.rail_transaction_id,
                "settled_at": row.settled_at.isoformat(),
            }
            has_outbox = self._ledger.outbox_event_exists(
                event_type="settlement_instruction.settled",
                subject_type="SettlementInstruction",
                subject_id=row.instruction_id,
                payload=outbox_payload,
                occurred_at=row.settled_at,
            ) or self._has_equivalent_outbox_event(
                "settlement_instruction.settled",
                "SettlementInstruction",
                row.instruction_id,
                outbox_payload,
                occurred_at=row.settled_at,
            )
            if not has_audit:
                self._audit_instruction(
                    row.instruction_id,
                    "SUBMITTED",
                    "SETTLED",
                    {},
                    occurred_at=row.settled_at,
                )
                repaired = True
            if not has_outbox:
                self._emit(
                    "settlement_instruction.settled",
                    "SettlementInstruction",
                    row.instruction_id,
                    outbox_payload,
                    occurred_at=row.settled_at,
                )
                repaired = True
        return repaired

    def _reconcile_batch_confirmed_events(self, batch_id: str) -> bool:
        """Emit missing audit/outbox events for a CONFIRMED batch.

        Repairs the crash window where ``update_batch(status=CONFIRMED)``
        succeeded but the process died before the batch-level audit/outbox
        events were written.  Events are replayed at ``confirmed_at`` so the
        derived event ids are deterministic and repeated reconciliation is a
        no-op.
        """
        repaired = False
        batch = self._require_batch(batch_id)
        if batch.status != "CONFIRMED" or batch.confirmed_at is None:
            return False
        audit_spec = AuditEventSpec(
            event_type="SETTLEMENT_BATCH_STATE_TRANSITION",
            actor_id=self._producer,
            actor_type="SERVICE",
            subject_type="SettlementBatch",
            subject_id=batch_id,
            from_state="DISPATCHED",
            to_state="CONFIRMED",
            payload={"sponsor_bank_ref": batch.sponsor_bank_ref},
        )
        has_audit = self._has_equivalent_audit_event(
            audit_spec, occurred_at=batch.confirmed_at
        )
        outbox_payload = {
            "rail": batch.rail,
            "cycle_date": batch.cycle_date.isoformat(),
            "confirmed_at": batch.confirmed_at.isoformat(),
            "sponsor_bank_ref": batch.sponsor_bank_ref,
        }
        has_outbox = self._ledger.outbox_event_exists(
            event_type="settlement_batch.confirmed",
            subject_type="SettlementBatch",
            subject_id=batch_id,
            payload=outbox_payload,
            occurred_at=batch.confirmed_at,
        ) or self._has_equivalent_outbox_event(
            "settlement_batch.confirmed",
            "SettlementBatch",
            batch_id,
            outbox_payload,
            occurred_at=batch.confirmed_at,
        )
        if not has_audit:
            self._audit_batch(
                batch_id,
                "DISPATCHED",
                "CONFIRMED",
                {"sponsor_bank_ref": batch.sponsor_bank_ref},
                occurred_at=batch.confirmed_at,
            )
            repaired = True
        if not has_outbox:
            self._emit(
                "settlement_batch.confirmed",
                "SettlementBatch",
                batch_id,
                outbox_payload,
                occurred_at=batch.confirmed_at,
            )
            repaired = True
        return repaired

    def _require_batch(self, batch_id: str) -> SettlementBatchRow:
        batch = self._store.get_batch(batch_id)
        if batch is None:
            raise SettlementError(f"unknown batch {batch_id}")
        return batch

    def _require_instruction(self, instruction_id: str) -> SettlementInstructionRow:
        row = self._store.get_instruction(instruction_id)
        if row is None:
            raise SettlementError(f"unknown instruction {instruction_id}")
        return row

    def _now(self) -> datetime:
        dt = self._clock.now()
        if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
            raise SettlementError("clock returned a naive datetime (spec/00 §7)")
        return dt.astimezone(UTC)

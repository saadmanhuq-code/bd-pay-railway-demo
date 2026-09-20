"""NettingEngine — deterministic multilateral DNS netting (spec/19 PSO-2).

The binding algorithm is spec/19 §"The netting algorithm" (steps 1–7) plus
FSM 3 (NettingRun) and the additive SettlementWindow rows (FSM 4). Refusal
posture:

- ``PsoModeError`` under ``PLATFORM_MODE=PSP`` — raised, never caught.
- Netting has NO trigger API: ``compute`` fires from the window FSM
  (``cutoff_reached`` scheduler side-effect) and from the approved D-19-2
  unwind. Any other window state refuses.
- Integrity failure NEVER advances the window: the run is recorded
  ``FAILED_INTEGRITY``, the window stays ``CUTOFF_REACHED``, and the error
  propagates (a late window beats a false one).

Determinism contract (test-pinned): seeded-order independence, idempotent
recompute via the content-addressed ``nrun`` id, byte-identical result files,
and a hashed result document containing ledger facts only (D-19-7 — every
clock read lands in row columns).

Dispatch is port-shaped: the engine emits ``PsoInstructionDispatch`` plans in
exact field parity with the frozen ``connectors.sdk.PaymentInstruction``
contract; the composition root maps them 1:1 and submits through
``ConnectorRunnerPort`` OUTSIDE any database transaction. No connector module
is imported here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from bdpay.ledger.errors import PsoModeError
from bdpay.ledger.netting.config import NettingConfig
from bdpay.ledger.netting.errors import (
    NettingError,
    NettingGuardError,
    NettingIntegrityError,
    NettingInvalidTransitionError,
    UnwindRefusedError,
)
from bdpay.ledger.netting.facts import LedgerFactsPort
from bdpay.ledger.netting.objectstore import ObjectStore
from bdpay.ledger.netting.positions import (
    NettingPositionStore,
    ParticipantDirectory,
    PositionPostingService,
)
from bdpay.ledger.netting.store import NettingStore
from bdpay.ledger.netting.types import (
    NETTING_RUN_TRANSITIONS,
    PSO_INSTRUCTION_TRANSITIONS,
    NettingObligationRow,
    NettingRunRow,
    ParticipantPosition,
    PsoNettingInstructionRow,
    build_result_document,
    compute_obligations,
    instruction_connector_ref,
    instruction_id_for,
    netting_inputs_hash,
    netting_run_id_for,
    obligation_id_for,
    result_pointer_for,
)
from bdpay.ledger.position import PositionService, SettlementWindowRow
from bdpay.ledger.service import LedgerService
from bdpay.ledger.settlement.types import HIGH_VALUE_THRESHOLD_MINOR
from bdpay.ledger.types import AuditEventSpec, JournalEntrySpec, PostingSpec
from bdpay.platform.canonical import canonical_json, sha256_canonical
from bdpay.platform.clock import Clock
from bdpay.platform.config import Settings
from bdpay.platform.ids import make_id

__all__ = ["NettingEngine", "PsoInstructionDispatch"]

PRODUCER = "settlement-engine@1"
RTGS_METHOD = "RTGS_CREDIT"


@dataclass(frozen=True, slots=True)
class PsoInstructionDispatch:
    """Field-parity dispatch plan for one PSO netting leg.

    Maps 1:1 onto the frozen ``connectors.sdk.PaymentInstruction`` constructor
    (pinned by test) — the composition root builds the SDK object and submits
    via ``ConnectorRunnerPort.submit`` outside any DB transaction.
    """

    instruction_id: str
    connector_ref: str
    amount_minor: int
    currency: str
    method: str
    sender_ref: str
    beneficiary_ref: str
    rail: str
    instruction_at: str  # RFC3339 UTC
    metadata: dict[str, str]

    def payment_instruction_kwargs(self) -> dict:
        """Exact kwargs for ``connectors.sdk.PaymentInstruction(...)``."""
        return {
            "instruction_id": self.instruction_id,
            "connector_ref": self.connector_ref,
            "amount": {"amount_minor": self.amount_minor, "currency": self.currency},
            "method": self.method,
            "sender_ref": self.sender_ref,
            "beneficiary_ref": self.beneficiary_ref,
            "rail": self.rail,
            "instruction_at": self.instruction_at,
            "metadata": dict(self.metadata),
        }


class NettingEngine:
    """Per-window multilateral netting across N participants (integer paisa)."""

    def __init__(
        self,
        *,
        ledger: LedgerService,
        netting_store: NettingStore,
        position_store: NettingPositionStore,
        position_service: PositionService,
        posting_service: PositionPostingService,
        facts: LedgerFactsPort,
        directory: ParticipantDirectory,
        object_store: ObjectStore,
        clock: Clock,
        settings: Settings,
        config: NettingConfig,
        rtgs_clearing_account_id: str,
        netting_suspense_account_id: str,
        producer: str = PRODUCER,
    ) -> None:
        self._ledger = ledger
        self._store = netting_store
        self._positions = position_store
        self._position_service = position_service
        self._poster = posting_service
        self._facts = facts
        self._directory = directory
        self._objects = object_store
        self._clock = clock
        self._settings = settings
        self._config = config
        self._clearing_account = rtgs_clearing_account_id
        self._suspense_account = netting_suspense_account_id
        self._producer = producer

    # ------------------------------------------------------------------
    # Mode gate (binding, spec/19 §Scope — raised, never caught)
    # ------------------------------------------------------------------

    def _require_pso(self) -> None:
        if self._settings.platform_mode != "PSO":
            raise PsoModeError(
                f"PSO-only operation refused: PLATFORM_MODE={self._settings.platform_mode!r}"
            )

    # ------------------------------------------------------------------
    # Step 1–6: compute
    # ------------------------------------------------------------------

    def compute(
        self,
        settlement_window_id: str,
        *,
        unwind_of: tuple[str, str] | None = None,
    ) -> NettingRunRow:
        """Deterministic multilateral net computation for one window.

        Returns the live ``PASSED`` run (idempotent re-fire returns the
        existing one). Raises ``NettingIntegrityError`` on any step-2 failure
        — the window stays ``CUTOFF_REACHED``.
        """
        self._require_pso()
        window = self._require_window(settlement_window_id)

        existing = self._store.live_passed_run(settlement_window_id)
        if existing is not None:
            return existing  # content-addressed idempotency (double-fire no-op)
        if window.status != "CUTOFF_REACHED":
            raise NettingGuardError(
                f"netting computes only at CUTOFF_REACHED; window "
                f"{settlement_window_id} is {window.status} (refusal-first)"
            )
        if window.cutoff_at is None:
            raise NettingGuardError(
                f"window {settlement_window_id} carries no cutoff_at instant; "
                "the instruction release time is undefined (refused)"
            )

        # 1. READ — derived (ledger facts only) + maintained (mirror) + tip.
        derivation = self._facts.derive_window(settlement_window_id)
        derived = list(derivation.positions)
        maintained = [
            ParticipantPosition(
                participant_id=row.participant_id,
                net_position_minor=row.net_position_minor,
            )
            for row in self._positions.list_window_positions(settlement_window_id)
        ]

        inputs_hash = netting_inputs_hash(derived)
        netting_run_id = netting_run_id_for(settlement_window_id, inputs_hash)
        prior = self._store.get_run(netting_run_id)
        if prior is not None:
            if prior.status == "PASSED":
                return prior
            if prior.status == "FAILED_INTEGRITY":
                # Same inputs already failed: recomputing cannot change the
                # verdict (content-addressed id). Fail closed with the record.
                raise NettingIntegrityError(netting_run_id, prior.failure_detail or {})
            raise NettingError(
                f"netting run {netting_run_id} is {prior.status}; a mid-compute "
                "record requires operator inspection before re-fire (fail closed)"
            )

        now = self._now()
        run = NettingRunRow(
            netting_run_id=netting_run_id,
            settlement_window_id=settlement_window_id,
            status="COMPUTING",
            participant_count=len(derived),
            inputs_hash=inputs_hash,
            result_hash=None,
            result_pointer=None,
            total_gross_minor=None,
            total_net_pay_minor=None,
            excluded_participant_id=unwind_of[1] if unwind_of else None,
            supersedes_netting_run_id=unwind_of[0] if unwind_of else None,
            failure_detail=None,
            computed_at=None,
            created_at=now,
        )
        self._store.insert_run(run)

        # 2. INTEGRITY — refusal-first; any failure seals FAILED_INTEGRITY.
        failure = self._integrity_failure(derived, maintained)
        if failure is not None:
            self._run_transition(run, "integrity_failure", "FAILED_INTEGRITY")
            self._store.update_run(
                netting_run_id,
                status="FAILED_INTEGRITY",
                failure_detail=failure,
                computed_at=self._now(),
            )
            self._audit(
                event_type="NETTING_INTEGRITY_FAILURE",
                subject_type="NettingRun",
                subject_id=netting_run_id,
                from_state="COMPUTING",
                to_state="FAILED_INTEGRITY",
                payload={"settlement_window_id": settlement_window_id, **failure},
            )
            self._emit(
                "netting_run.failed",
                "NettingRun",
                netting_run_id,
                {
                    "netting_run_id": netting_run_id,
                    "settlement_window_id": settlement_window_id,
                    "failure_class": str(failure.get("failure_class", "integrity")),
                },
            )
            # Window stays CUTOFF_REACHED — never advance on a broken derivation.
            raise NettingIntegrityError(netting_run_id, failure)

        # 3–4. OBLIGATIONS + HASHES + the canonical result document.
        obligations = compute_obligations(derived)
        total_net_pay = sum(amount for _, direction, amount in obligations if direction == "PAY")
        tokens = self._account_tokens(derived)
        document = build_result_document(
            settlement_window_id=settlement_window_id,
            dns_session=window.dns_session,
            window_date=window.window_date,
            chain_index_at_compute=derivation.chain_tip.chain_index,
            chain_hash_at_compute=derivation.chain_tip.chain_hash,
            positions=derived,
            obligations=obligations,
            total_gross_minor=derivation.total_gross_minor,
            total_net_pay_minor=total_net_pay,
            instruction_rail=self._config.instruction_rail,
            account_token_for=tokens,
        )
        result_bytes = canonical_json(document)
        result_hash = sha256_canonical(document)
        result_pointer = result_pointer_for(settlement_window_id, result_hash)

        # 6. WRITE — obligations + D-19-4 instructions + seal + ledger entry
        #    + audit + outbox + window transition.
        release_at = window.cutoff_at
        for participant_id, direction, amount in obligations:
            instruction_id: str | None = None
            if direction != "FLAT":
                instruction_id = instruction_id_for(netting_run_id, participant_id)
                self._store.insert_instruction(
                    PsoNettingInstructionRow(
                        instruction_id=instruction_id,
                        netting_run_id=netting_run_id,
                        participant_id=participant_id,
                        direction="DEBIT" if direction == "PAY" else "CREDIT",
                        amount_minor=amount,
                        currency="BDT",
                        rail=self._config.instruction_rail,
                        counterparty_account_token=tokens[participant_id],
                        high_value=amount >= HIGH_VALUE_THRESHOLD_MINOR,
                        earliest_release_at=release_at,
                        latest_release_at=release_at,
                        status="QUEUED",
                        connector_ref=instruction_connector_ref(
                            netting_run_id, participant_id
                        ),
                        rail_transaction_id=None,
                        created_at=now,
                        submitted_at=None,
                        settled_at=None,
                        failed_at=None,
                        cancelled_at=None,
                        produced_by=self._producer,
                    )
                )
            self._store.insert_obligation(
                NettingObligationRow(
                    obligation_id=obligation_id_for(netting_run_id, participant_id),
                    netting_run_id=netting_run_id,
                    participant_id=participant_id,
                    net_position_minor=next(
                        p.net_position_minor for p in derived if p.participant_id == participant_id
                    ),
                    direction=direction,
                    obligation_minor=amount,
                    settlement_instruction_id=instruction_id,
                )
            )

        self._objects.put(result_pointer, result_bytes)
        self._run_transition(run, "result_sealed", "PASSED")
        sealed = self._store.update_run(
            netting_run_id,
            status="PASSED",
            result_hash=result_hash,
            result_pointer=result_pointer,
            total_gross_minor=derivation.total_gross_minor,
            total_net_pay_minor=total_net_pay,
            computed_at=self._now(),
        )

        if total_net_pay > 0:
            # The net-file ledger entry (spec/05 netting_computed side-effect).
            # Clearing memo pair — position accounts are NEVER posted here
            # (errata N19-5): DR RTGS clearing / CR netting suspense.
            self._ledger.post_journal_entry(
                JournalEntrySpec(
                    reference_id=settlement_window_id,
                    reference_type="SETTLEMENT",
                    entry_type="settlement_batch_close",
                    description=f"PSO netting file sealed for {settlement_window_id}",
                    produced_by=self._producer,
                    idempotency_key=make_id(
                        "idem",
                        {
                            "netting_run_id": netting_run_id,
                            "entry_type": "settlement_batch_close",
                        },
                    ),
                    postings=(
                        PostingSpec(
                            account_id=self._clearing_account,
                            side="DEBIT",
                            amount_minor=total_net_pay,
                        ),
                        PostingSpec(
                            account_id=self._suspense_account,
                            side="CREDIT",
                            amount_minor=total_net_pay,
                        ),
                    ),
                )
            )

        self._audit(
            event_type="NETTING_COMPLETED",
            subject_type="NettingRun",
            subject_id=netting_run_id,
            from_state="COMPUTING",
            to_state="PASSED",
            payload={
                "settlement_window_id": settlement_window_id,
                "participant_count": len(derived),
                "total_net_pay_minor": total_net_pay,
                "result_hash": result_hash,
            },
        )
        self._emit(
            "netting_run.completed",
            "NettingRun",
            netting_run_id,
            {
                "netting_run_id": netting_run_id,
                "settlement_window_id": settlement_window_id,
                "participant_count": len(derived),
                "total_net_pay_minor": total_net_pay,
                "result_hash": result_hash,
            },
        )
        self._emit(
            "settlement_batch.closed",
            "SettlementWindow",
            settlement_window_id,
            {
                "settlement_window_id": settlement_window_id,
                "netting_run_id": netting_run_id,
                "total_net_pay_minor": total_net_pay,
            },
        )

        # Window FSM: tightened guard holds by construction (run is PASSED).
        self._window_transition(
            settlement_window_id,
            "netting_computed",
            "NETTING_CALCULATED",
            netting_completed_at=self._now(),
            net_settlement_amount_minor=total_net_pay,
        )
        return sealed

    # ------------------------------------------------------------------
    # Integrity (step 2)
    # ------------------------------------------------------------------

    def _integrity_failure(
        self,
        derived: list[ParticipantPosition],
        maintained: list[ParticipantPosition],
    ) -> dict | None:
        derived_map = {p.participant_id: p.net_position_minor for p in derived}
        maintained_map = {p.participant_id: p.net_position_minor for p in maintained}
        if set(derived_map) != set(maintained_map):
            return {
                "failure_class": "participant_set_mismatch",
                "derived_only": sorted(set(derived_map) - set(maintained_map)),
                "maintained_only": sorted(set(maintained_map) - set(derived_map)),
            }
        for participant_id in sorted(derived_map):
            if derived_map[participant_id] != maintained_map[participant_id]:
                return {
                    "failure_class": "derived_maintained_mismatch",
                    "participant_id": participant_id,
                    "derived_minor": derived_map[participant_id],
                    "maintained_minor": maintained_map[participant_id],
                    "delta_minor": derived_map[participant_id]
                    - maintained_map[participant_id],
                }
        zero_sum_delta = sum(derived_map.values())
        if zero_sum_delta != 0:
            return {
                "failure_class": "zero_sum_violation",
                "zero_sum_delta_minor": zero_sum_delta,
            }
        for participant_id in sorted(derived_map):
            if self._positions.active_cap(participant_id) is None:
                return {
                    "failure_class": "missing_active_ndc",
                    "participant_id": participant_id,
                }
        return None

    def _account_tokens(self, derived: list[ParticipantPosition]) -> dict[str, str]:
        tokens: dict[str, str] = {}
        for position in derived:
            token = self._directory.settlement_account_token(position.participant_id)
            if not token:
                raise NettingGuardError(
                    f"participant {position.participant_id} has no tokenized "
                    "settlement-account reference (onboarding incomplete; refused)"
                )
            tokens[position.participant_id] = token
        return tokens

    # ------------------------------------------------------------------
    # Window FSM 4 additive rows
    # ------------------------------------------------------------------

    def mark_participant_suspended(
        self,
        settlement_window_id: str,
        participant_id: str,
        *,
        actor_id: str,
        actor_type: str = "SERVICE",
    ) -> SettlementWindowRow:
        """The NEW spec/19 side-effect row: ``participant_suspended`` mid-window.

        The window state does NOT change; postings already accepted REMAIN
        (accepted-is-final mid-window) and the directory/trigger guard refuses
        all FURTHER postings for the participant.
        """
        self._require_pso()
        updated = self._window_transition(
            settlement_window_id,
            "participant_suspended",
            "ACCUMULATING_POSITIONS",
            audit_event_type="WINDOW_PARTICIPANT_SUSPENDED",
            actor_id=actor_id,
            actor_type=actor_type,
            payload={"participant_id": participant_id},
        )
        self._emit(
            "settlement_window.participant_suspended",
            "SettlementWindow",
            settlement_window_id,
            {
                "settlement_window_id": settlement_window_id,
                "participant_id": participant_id,
            },
        )
        return updated

    def confirm_settlement(self, settlement_window_id: str) -> SettlementWindowRow:
        """Tightened guard: ALL ``PSO_NETTING`` legs ``SETTLED`` (N legs, not 1)."""
        self._require_pso()
        run = self._require_live_run(settlement_window_id)
        legs = self._store.list_instructions(run.netting_run_id)
        unsettled = [leg.instruction_id for leg in legs if leg.status != "SETTLED"]
        if unsettled:
            raise NettingGuardError(
                f"settlement_confirmed refused: {len(unsettled)} leg(s) not SETTLED "
                f"({', '.join(sorted(unsettled))})"
            )
        updated = self._window_transition(
            settlement_window_id,
            "settlement_confirmed",
            "SETTLEMENT_CONFIRMED",
            confirmed_at=self._now(),
        )
        self._emit(
            "settlement.confirmed",
            "SettlementWindow",
            settlement_window_id,
            {
                "settlement_window_id": settlement_window_id,
                "netting_run_id": run.netting_run_id,
                "leg_count": len(legs),
            },
        )
        return updated

    # ------------------------------------------------------------------
    # Leg lifecycle (spec/04 FSM 2, rail-facing) + dispatch plans
    # ------------------------------------------------------------------

    def dispatch_plans(self, settlement_window_id: str) -> list[PsoInstructionDispatch]:
        """Field-parity dispatch plans for the live run's QUEUED legs.

        TCSA gate first (spec/19 step 7): plans exist only once the window is
        ``SETTLEMENT_INSTRUCTED`` (the spec/05 ``tcsa_ok`` row fired).
        """
        self._require_pso()
        window = self._require_window(settlement_window_id)
        if window.status != "SETTLEMENT_INSTRUCTED":
            raise NettingGuardError(
                f"dispatch plans exist only at SETTLEMENT_INSTRUCTED; window "
                f"{settlement_window_id} is {window.status} (TCSA gate first)"
            )
        run = self._require_live_run(settlement_window_id)
        plans: list[PsoInstructionDispatch] = []
        for leg in self._store.list_instructions(run.netting_run_id):
            if leg.status != "QUEUED":
                continue
            # PAY legs (DEBIT) draw FROM the participant's settlement account;
            # RECEIVE legs (CREDIT) pay TO it. The opposite side of every leg
            # is the PSO net-settlement clearing reference.
            if leg.direction == "DEBIT":
                sender_ref = leg.counterparty_account_token
                beneficiary_ref = f"pso-clearing:{self._clearing_account}"
            else:
                sender_ref = f"pso-clearing:{self._clearing_account}"
                beneficiary_ref = leg.counterparty_account_token
            plans.append(
                PsoInstructionDispatch(
                    instruction_id=leg.instruction_id,
                    connector_ref=leg.connector_ref,
                    amount_minor=leg.amount_minor,
                    currency=leg.currency,
                    method=RTGS_METHOD,
                    sender_ref=sender_ref,
                    beneficiary_ref=beneficiary_ref,
                    rail=leg.rail,
                    instruction_at=_rfc3339(self._now()),
                    metadata={
                        "netting_run_id": leg.netting_run_id,
                        "settlement_window_id": settlement_window_id,
                        "participant_id": leg.participant_id,
                    },
                )
            )
        return plans

    def mark_leg_submitted(self, instruction_id: str) -> PsoNettingInstructionRow:
        self._require_pso()
        leg = self._require_leg(instruction_id)
        self._leg_transition(leg, "batch_dispatched", "SUBMITTED")
        updated = self._store.update_instruction(
            instruction_id, status="SUBMITTED", submitted_at=self._now()
        )
        self._audit_leg(leg, "SUBMITTED")
        return updated

    def record_leg_settled(
        self, instruction_id: str, rail_transaction_id: str
    ) -> PsoNettingInstructionRow:
        self._require_pso()
        leg = self._require_leg(instruction_id)
        self._leg_transition(leg, "rail_confirmed", "SETTLED")
        updated = self._store.update_instruction(
            instruction_id,
            status="SETTLED",
            rail_transaction_id=rail_transaction_id,
            settled_at=self._now(),
        )
        self._audit_leg(leg, "SETTLED", {"rail_transaction_id": rail_transaction_id})
        return updated

    def record_leg_rejected(
        self, instruction_id: str, *, error_code: str
    ) -> PsoNettingInstructionRow:
        """ANY leg ``REJECTED`` fails the whole window (tightened spec/19 row)."""
        self._require_pso()
        leg = self._require_leg(instruction_id)
        self._leg_transition(leg, "rail_hard_rejected", "FAILED")
        updated = self._store.update_instruction(
            instruction_id, status="FAILED", failed_at=self._now()
        )
        self._audit_leg(leg, "FAILED", {"error_code": error_code})
        run = self._store.get_run(leg.netting_run_id)
        window_id = run.settlement_window_id if run else None
        if window_id is not None:
            window = self._require_window(window_id)
            if window.status == "SETTLEMENT_INSTRUCTED":
                self._window_transition(
                    window_id,
                    "settlement_rejected",
                    "SETTLEMENT_FAILED",
                    block_reason=(
                        f"PSO_NETTING leg rejected for participant {leg.participant_id}"
                    ),
                    payload={
                        "participant_id": leg.participant_id,
                        "instruction_id": instruction_id,
                        "error_code": error_code,
                    },
                )
                self._emit(
                    "settlement.failed",
                    "SettlementWindow",
                    window_id,
                    {
                        "settlement_window_id": window_id,
                        "participant_id": leg.participant_id,
                        "instruction_id": instruction_id,
                        "error_code": error_code,
                    },
                )
        return updated

    # ------------------------------------------------------------------
    # D-19-2: UNWIND_AND_RECOMPUTE
    # ------------------------------------------------------------------

    def unwind(
        self,
        settlement_window_id: str,
        excluded_participant_id: str,
        *,
        approval_request_id: str,
        operator_id: str,
        reason: str,
    ) -> NettingRunRow:
        """Approved unwind: exclude the failed net debtor, re-net the survivors.

        Guards (all refusal-first): policy is ``UNWIND_AND_RECOMPUTE``; the
        window is ``SETTLEMENT_FAILED``; the named participant is the failed
        net debtor of the latest run; unwind rounds < ``PSO_MAX_UNWIND_ROUNDS``;
        the two-eyes ``ApprovalRequest`` id and the acting operator are
        recorded in the audit trail. Excluded transactions are reversed by
        offsetting entries — never deleted.
        """
        self._require_pso()
        if not approval_request_id:
            raise UnwindRefusedError("unwind requires an approved ApprovalRequest id")
        if not operator_id:
            raise UnwindRefusedError("unwind requires the acting operator id")
        if not reason or len(reason) > 1000:
            raise UnwindRefusedError("unwind reason is required (max 1000 chars)")

        window = self._require_window(settlement_window_id)
        if window.status != "SETTLEMENT_FAILED":
            raise UnwindRefusedError(
                f"unwind requires SETTLEMENT_FAILED; window is {window.status}"
            )
        run = self._require_live_run(settlement_window_id)
        obligation = next(
            (
                o
                for o in self._store.list_obligations(run.netting_run_id)
                if o.participant_id == excluded_participant_id
            ),
            None,
        )
        if obligation is None or obligation.direction != "PAY":
            raise UnwindRefusedError(
                f"{excluded_participant_id} is not a net debtor of run "
                f"{run.netting_run_id} (refused)"
            )
        failed_leg = next(
            (
                leg
                for leg in self._store.list_instructions(run.netting_run_id)
                if leg.participant_id == excluded_participant_id
                and leg.status in ("FAILED", "RETURNED")
            ),
            None,
        )
        if failed_leg is None:
            raise UnwindRefusedError(
                f"{excluded_participant_id}'s settlement leg has not failed (refused)"
            )
        rounds = self._store.count_unwind_runs(settlement_window_id)
        if rounds >= self._config.max_unwind_rounds:
            raise UnwindRefusedError(
                f"PSO_MAX_UNWIND_ROUNDS={self._config.max_unwind_rounds} exhausted "
                f"for window {settlement_window_id}; window stays SETTLEMENT_FAILED "
                "and the principal is paged"
            )

        # 1) Window FSM: SETTLEMENT_FAILED --[unwind_and_recompute]--> CUTOFF_REACHED.
        self._window_transition(
            settlement_window_id,
            "unwind_and_recompute",
            "CUTOFF_REACHED",
            audit_event_type="WINDOW_UNWOUND",
            actor_id=operator_id,
            actor_type="OPERATOR",
            payload={
                "excluded_participant_id": excluded_participant_id,
                "approval_request_id": approval_request_id,
                "reason": reason,
                "superseded_netting_run_id": run.netting_run_id,
            },
        )

        # 2) Supersede the old run; CANCEL its non-terminal instructions.
        self._run_transition(run, "superseded_by_unwind", "SUPERSEDED")
        self._store.update_run(run.netting_run_id, status="SUPERSEDED")
        for leg in self._store.list_instructions(run.netting_run_id):
            if leg.status in ("QUEUED", "SUBMITTED"):
                self._leg_transition(leg, "batch_cancelled", "CANCELLED")
                self._store.update_instruction(
                    leg.instruction_id, status="CANCELLED", cancelled_at=self._now()
                )
                self._audit_leg(leg, "CANCELLED", {"reason": "superseded_by_unwind"})

        # 3) Reverse the excluded participant's window transactions
        #    (offsetting entries through the attributed posting path).
        derivation = self._facts.derive_window(settlement_window_id)
        for fact in derivation.entries:
            if fact.entry_type != "position_updated":
                continue
            if excluded_participant_id not in fact.participant_by_posting:
                continue
            self._poster.post_unwind_reversal(
                settlement_window_id=settlement_window_id,
                fact=fact,
                excluded_participant_id=excluded_participant_id,
            )

        # 4) Re-net deterministically over the survivors.
        new_run = self.compute(
            settlement_window_id,
            unwind_of=(run.netting_run_id, excluded_participant_id),
        )
        self._emit(
            "settlement_window.unwound",
            "SettlementWindow",
            settlement_window_id,
            {
                "settlement_window_id": settlement_window_id,
                "excluded_participant_id": excluded_participant_id,
                "old_netting_run_id": run.netting_run_id,
                "new_netting_run_id": new_run.netting_run_id,
            },
        )
        return new_run

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def get_window_netting_run(self, settlement_window_id: str) -> NettingRunRow | None:
        """The window's live PASSED run (errata N19-7 association)."""
        return self._store.live_passed_run(settlement_window_id)

    def get_result_file(self, netting_run_id: str) -> bytes:
        """The canonical netting file — byte-identical on every read."""
        run = self._store.get_run(netting_run_id)
        if run is None or run.result_pointer is None:
            raise NettingError(f"netting run {netting_run_id} has no sealed result file")
        return self._objects.get(run.result_pointer)

    # ------------------------------------------------------------------
    # FSM guards + side-effect helpers
    # ------------------------------------------------------------------

    def _run_transition(self, row: NettingRunRow, trigger: str, to_state: str) -> None:
        allowed = NETTING_RUN_TRANSITIONS.get((row.status, trigger))
        if allowed is None or to_state not in allowed:
            raise NettingInvalidTransitionError(
                f"netting run {row.netting_run_id}: {row.status} --[{trigger}]--> "
                f"{to_state} is not in the spec/19 FSM 3 table (denied)"
            )

    def _leg_transition(
        self, leg: PsoNettingInstructionRow, trigger: str, to_state: str
    ) -> None:
        allowed = PSO_INSTRUCTION_TRANSITIONS.get((leg.status, trigger))
        if allowed is None or to_state not in allowed:
            raise NettingInvalidTransitionError(
                f"instruction {leg.instruction_id}: {leg.status} --[{trigger}]--> "
                f"{to_state} is not in the PSO leg table (denied)"
            )

    def _window_transition(
        self,
        settlement_window_id: str,
        trigger: str,
        to_state: str,
        *,
        audit_event_type: str = "SETTLEMENT_WINDOW_STATE_TRANSITION",
        actor_id: str | None = None,
        actor_type: str = "SERVICE",
        payload: dict | None = None,
        **changes: object,
    ) -> SettlementWindowRow:
        window = self._require_window(settlement_window_id)
        from_state = window.status
        updated = self._position_service.transition_window(
            settlement_window_id, trigger, to_state, **changes
        )
        self._audit(
            event_type=audit_event_type,
            subject_type="SettlementWindow",
            subject_id=settlement_window_id,
            from_state=from_state,
            to_state=to_state,
            payload={"trigger": trigger, **(payload or {})},
            actor_id=actor_id,
            actor_type=actor_type,
        )
        return updated

    def _audit_leg(
        self, leg: PsoNettingInstructionRow, to_state: str, payload: dict | None = None
    ) -> None:
        self._audit(
            event_type="SETTLEMENT_INSTRUCTION_STATE_TRANSITION",
            subject_type="SettlementInstruction",
            subject_id=leg.instruction_id,
            from_state=leg.status,
            to_state=to_state,
            payload={
                "instruction_kind": "PSO_NETTING",
                "participant_id": leg.participant_id,
                **(payload or {}),
            },
        )

    def _audit(
        self,
        *,
        event_type: str,
        subject_type: str,
        subject_id: str,
        from_state: str | None,
        to_state: str | None,
        payload: dict,
        actor_id: str | None = None,
        actor_type: str = "SERVICE",
    ) -> None:
        self._ledger.write_audit_event(
            AuditEventSpec(
                event_type=event_type,
                actor_id=actor_id or self._producer,
                actor_type=actor_type,
                subject_type=subject_type,
                subject_id=subject_id,
                from_state=from_state,
                to_state=to_state,
                payload=payload,
            )
        )

    def _emit(self, event_type: str, subject_type: str, subject_id: str, payload: dict) -> None:
        self._ledger.emit_event(
            event_type=event_type,
            subject_type=subject_type,
            subject_id=subject_id,
            payload=payload,
            topic="settlement.events",
            producer=self._producer,
        )

    def _require_window(self, settlement_window_id: str) -> SettlementWindowRow:
        window = self._positions.get_window(settlement_window_id)
        if window is None:
            raise NettingError(f"unknown settlement window {settlement_window_id}")
        return window

    def _require_live_run(self, settlement_window_id: str) -> NettingRunRow:
        run = self._store.live_passed_run(settlement_window_id)
        if run is None:
            raise NettingGuardError(
                f"window {settlement_window_id} has no live PASSED netting run"
            )
        return run

    def _require_leg(self, instruction_id: str) -> PsoNettingInstructionRow:
        leg = self._store.get_instruction(instruction_id)
        if leg is None:
            raise NettingError(f"unknown PSO netting instruction {instruction_id}")
        return leg

    def _now(self) -> datetime:
        dt = self._clock.now()
        if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
            raise NettingError("clock returned a naive datetime (spec/00 §7)")
        return dt.astimezone(UTC)


def _rfc3339(dt: datetime) -> str:
    dt = dt.astimezone(UTC)
    return (
        f"{dt.year:04d}-{dt.month:02d}-{dt.day:02d}"
        f"T{dt.hour:02d}:{dt.minute:02d}:{dt.second:02d}"
        f".{dt.microsecond // 1000:03d}Z"
    )

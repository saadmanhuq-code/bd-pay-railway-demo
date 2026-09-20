"""ReconciliationEngine (spec/04) — three-way recon, match cascade, exceptions.

Three-way: the internal ledger (settlement instructions written against
journal entries) vs the rail settlement file (normalized lines) vs rail
confirmations (rail_transaction_id echoes). The engine is read-only against
the immutable ledger; the ONLY ledger writes it performs are operator-resolved
exception adjustments through ``LedgerService.post_journal_entry`` under the
two-eyes threshold rule.

Match cascade (binding, spec/04):
  Level 1 — exact rail_transaction_id against settlement_instructions.
  Level 2 — (amount, rail, cycle-date ±2 days) fuzzy; ambiguity escalates.
  Level 3 — TIMING_GAP inside the T+2 business-day window, else UNMATCHED.

Drift-classification report shape ported from
``dse-profit-engine/src/dse_engine/pod/reconciler.py`` (ADAPT): one counter
per classification, errors listed not raised, idempotent re-runs (records are
content-addressed on the raw line hash).

spec/04's suspense journal pattern names entry types outside the spec/03
canonical list; they map to the canonical vocabulary (errata LA-L2):
``suspense_entry`` (unknown credit parked) / ``suspense_cleared`` (write-off
out of suspense).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from bdpay.ledger.calendar_ext import add_working_days, business_days_between
from bdpay.ledger.errors import (
    ReconciliationError,
    ReconInvalidTransitionError,
    SettlementGuardError,
)
from bdpay.ledger.reconciliation.store import ReconStore
from bdpay.ledger.reconciliation.types import (
    AML_ESCALATION_DELTA_MINOR,
    EXCEPTION_TRANSITIONS,
    PRODUCER,
    REASON_CODES,
    RECON_ADJUSTMENT_APPROVAL_THRESHOLD_MINOR,
    RECORD_TRANSITIONS,
    RESOLUTION_ACTIONS,
    RESOLUTION_TIERS,
    NormalizedReconLine,
    ReconcileReport,
    ReconciliationExceptionRow,
    ReconciliationRecordRow,
)
from bdpay.ledger.service import LedgerService
from bdpay.ledger.settlement.store import SettlementStore
from bdpay.ledger.types import AuditEventSpec, JournalEntrySpec, PostingSpec
from bdpay.platform.bangla import AmountParseError, parse_bdt_amount
from bdpay.platform.canonical import sha256_canonical
from bdpay.platform.clock import Clock
from bdpay.platform.ids import make_id
from bdpay.platform.observability import MetricsRegistry
from bdpay.platform.scheduler import DHAKA_TZ, BangladeshBankCalendar

__all__ = ["ReconciliationEngine", "normalize_raw_line"]

#: Ledger-affecting resolution actions and their canonical entry mapping (LA-L2).
_LEDGER_ACTIONS = frozenset({"WRITE_OFF", "SUSPENSE_TRANSFER"})


def normalize_raw_line(rail: str, raw_line: dict) -> NormalizedReconLine:
    """Parse one raw settlement-file line into a NormalizedReconLine.

    Amount fields may arrive in Bengali numerals / Taka-sign / lakh-grouped
    forms; ``parse_bdt_amount`` normalizes before any arithmetic. A line that
    cannot be parsed comes back with ``parse_failed=True`` (it is NEVER
    dropped — it dead-letters downstream; fails closed).
    """
    raw_line_hash = sha256_canonical(raw_line)
    try:
        amount_raw = raw_line.get("amount")
        if amount_raw is None:
            raise AmountParseError("missing amount field")
        if isinstance(amount_raw, bool) or isinstance(amount_raw, float):
            raise AmountParseError(f"invalid amount type {type(amount_raw).__name__}")
        if isinstance(amount_raw, int):
            amount_minor = amount_raw
            if amount_minor <= 0:
                raise AmountParseError(f"non-positive amount {amount_minor}")
        else:
            amount_minor = parse_bdt_amount(str(amount_raw)).amount_minor
        direction = str(raw_line.get("direction", "CREDIT")).upper()
        if direction not in ("CREDIT", "DEBIT"):
            raise AmountParseError(f"invalid direction {direction!r}")
        effective_raw = raw_line.get("effective_date")
        if effective_raw is None:
            raise AmountParseError("missing effective_date field")
        effective_date = datetime.strptime(str(effective_raw), "%Y-%m-%d").date()  # noqa: DTZ007
        return NormalizedReconLine(
            rail=rail,
            rail_transaction_id=(
                str(raw_line["rail_transaction_id"])
                if raw_line.get("rail_transaction_id")
                else None
            ),
            rail_amount_minor=amount_minor,
            rail_direction=direction,
            rail_effective_date=effective_date,
            rail_merchant_ref=(
                str(raw_line["merchant_ref"]) if raw_line.get("merchant_ref") else None
            ),
            raw_line_hash=raw_line_hash,
        )
    except (AmountParseError, ValueError, KeyError, TypeError) as exc:
        return NormalizedReconLine(
            rail=rail,
            rail_transaction_id=None,
            rail_amount_minor=None,
            rail_direction=None,
            rail_effective_date=None,
            rail_merchant_ref=None,
            raw_line_hash=raw_line_hash,
            parse_failed=True,
            parse_error=f"{type(exc).__name__}: {exc}",
        )


class _MatchResult:
    __slots__ = ("candidates", "delta_minor", "instruction_id", "match_level", "status")

    def __init__(
        self,
        status: str,
        instruction_id: str | None = None,
        match_level: int | None = None,
        delta_minor: int | None = None,
        candidates: tuple[str, ...] = (),
    ) -> None:
        self.status = status
        self.instruction_id = instruction_id
        self.match_level = match_level
        self.delta_minor = delta_minor
        self.candidates = candidates


class ReconciliationEngine:
    """reconciliation-service@1 — logical sub-service inside the ledger package."""

    def __init__(
        self,
        store: ReconStore,
        settlement_store: SettlementStore,
        ledger: LedgerService,
        *,
        clock: Clock,
        calendar: BangladeshBankCalendar | None = None,
        suspense_account_id: str,
        tcsa_account_id: str,
        mdr_income_account_id: str,
        producer: str = PRODUCER,
        metrics: MetricsRegistry | None = None,
    ) -> None:
        self._store = store
        self._settlement = settlement_store
        self._ledger = ledger
        self._clock = clock
        self._calendar = calendar or BangladeshBankCalendar()
        self._suspense_account_id = suspense_account_id
        self._tcsa_account_id = tcsa_account_id
        self._mdr_income_account_id = mdr_income_account_id
        self._producer = producer
        self._metrics = metrics

    # ------------------------------------------------------------------
    # FSM plumbing — refusal-first
    # ------------------------------------------------------------------

    def _record_transition(
        self, row: ReconciliationRecordRow, trigger: str, to_state: str
    ) -> None:
        allowed = RECORD_TRANSITIONS.get((row.match_status, trigger))
        if allowed is None or to_state not in allowed:
            raise ReconInvalidTransitionError(
                f"record {row.record_id}: {row.match_status} --[{trigger}]--> {to_state} "
                "is not in the spec/04 FSM 3 table (denied)"
            )

    def _exception_transition(
        self, row: ReconciliationExceptionRow, trigger: str, to_state: str
    ) -> None:
        allowed = EXCEPTION_TRANSITIONS.get((row.status, trigger))
        if allowed is None or to_state not in allowed:
            raise ReconInvalidTransitionError(
                f"exception {row.exception_id}: {row.status} --[{trigger}]--> {to_state} "
                "is not in the spec/04 FSM 4 table (denied)"
            )

    def _audit(
        self,
        subject_type: str,
        subject_id: str,
        from_state: str | None,
        to_state: str,
        detail: dict,
    ) -> None:
        event_type = (
            "RECONCILIATION_RECORD_STATE_TRANSITION"
            if subject_type == "ReconciliationRecord"
            else "RECONCILIATION_EXCEPTION_STATE_TRANSITION"
        )
        self._ledger.write_audit_event(
            AuditEventSpec(
                event_type=event_type,
                actor_id=self._producer,
                actor_type="SERVICE",
                subject_type=subject_type,
                subject_id=subject_id,
                from_state=from_state,
                to_state=to_state,
                payload=detail,
            )
        )

    def _emit(self, event_type: str, subject_type: str, subject_id: str, payload: dict) -> None:
        self._ledger.emit_event(
            event_type=event_type,
            subject_type=subject_type,
            subject_id=subject_id,
            payload=payload,
            topic="recon.events",
            producer=self._producer,
        )

    # ------------------------------------------------------------------
    # Match cascade (spec/04, binding)
    # ------------------------------------------------------------------

    def _match_cascade(self, line: NormalizedReconLine, now: datetime) -> _MatchResult:
        # Level 1 — primary key match on rail_transaction_id.
        if line.rail_transaction_id is not None:
            # spec/PT-G: pass rail_direction so a CREDIT recon line cannot match a DEBIT
            # instruction that shares the same rail_transaction_id.
            instruction = self._settlement.find_instruction_by_rail_txid(
                line.rail_transaction_id, direction=line.rail_direction
            )
            if instruction is not None:
                assert line.rail_amount_minor is not None
                if instruction.net_payout_minor == line.rail_amount_minor:
                    return _MatchResult(
                        "MATCHED", instruction.instruction_id, match_level=1, delta_minor=None
                    )
                return _MatchResult(
                    "AMOUNT_MISMATCH",
                    instruction.instruction_id,
                    match_level=1,
                    delta_minor=line.rail_amount_minor - instruction.net_payout_minor,
                )
        # Level 2 — fuzzy: amount + rail + cycle-date window (±2 days).
        # spec/PT-G: pass direction so a CREDIT recon line cannot fuzzy-match
        # a DEBIT instruction that shares the same amount/rail/date range.
        assert line.rail_effective_date is not None and line.rail_amount_minor is not None
        candidates = self._settlement.find_instructions_for_match(
            line.rail,
            line.rail_amount_minor,
            line.rail_effective_date - timedelta(days=2),
            line.rail_effective_date + timedelta(days=2),
            direction=line.rail_direction,
        )
        if len(candidates) == 1:
            return _MatchResult(
                "MATCHED", candidates[0].instruction_id, match_level=2, delta_minor=None
            )
        if len(candidates) > 1:
            return _MatchResult(
                "AMOUNT_MISMATCH",  # records as mismatch class; exception is AMBIGUOUS
                match_level=2,
                candidates=tuple(c.instruction_id for c in candidates),
            )
        # Level 3 — no match; T+2 business-day timing window.
        age_bd = business_days_between(
            self._calendar, line.rail_effective_date, now.astimezone(DHAKA_TZ).date()
        )
        if age_bd <= 2:
            return _MatchResult("TIMING_GAP")
        return _MatchResult("UNMATCHED")

    # ------------------------------------------------------------------
    # Ingestion
    # ------------------------------------------------------------------

    def ingest(
        self,
        batch_id: str | None,
        lines: list[NormalizedReconLine],
        now: datetime | None = None,
    ) -> ReconcileReport:
        """Persist one ReconciliationRecord per line and run the cascade.

        Idempotent: a line whose content-addressed record_id already exists
        is skipped (re-ingesting the same file produces no duplicates).
        """
        now = now or self._now()
        report = ReconcileReport()
        for line in lines:
            report.n_lines += 1
            try:
                self._ingest_line(batch_id, line, now, report)
            except ReconciliationError as exc:
                report.errors.append(f"{line.raw_line_hash[:12]}: {exc}")
        return report

    def _ingest_line(
        self,
        batch_id: str | None,
        line: NormalizedReconLine,
        now: datetime,
        report: ReconcileReport,
    ) -> None:
        record_id = make_id(
            "recon", {"raw_line_hash": line.raw_line_hash, "rail": line.rail}
        )
        if self._store.get_record(record_id) is not None:
            return  # idempotent re-ingest
        if line.parse_failed:
            row = self._record_row(record_id, batch_id, line, now, "DEAD_LETTER")
            self._store.insert_record(row)
            self._audit(
                "ReconciliationRecord",
                record_id,
                None,
                "DEAD_LETTER",
                {"parse_error": line.parse_error or "parse failed"},
            )
            self._raise_exception(
                row, "DEAD_LETTER", "PARSE_ERROR", internal_amount_minor=None, now=now
            )
            report.n_dead_letter += 1
            return
        result = self._match_cascade(line, now)
        row = self._record_row(
            record_id,
            batch_id,
            line,
            now,
            "INGESTED",
        )
        self._store.insert_record(row)
        self._record_transition(row, "match_cascade_run", result.status)
        if result.status == "MATCHED":
            self._store.update_record(
                record_id,
                match_status="MATCHED",
                match_level=result.match_level,
                matched_instruction_id=result.instruction_id,
                matched_at=now,
            )
            self._audit(
                "ReconciliationRecord",
                record_id,
                "INGESTED",
                "MATCHED",
                {"match_level": result.match_level},
            )
            self._emit(
                "reconciliation_record.matched",
                "ReconciliationRecord",
                record_id,
                {
                    "batch_id": batch_id,
                    "rail": line.rail,
                    "match_level": result.match_level,
                    "matched_instruction_id": result.instruction_id,
                },
            )
            report.n_matched += 1
            return
        if result.status == "AMOUNT_MISMATCH" and result.candidates:
            # Ambiguous fuzzy match — finance queue.
            updated = self._store.update_record(
                record_id, match_status="AMOUNT_MISMATCH", match_level=result.match_level
            )
            self._audit(
                "ReconciliationRecord",
                record_id,
                "INGESTED",
                "AMOUNT_MISMATCH",
                {"candidates": list(result.candidates)},
            )
            self._raise_exception(
                updated,
                "FINANCE_QUEUE",
                "AMBIGUOUS_MATCH",
                internal_amount_minor=None,
                now=now,
            )
            report.n_ambiguous += 1
            return
        if result.status == "AMOUNT_MISMATCH":
            instruction = (
                self._settlement.get_instruction(result.instruction_id)
                if result.instruction_id
                else None
            )
            updated = self._store.update_record(
                record_id,
                match_status="AMOUNT_MISMATCH",
                match_level=result.match_level,
                matched_instruction_id=result.instruction_id,
                delta_minor=result.delta_minor,
            )
            self._audit(
                "ReconciliationRecord",
                record_id,
                "INGESTED",
                "AMOUNT_MISMATCH",
                {"delta_minor": result.delta_minor},
            )
            self._emit(
                "reconciliation_record.amount_mismatch",
                "ReconciliationRecord",
                record_id,
                {
                    "matched_instruction_id": result.instruction_id,
                    "delta_minor": result.delta_minor,
                },
            )
            self._raise_exception(
                updated,
                "FINANCE_QUEUE",
                "AMOUNT_MISMATCH",
                internal_amount_minor=(
                    instruction.net_payout_minor if instruction is not None else None
                ),
                now=now,
            )
            report.n_amount_mismatch += 1
            return
        if result.status == "TIMING_GAP":
            updated = self._store.update_record(record_id, match_status="TIMING_GAP")
            self._audit("ReconciliationRecord", record_id, "INGESTED", "TIMING_GAP", {})
            self._emit(
                "reconciliation_record.timing_gap",
                "ReconciliationRecord",
                record_id,
                {
                    "batch_id": batch_id,
                    "rail": line.rail,
                    "rail_transaction_id": line.rail_transaction_id,
                    "ingested_at": now.isoformat(),
                },
            )
            self._raise_exception(
                updated, "AUTO_TIMING_GAP", "TIMING_GAP", internal_amount_minor=None, now=now
            )
            report.n_timing_gap += 1
            return
        # UNMATCHED (already beyond T+2 at first sight)
        updated = self._store.update_record(record_id, match_status="UNMATCHED")
        self._audit("ReconciliationRecord", record_id, "INGESTED", "UNMATCHED", {})
        self._emit(
            "reconciliation_record.unmatched",
            "ReconciliationRecord",
            record_id,
            {"batch_id": batch_id, "rail": line.rail},
        )
        self._raise_exception(
            updated, "FINANCE_QUEUE", "TIMING_GAP_EXPIRED", internal_amount_minor=None, now=now
        )
        report.n_unmatched += 1

    def _record_row(
        self,
        record_id: str,
        batch_id: str | None,
        line: NormalizedReconLine,
        now: datetime,
        match_status: str,
    ) -> ReconciliationRecordRow:
        return ReconciliationRecordRow(
            record_id=record_id,
            batch_id=batch_id,
            rail=line.rail,
            rail_transaction_id=line.rail_transaction_id,
            rail_amount_minor=line.rail_amount_minor,
            rail_direction=line.rail_direction,
            rail_effective_date=line.rail_effective_date,
            rail_merchant_ref=line.rail_merchant_ref,
            raw_line_hash=line.raw_line_hash,
            parse_failed=line.parse_failed,
            parse_error=line.parse_error,
            match_status=match_status,
            match_level=None,
            matched_instruction_id=None,
            delta_minor=None,
            ingested_at=now,
            matched_at=None,
            produced_by=self._producer,
        )

    # ------------------------------------------------------------------
    # Exceptions
    # ------------------------------------------------------------------

    def _due_by(self, tier: str, now: datetime) -> datetime:
        local = now.astimezone(DHAKA_TZ).date()
        days = {"AUTO_TIMING_GAP": 2, "FINANCE_QUEUE": 2, "DEAD_LETTER": 5}[tier]
        due_date = add_working_days(self._calendar, local, days)
        return datetime(
            due_date.year, due_date.month, due_date.day, tzinfo=DHAKA_TZ
        ).astimezone(UTC)

    def _raise_exception(
        self,
        record: ReconciliationRecordRow,
        tier: str,
        reason_code: str,
        *,
        internal_amount_minor: int | None,
        now: datetime,
    ) -> ReconciliationExceptionRow:
        if tier not in RESOLUTION_TIERS:
            raise ReconciliationError(f"unknown resolution tier {tier!r}")
        if reason_code not in REASON_CODES:
            raise ReconciliationError(f"unknown reason_code {reason_code!r}")
        delta = record.delta_minor
        exception_id = make_id(
            "rexc", {"record_id": record.record_id, "reason_code": reason_code}
        )
        row = ReconciliationExceptionRow(
            exception_id=exception_id,
            record_id=record.record_id,
            resolution_tier=tier,
            reason_code=reason_code,
            rail_amount_minor=record.rail_amount_minor,
            internal_amount_minor=internal_amount_minor,
            delta_minor=delta,
            status="OPEN",
            assigned_to=None,
            raised_at=now,
            due_by=self._due_by(tier, now),
            resolved_at=None,
            closed_at=None,
            resolution_action=None,
            adjustment_amount_minor=None,
            journal_entry_id=None,
            approval_request_id=None,
            notes=None,
            supporting_ref=None,
            resolved_by=None,
            aml_alert_id=None,
            produced_by=self._producer,
        )
        self._store.insert_exception(row)
        # Monitoring contract (monitoring/README.md + monitoring/prometheus-rules.yml):
        # the ReconExceptionsRaised alert watches increase() on this counter — every
        # newly raised exception ticks it exactly once, at the insert point.
        if self._metrics is not None:
            self._metrics.increment("reconciliation_exceptions_total")
        self._audit(
            "ReconciliationException",
            exception_id,
            None,
            "OPEN",
            {"resolution_tier": tier, "reason_code": reason_code},
        )
        self._emit(
            "reconciliation_exception.raised",
            "ReconciliationException",
            exception_id,
            {
                "record_id": record.record_id,
                "resolution_tier": tier,
                "reason_code": reason_code,
                "raised_at": now.isoformat(),
                "due_by": row.due_by.isoformat(),
            },
        )
        # Large deltas auto-escalate to CAMLCO (spec/04 FSM 4 aml_suspicion_flagged).
        if delta is not None and abs(delta) > AML_ESCALATION_DELTA_MINOR:
            self._exception_transition(row, "aml_suspicion_flagged", "ESCALATED")
            escalated = self._store.update_exception(exception_id, status="ESCALATED")
            self._audit(
                "ReconciliationException",
                exception_id,
                "OPEN",
                "ESCALATED",
                {"delta_minor": delta},
            )
            self._emit(
                "reconciliation_exception.escalated",
                "ReconciliationException",
                exception_id,
                {"reason": "delta_above_aml_threshold"},
            )
            return escalated
        return row

    def assign(self, exception_id: str, operator_id: str) -> ReconciliationExceptionRow:
        row = self._require_exception(exception_id)
        self._exception_transition(row, "assigned_to_finance", "IN_REVIEW")
        updated = self._store.update_exception(
            exception_id, status="IN_REVIEW", assigned_to=operator_id
        )
        self._audit(
            "ReconciliationException", exception_id, "OPEN", "IN_REVIEW", {"assigned": True}
        )
        return updated

    def retry_timing_gaps(self, now: datetime | None = None) -> ReconcileReport:
        """Tier-1 auto resolution: re-run the cascade for TIMING_GAP records."""
        now = now or self._now()
        report = ReconcileReport()
        for record in self._store.list_records(match_status="TIMING_GAP"):
            report.n_lines += 1
            line = NormalizedReconLine(
                rail=record.rail,
                rail_transaction_id=record.rail_transaction_id,
                rail_amount_minor=record.rail_amount_minor,
                rail_direction=record.rail_direction,
                rail_effective_date=record.rail_effective_date,
                rail_merchant_ref=record.rail_merchant_ref,
                raw_line_hash=record.raw_line_hash,
            )
            result = self._match_cascade(line, now)
            exception = self._store.find_exception_for_record(record.record_id)
            if result.status == "MATCHED":
                self._record_transition(record, "auto_retry_match", "MATCHED")
                self._store.update_record(
                    record.record_id,
                    match_status="MATCHED",
                    match_level=result.match_level,
                    matched_instruction_id=result.instruction_id,
                    matched_at=now,
                )
                self._audit(
                    "ReconciliationRecord", record.record_id, "TIMING_GAP", "MATCHED", {}
                )
                self._emit(
                    "reconciliation_record.matched",
                    "ReconciliationRecord",
                    record.record_id,
                    {
                        "batch_id": record.batch_id,
                        "rail": record.rail,
                        "match_level": result.match_level,
                        "matched_instruction_id": result.instruction_id,
                    },
                )
                if exception is not None:
                    self._exception_transition(exception, "auto_timing_gap_resolved", "CLOSED")
                    self._store.update_exception(
                        exception.exception_id,
                        status="CLOSED",
                        closed_at=now,
                        notes="TIMING_GAP_AUTO_RESOLVED",
                    )
                    self._audit(
                        "ReconciliationException",
                        exception.exception_id,
                        "OPEN",
                        "CLOSED",
                        {"reason": "TIMING_GAP_AUTO_RESOLVED"},
                    )
                report.n_matched += 1
                continue
            assert record.rail_effective_date is not None
            age_bd = business_days_between(
                self._calendar, record.rail_effective_date, now.astimezone(DHAKA_TZ).date()
            )
            if age_bd > 2:
                self._record_transition(record, "timing_window_expired", "UNMATCHED")
                self._store.update_record(record.record_id, match_status="UNMATCHED")
                self._audit(
                    "ReconciliationRecord", record.record_id, "TIMING_GAP", "UNMATCHED", {}
                )
                self._emit(
                    "reconciliation_record.unmatched",
                    "ReconciliationRecord",
                    record.record_id,
                    {"batch_id": record.batch_id, "rail": record.rail},
                )
                if exception is not None:
                    # spec/PT-H: tier-upgrade from AUTO_TIMING_GAP to FINANCE_QUEUE.
                    # The FSM comment was correct but the call was missing resolution_tier —
                    # expired timing gaps were staying queryable as AUTO_TIMING_GAP and never
                    # reaching the finance-ops queue.
                    self._store.update_exception(
                        exception.exception_id,
                        resolution_tier="FINANCE_QUEUE",
                        notes="TIMING_GAP_EXPIRED",
                    )
                report.n_unmatched += 1
            else:
                report.n_timing_gap += 1
        return report

    def resolve(
        self,
        exception_id: str,
        *,
        resolution_action: str,
        resolved_by: str,
        record_outcome: str,
        adjustment_amount_minor: int | None = None,
        approval_request_id: str | None = None,
        notes: str | None = None,
        supporting_ref: str | None = None,
        now: datetime | None = None,
    ) -> ReconciliationExceptionRow:
        """Operator resolution (spec/04 §4.2). Money-moving actions need
        two-eyes approval above the BDT 500 threshold (fails closed)."""
        now = now or self._now()
        if resolution_action not in RESOLUTION_ACTIONS:
            raise ReconciliationError(f"unknown resolution_action {resolution_action!r}")
        row = self._require_exception(exception_id)
        trigger = "camlco_resolved" if row.status == "ESCALATED" else "resolved"
        self._exception_transition(row, trigger, "RESOLVED")
        journal_entry_id: str | None = None
        if resolution_action in _LEDGER_ACTIONS:
            if adjustment_amount_minor is None or adjustment_amount_minor <= 0:
                raise ReconciliationError(
                    f"{resolution_action} requires a positive adjustment_amount_minor"
                )
            if (
                abs(adjustment_amount_minor) > RECON_ADJUSTMENT_APPROVAL_THRESHOLD_MINOR
                and approval_request_id is None
            ):
                raise SettlementGuardError(
                    "ledger adjustment above the two-eyes threshold requires an "
                    "approval_request_id (fails closed)"
                )
            journal_entry_id = self._post_resolution_entry(
                exception_id, resolution_action, adjustment_amount_minor
            )
        record = self._store.get_record(row.record_id)
        if record is not None and record.match_status in ("UNMATCHED", "AMOUNT_MISMATCH"):
            self._record_transition(record, "finance_resolved", record_outcome)
            changes: dict[str, object] = {"match_status": record_outcome}
            if record_outcome == "MATCHED":
                changes["matched_at"] = now
            self._store.update_record(record.record_id, **changes)
            self._audit(
                "ReconciliationRecord",
                record.record_id,
                record.match_status,
                record_outcome,
                {"resolution_action": resolution_action},
            )
        updated = self._store.update_exception(
            exception_id,
            status="RESOLVED",
            resolved_at=now,
            resolution_action=resolution_action,
            adjustment_amount_minor=adjustment_amount_minor,
            journal_entry_id=journal_entry_id,
            approval_request_id=approval_request_id,
            notes=notes,
            supporting_ref=supporting_ref,
            resolved_by=resolved_by,
        )
        self._audit(
            "ReconciliationException",
            exception_id,
            row.status,
            "RESOLVED",
            {"resolution_action": resolution_action},
        )
        self._emit(
            "reconciliation_exception.resolved",
            "ReconciliationException",
            exception_id,
            {
                "resolution_action": resolution_action,
                "journal_entry_id": journal_entry_id,
                "resolved_at": now.isoformat(),
            },
        )
        return updated

    def _post_resolution_entry(
        self, exception_id: str, action: str, amount_minor: int
    ) -> str:
        if action == "SUSPENSE_TRANSFER":
            # Unknown rail credit parked in suspense (LA-L2: suspense_entry).
            entry_type = "suspense_entry"
            postings = (
                PostingSpec(
                    account_id=self._tcsa_account_id, side="DEBIT", amount_minor=amount_minor
                ),
                PostingSpec(
                    account_id=self._suspense_account_id,
                    side="CREDIT",
                    amount_minor=amount_minor,
                ),
            )
        else:  # WRITE_OFF — clear out of suspense (LA-L2: suspense_cleared).
            entry_type = "suspense_cleared"
            postings = (
                PostingSpec(
                    account_id=self._suspense_account_id,
                    side="DEBIT",
                    amount_minor=amount_minor,
                ),
                PostingSpec(
                    account_id=self._mdr_income_account_id,
                    side="CREDIT",
                    amount_minor=amount_minor,
                ),
            )
        return self._ledger.post_journal_entry(
            JournalEntrySpec(
                reference_id=exception_id,
                reference_type="ADJUSTMENT",
                entry_type=entry_type,
                description=f"reconciliation exception resolution: {action}",
                produced_by=self._producer,
                idempotency_key=make_id(
                    "idem", {"exception_id": exception_id, "action": action}
                ),
                postings=postings,
            )
        )

    def sla_check(self, now: datetime | None = None) -> list[str]:
        """Emit ``reconciliation_exception.sla_breached`` for overdue items."""
        now = now or self._now()
        breached: list[str] = []
        for status in ("OPEN", "IN_REVIEW"):
            for row in self._store.list_exceptions(status=status):
                if now > row.due_by:
                    days_overdue = (now - row.due_by).days
                    self._emit(
                        "reconciliation_exception.sla_breached",
                        "ReconciliationException",
                        row.exception_id,
                        {
                            "resolution_tier": row.resolution_tier,
                            "days_overdue": days_overdue,
                        },
                    )
                    breached.append(row.exception_id)
        return breached

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _require_exception(self, exception_id: str) -> ReconciliationExceptionRow:
        row = self._store.get_exception(exception_id)
        if row is None:
            raise ReconciliationError(f"unknown exception {exception_id}")
        return row

    def _now(self) -> datetime:
        dt = self._clock.now()
        if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
            raise ReconciliationError("clock returned a naive datetime (spec/00 §7)")
        return dt.astimezone(UTC)

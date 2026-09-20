"""Reconciliation entities + FSM tables (spec/04 FSM 3 / FSM 4).

Refusal-first: any (state, trigger) pair not in the tables is DENIED.
The per-run ``ReconcileReport`` counter shape is ported from
``dse-profit-engine/src/dse_engine/pod/reconciler.py::ReconcileReport``
(one counter per drift classification + an errors list).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

PRODUCER = "reconciliation-service@1"

# --- FSM 3: ReconciliationRecord ---------------------------------------------

RECORD_STATES: frozenset[str] = frozenset(
    {"INGESTED", "MATCHED", "TIMING_GAP", "UNMATCHED", "AMOUNT_MISMATCH", "DEAD_LETTER"}
)
RECORD_TERMINAL_STATES: frozenset[str] = frozenset({"MATCHED", "DEAD_LETTER"})

RECORD_TRANSITIONS: dict[tuple[str, str], tuple[str, ...]] = {
    ("INGESTED", "match_cascade_run"): (
        "MATCHED",
        "TIMING_GAP",
        "UNMATCHED",
        "AMOUNT_MISMATCH",
        "DEAD_LETTER",
    ),
    ("TIMING_GAP", "auto_retry_match"): ("MATCHED",),
    ("TIMING_GAP", "timing_window_expired"): ("UNMATCHED",),
    ("UNMATCHED", "finance_resolved"): ("MATCHED", "DEAD_LETTER"),
    ("AMOUNT_MISMATCH", "finance_resolved"): ("MATCHED", "DEAD_LETTER"),
}

# --- FSM 4: ReconciliationException ------------------------------------------

EXCEPTION_STATES: frozenset[str] = frozenset(
    {"OPEN", "IN_REVIEW", "RESOLVED", "ESCALATED", "CLOSED"}
)
EXCEPTION_TERMINAL_STATES: frozenset[str] = frozenset({"RESOLVED", "CLOSED"})

EXCEPTION_TRANSITIONS: dict[tuple[str, str], tuple[str, ...]] = {
    ("OPEN", "auto_timing_gap_resolved"): ("CLOSED",),
    ("OPEN", "assigned_to_finance"): ("IN_REVIEW",),
    ("OPEN", "aml_suspicion_flagged"): ("ESCALATED",),
    ("IN_REVIEW", "resolved"): ("RESOLVED",),
    ("IN_REVIEW", "escalated_from_review"): ("ESCALATED",),
    ("ESCALATED", "camlco_resolved"): ("RESOLVED",),
}

RESOLUTION_TIERS: frozenset[str] = frozenset({"AUTO_TIMING_GAP", "FINANCE_QUEUE", "DEAD_LETTER"})

REASON_CODES: frozenset[str] = frozenset(
    {
        "PARSE_ERROR",
        "RAIL_RETURN",
        "RAIL_REJECT",
        "AMOUNT_MISMATCH",
        "AMBIGUOUS_MATCH",
        "TIMING_GAP_EXPIRED",
        "TIMING_GAP",
        "AML_SUSPICIOUS",
        "NO_MATCH_DEAD_LETTER",
        "BATCH_FAILED",
    }
)

RESOLUTION_ACTIONS: frozenset[str] = frozenset(
    {"WRITE_OFF", "CHASE_RAIL", "REVERSE_INTERNAL", "SUSPENSE_TRANSFER", "CLOSE_NO_ACTION"}
)

#: AML escalation threshold: delta > BDT 50,000 = 5,000,000 paisa (spec/04 FSM 4).
AML_ESCALATION_DELTA_MINOR = 5_000_000

#: Two-eyes threshold for recon ledger adjustments (spec/04 Tier 2):
#: |delta| > 50,000 paisa = BDT 500.
RECON_ADJUSTMENT_APPROVAL_THRESHOLD_MINOR = 50_000


@dataclass(frozen=True, slots=True)
class NormalizedReconLine:
    """One normalized line from a rail settlement file (spec/04 shape)."""

    rail: str
    rail_transaction_id: str | None
    rail_amount_minor: int | None  # paisa; after Bengali-digit normalization
    rail_direction: str | None  # 'CREDIT' | 'DEBIT'
    rail_effective_date: date | None
    rail_merchant_ref: str | None
    raw_line_hash: str  # sha256(canonical_json(raw_line)); raw never logged
    parse_failed: bool = False
    parse_error: str | None = None


@dataclass(frozen=True, slots=True)
class ReconciliationRecordRow:
    """reconciliation_records row (spec/04 DDL; unpartitioned per LA-L1)."""

    record_id: str
    batch_id: str | None
    rail: str
    rail_transaction_id: str | None
    rail_amount_minor: int | None
    rail_direction: str | None
    rail_effective_date: date | None
    rail_merchant_ref: str | None
    raw_line_hash: str
    parse_failed: bool
    parse_error: str | None
    match_status: str
    match_level: int | None
    matched_instruction_id: str | None
    delta_minor: int | None
    ingested_at: datetime
    matched_at: datetime | None
    produced_by: str
    schema_version: int = 1


@dataclass(frozen=True, slots=True)
class ReconciliationExceptionRow:
    """reconciliation_exceptions row (spec/04 DDL)."""

    exception_id: str
    record_id: str
    resolution_tier: str
    reason_code: str
    rail_amount_minor: int | None
    internal_amount_minor: int | None
    delta_minor: int | None
    status: str
    assigned_to: str | None
    raised_at: datetime
    due_by: datetime
    resolved_at: datetime | None
    closed_at: datetime | None
    resolution_action: str | None
    adjustment_amount_minor: int | None
    journal_entry_id: str | None
    approval_request_id: str | None
    notes: str | None
    supporting_ref: str | None
    resolved_by: str | None
    aml_alert_id: str | None
    produced_by: str
    schema_version: int = 1


@dataclass
class ReconcileReport:
    """Per-run summary — drift-classification shape from the DSE reconciler."""

    n_lines: int = 0
    n_matched: int = 0
    n_amount_mismatch: int = 0
    n_timing_gap: int = 0
    n_unmatched: int = 0
    n_ambiguous: int = 0
    n_dead_letter: int = 0
    errors: list[str] = field(default_factory=list)

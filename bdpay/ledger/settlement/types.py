"""Settlement entities, FSM transition tables, and rail vocabularies (spec/04).

The transition tables below are the BINDING FSM 1 / FSM 2 tables from
spec/04 §State Machines, encoded as ``{(from_state, trigger): allowed
to-states}``. Refusal-first: any (state, trigger) pair not in the table is
DENIED (``SettlementInvalidTransitionError``); guards select among the
allowed to-states and a failed guard is equally a denial.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

PRODUCER = "settlement-engine@1"

RAILS: frozenset[str] = frozenset(
    {
        "beftn_batch_v2",
        "npsb_iso8583_v28",
        "rtgs_iso20022_v1",
        "bkash_pgw_v2",
        "nagad_pgw_v33",
        "rocket_aggregator_v1",
        "card_acquirer_v1",
    }
)

SESSIONS: frozenset[str] = frozenset({"morning", "afternoon", "eod"})

MCC_CATEGORIES: frozenset[str] = frozenset({"DAILY_ESSENTIAL", "DIRECT_MERCHANT", "OTHER"})

DIRECTIONS: frozenset[str] = frozenset({"CREDIT", "DEBIT"})

# --- FSM 1: SettlementBatch -------------------------------------------------

BATCH_STATES: frozenset[str] = frozenset(
    {"OPEN", "PENDING_DISPATCH", "HELD", "DISPATCHED", "CONFIRMED", "FAILED", "CANCELLED"}
)
BATCH_TERMINAL_STATES: frozenset[str] = frozenset({"CONFIRMED", "CANCELLED"})

#: (from_state, trigger) -> tuple of permitted to-states (guard chooses).
BATCH_TRANSITIONS: dict[tuple[str, str], tuple[str, ...]] = {
    ("OPEN", "batch_cutoff_reached"): ("PENDING_DISPATCH", "CANCELLED"),
    ("PENDING_DISPATCH", "approval_granted"): ("PENDING_DISPATCH",),
    ("PENDING_DISPATCH", "dispatch_triggered"): ("DISPATCHED", "HELD"),
    ("PENDING_DISPATCH", "operator_cancel"): ("CANCELLED",),
    ("HELD", "tcsa_shortfall_resolved"): ("PENDING_DISPATCH",),
    ("HELD", "operator_cancel"): ("CANCELLED",),
    ("DISPATCHED", "sponsor_bank_confirmed"): ("CONFIRMED",),
    # Crash-resume: a CONFIRMED batch with still-SUBMITTED instructions can
    # re-enter confirm() to finish phase 3 (settle the remaining instructions)
    # without re-posting the settlement_batch_close JE or re-updating the batch.
    ("CONFIRMED", "sponsor_bank_confirmed"): ("CONFIRMED",),
    ("DISPATCHED", "sponsor_bank_rejected"): ("FAILED",),
    ("DISPATCHED", "connector_timeout"): ("FAILED",),
    ("FAILED", "manual_retry_approved"): ("PENDING_DISPATCH",),
    ("FAILED", "max_retries_exceeded"): ("CANCELLED",),
}

# --- FSM 2: SettlementInstruction --------------------------------------------

INSTRUCTION_STATES: frozenset[str] = frozenset(
    {"QUEUED", "SUBMITTED", "SETTLED", "RETURNED", "CANCELLED", "FAILED"}
)
INSTRUCTION_TERMINAL_STATES: frozenset[str] = frozenset(
    {"SETTLED", "RETURNED", "CANCELLED", "FAILED"}
)

INSTRUCTION_TRANSITIONS: dict[tuple[str, str], tuple[str, ...]] = {
    ("QUEUED", "batch_dispatched"): ("SUBMITTED",),
    ("QUEUED", "batch_cancelled"): ("CANCELLED",),
    ("SUBMITTED", "rail_confirmed"): ("SETTLED",),
    ("SUBMITTED", "rail_returned"): ("RETURNED",),
    ("SUBMITTED", "rail_hard_rejected"): ("FAILED",),
    ("SUBMITTED", "timeout_on_batch_fail"): ("FAILED",),
    # PT-E fix: manual_retry resets FAILED instructions to QUEUED so
    # try_dispatch picks them up again.  Only reachable from engine.manual_retry();
    # never from the rail or the normal dispatch path.
    ("FAILED", "batch_retry_reset"): ("QUEUED",),
}

#: spec/04 MAX_RETRIES defaults (configurable).
MAX_RETRIES: dict[str, int] = {
    "beftn_batch_v2": 3,
    "npsb_iso8583_v28": 5,
    "rtgs_iso20022_v1": 3,
    "bkash_pgw_v2": 5,
    "nagad_pgw_v33": 5,
    "rocket_aggregator_v1": 5,
    "card_acquirer_v1": 3,
}

#: spec/04 dispatch timeout defaults, minutes (BEFTN=120, NPSB=60, RTGS=30, MFS=30).
BATCH_DISPATCH_TIMEOUT_MINUTES: dict[str, int] = {
    "beftn_batch_v2": 120,
    "npsb_iso8583_v28": 60,
    "rtgs_iso20022_v1": 30,
    "bkash_pgw_v2": 30,
    "nagad_pgw_v33": 30,
    "rocket_aggregator_v1": 30,
    "card_acquirer_v1": 30,
}

#: Two-eyes thresholds (paisa). Dispatch: > BDT 5,000,000; retry: > BDT 50,000
#: lakh-denominated per spec/04 §4.1 (BDT 5 lakh = 500,000 BDT = 50,000,000 paisa).
DISPATCH_TWO_EYES_THRESHOLD_MINOR = 500_000_000
RETRY_TWO_EYES_THRESHOLD_MINOR = 50_000_000

#: RTGS high-value threshold: BDT 100,000 = 10,000,000 paisa (research 02 §3).
HIGH_VALUE_THRESHOLD_MINOR = 10_000_000


@dataclass(frozen=True, slots=True)
class SettlementBatchRow:
    """settlement_batches row (spec/04 DDL; unpartitioned per errata LA-L1)."""

    batch_id: str
    rail: str
    cycle_date: date
    session: str | None
    status: str
    instruction_count: int
    total_credit_minor: int
    total_debit_minor: int
    currency: str
    retry_count: int
    two_eyes_required: bool
    approval_request_id: str | None
    held_at: datetime | None
    held_tcsa_snapshot_id: str | None
    sponsor_bank_ref: str | None
    recon_file_pointer: str | None
    recon_file_hash: str | None
    connector_ref: str | None
    created_at: datetime
    cutoff_at: datetime | None
    dispatched_at: datetime | None
    confirmed_at: datetime | None
    failed_at: datetime | None
    cancelled_at: datetime | None
    fail_reason_code: str | None
    produced_by: str
    schema_version: int = 1


@dataclass(frozen=True, slots=True)
class SettlementInstructionRow:
    """settlement_instructions row (spec/04 DDL).

    ``batch_id`` is None while the instruction is QUEUED behind its release
    holds; it is assigned when the instruction joins an OPEN batch (errata
    LA-L9: the spec's NOT NULL batch_id contradicts its own "not added to any
    dispatch batch until released" rule).
    """

    instruction_id: str
    batch_id: str | None
    payment_intent_id: str
    merchant_id: str
    direction: str
    amount_minor: int
    fee_minor: int
    net_payout_minor: int
    fee_rule_id: str | None
    currency: str
    rail: str
    beneficiary_account_ref: str
    mcc_category: str
    high_value: bool
    earliest_release_at: datetime
    latest_release_at: datetime
    delivery_hold_cleared: bool
    aml_hold: bool
    status: str
    rail_transaction_id: str | None
    return_reason_code: str | None
    connector_ref: str
    created_at: datetime
    submitted_at: datetime | None
    settled_at: datetime | None
    returned_at: datetime | None
    failed_at: datetime | None
    produced_by: str
    schema_version: int = 1

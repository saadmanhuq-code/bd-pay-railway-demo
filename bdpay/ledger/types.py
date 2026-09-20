"""Canonical ledger types and vocabularies (spec/03 Entities + Data model).

Ported from bd-pay-fable-pilot/src/bdpay_pilot/ledger/types.py (verified
pilot), extended with the audit-event row/spec types (spec/03 AuditEvent
design — descoped in the pilot, implemented here).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

PRODUCER = "ledger-service@1"

SIDES: frozenset[str] = frozenset({"DEBIT", "CREDIT"})

ACCOUNT_TYPES: frozenset[str] = frozenset({"ASSET", "LIABILITY", "EQUITY", "INCOME", "EXPENSE"})

# Normal balance: ASSET/EXPENSE = DEBIT; LIABILITY/EQUITY/INCOME = CREDIT.
DEBIT_NORMAL_TYPES: frozenset[str] = frozenset({"ASSET", "EXPENSE"})

ACCOUNT_SUBTYPES: frozenset[str] = frozenset(
    {
        # ASSET
        "SPONSOR_BANK_TCSA",
        "IN_TRANSIT",
        "FEE_RECEIVABLE",
        "SUSPENSE",
        "CONNECTOR_CLEARING",
        # LIABILITY
        "CUSTOMER_FLOAT",
        "CUSTOMER_HOLD_RESERVE",
        "MERCHANT_SETTLEMENT",
        "MERCHANT_HOLD_RESERVE",
        "MERCHANT_ROLLING_RESERVE",
        "REFUND_RESERVE",
        "PARTICIPANT_POSITION",
        "NET_DEBIT_CAP",
        # EQUITY
        "RETAINED_EARNINGS",
        "PAID_UP_CAPITAL",
        # INCOME
        "MDR_INCOME",
        "SWITCHING_FEE_INCOME",
        "INTERCHANGE_INCOME",
        "FLOAT_INTEREST_INCOME",
        # INCOME (spec/18 offer engine — MONEY-02)
        "OFFER_COMMISSION_INCOME",
        # EXPENSE
        "SPONSOR_BANK_FEE",
        "NETWORK_FEE",
        "REFUND_EXPENSE",
        "CONNECTIVITY_EXPENSE",
        # EXPENSE (spec/18 offer engine — MONEY-02)
        "OFFER_MARKETING_EXPENSE",
    }
)

OWNER_TYPES: frozenset[str] = frozenset({"MERCHANT", "CUSTOMER", "PARTICIPANT", "SYSTEM"})

HOLD_STATUSES: frozenset[str] = frozenset({"OPEN", "CAPTURED", "VOIDED", "EXPIRED"})

ENTRY_TYPES: frozenset[str] = frozenset(
    {
        "payment_authorized",
        "payment_captured",
        "payment_failed",
        "payment_reversed",
        "refund_initiated",
        "refund_settled",
        "fee_collected",
        "hold_opened",
        "hold_captured",
        "hold_voided",
        "hold_expired",
        "rolling_reserve_withheld",
        "rolling_reserve_released",
        "settlement_batch_open",
        "settlement_batch_close",
        "settlement_confirmed",
        "tcsa_balance_update",
        "participant_debit_cap_set",
        "position_updated",
        "suspense_entry",
        "suspense_cleared",
        "connector_clearing_in",
        "connector_clearing_out",
        "adjustment_debit",
        "adjustment_credit",
        # spec/18 offer engine ledger legs (MONEY-02): deal commission collected
        # /reversed and launch subsidy granted/reversed (offers/ledger_legs.py).
        "offer_commission_collected",
        "offer_commission_reversed",
        "offer_subsidy_granted",
        "offer_subsidy_reversed",
        # spec/17 VX4 bulk-disbursement ledger legs (MONEY-02 class): the
        # DisbursementService (kernel/disbursement/service.py) posts these on
        # batch funding, item settlement and item return. They are already in
        # the 0120 DB CHECK; the Python vocabulary must match or every
        # disbursement posting is rejected at _validate_entry_spec — masked
        # today only because the kernel tests use the invariant-free
        # RecordingLedger fake.
        "disbursement_funded",
        "disbursement_item_paid",
        "disbursement_item_returned",
        # spec/17 FAILED terminal state hold-release (0155): debits
        # merchant_hold_reserve / credits merchant_settlement when an item
        # transitions to FAILED so the reserved funds are not trapped.
        "disbursement_failed_release",
        # spec/18 PT-C: settlement cancel/fail reversal leg — DR transit_account
        # / CR TCSA posted at terminal-CANCELLED exits (cancel() and
        # manual_retry-at-max); unstages the float staged by settlement_batch_open.
        # DB vocabulary: migration 0156_settlement_batch_open_reversal_vocab.sql.
        "settlement_batch_open_reversal",
    }
)

REFERENCE_TYPES: frozenset[str] = frozenset(
    {"PAYMENT", "SETTLEMENT", "REFUND", "FEE", "REVERSAL", "ADJUSTMENT", "TCSA", "HOLD", "RESERVE"}
)

CHECKPOINT_INTERVAL = 1000

GENESIS_ENTRY_TYPE = "genesis"

#: spec/03 audit event vocabulary, plus the additive settlement/recon/window
#: transition types the spec/04 + spec/05 FSM side-effects require but the
#: spec/03 CHECK list omits (errata row LA-L7; additive only).
AUDIT_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "PAYMENT_STATE_TRANSITION",
        "PAYMENT_ATTEMPT_STATE_TRANSITION",
        "REFUND_STATE_TRANSITION",
        "HOLD_OPENED",
        "HOLD_CAPTURED",
        "HOLD_VOIDED",
        "HOLD_EXPIRED",
        "KYC_VERIFIED",
        "KYC_TIER_ASSIGNED",
        "KYC_REVIEW_TRIGGERED",
        "KYB_VERIFIED",
        "MERCHANT_STATE_TRANSITION",
        "MERCHANT_SUSPENDED",
        "MERCHANT_TERMINATED",
        "RULE_ACTIVATED",
        "RULE_DEACTIVATED",
        "CONFIG_CHANGED",
        "APPROVAL_REQUESTED",
        "APPROVAL_GRANTED",
        "APPROVAL_DENIED",
        "APPROVAL_EXECUTED",
        "SANCTIONS_HIT",
        "SANCTIONS_CLEARED",
        "AML_ALERT_RAISED",
        "STR_FILED",
        "CTR_FILED",
        "CONNECTOR_CALL",
        "CONNECTOR_RESULT",
        "CHAIN_VERIFY_STARTED",
        "CHAIN_VERIFY_COMPLETED",
        "CHAIN_TAMPER_DETECTED",
        "CHAIN_VERIFY_ERROR",
        "REPLAY_STARTED",
        "REPLAY_COMPLETED",
        "REPLAY_FAILED",
        "SETTLEMENT_BATCH_STATE_TRANSITION",
        "TCSA_SHORTFALL_DETECTED",
        "POSITION_LIMIT_BREACHED",
        "ARCHIVAL_STARTED",
        "ARCHIVAL_COMPLETED",
        "OPERATOR_LOGIN",
        "OPERATOR_ACTION",
        "BB_INSPECTOR_ACCESS",
        # Additive (LA-L7): spec/04 + spec/05 FSM audit side-effects.
        "SETTLEMENT_INSTRUCTION_STATE_TRANSITION",
        "RECONCILIATION_RECORD_STATE_TRANSITION",
        "RECONCILIATION_EXCEPTION_STATE_TRANSITION",
        "SETTLEMENT_WINDOW_STATE_TRANSITION",
        "NDC_BREACH_BLOCKED",
        "TCSA_SHORTFALL_RESOLVED",
        # Additive (spec/06+07 compliance wiring): sanctions screener + AML
        # monitor emit these through the shared AuditPort; they are recorded in
        # the AUDIT chain alongside payment and settlement events.
        "SANCTIONS_LIST_INGESTED",
        "SANCTIONS_SCREENING_COMPLETED",
        "SANCTIONS_HIT_DETECTED",
        "SANCTIONS_HIT_STATE_TRANSITION",
        "AML_ALERT_STATE_TRANSITION",
        "AML_PREFLIGHT_RULE_PACK_MISSING",
        "MONITOR_EVALUATION_FAILED",
        "AML_MONITOR_RULE_PACK_MISSING",
        "RULE_PACK_LOADED",
        "RULE_PACK_ACTIVATED",
        "CAMLCO_EXPORT_STARTED",
        "CAMLCO_EXPORT_COMPLETED",
        "FACT_CORRECTION_VERIFIED",
        "DOSSIER_EXPORT_STARTED",
        "DOSSIER_EXPORT_COMPLETED",
        # Additive (spec/16 LR-1/LR-2/LR-3 FSM side-effects, exact spec names):
        # the DossierExport FSM writes DOSSIER_ASSEMBLY_* (spec/16 State
        # Machines §1) and the FactCorrection FSM writes RECORDED/RETRACTED
        # alongside the VERIFIED row above.
        "DOSSIER_ASSEMBLY_STARTED",
        "DOSSIER_ASSEMBLY_COMPLETED",
        "DOSSIER_ASSEMBLY_FAILED",
        "FACT_CORRECTION_RECORDED",
        "FACT_CORRECTION_RETRACTED",
        # Additive (LA-L7 class; spec/13 Bangla QR FSM side-effects): the
        # QrService writes QR_ISSUED on activation, QR_<TRIGGER> on lifecycle
        # transitions, and QR_PAY_INITIATED on the kernel hand-off.
        "QR_ISSUED",
        "QR_SUSPEND",
        "QR_REACTIVATE",
        "QR_REVOKE",
        "QR_PAY_INITIATED",
        # Additive (LA-L7 class; spec/08 KYB FSM side-effects): the built
        # MerchantOnboardingService writes these through the shared AuditPort;
        # spec/03's CHECK list carries only the summary KYB_VERIFIED row.
        # Exposed by the spec/16 LR-4 sandbox provisioning path, which drives
        # the unmodified KYB FSM against the ledger-backed audit adapter.
        "KYB_STATE_TRANSITION",
        "KYB_DOCUMENT_UPLOADED",
        "KYB_UBO_ADDED",
        "KYB_UBO_VERIFICATION",
        "KYB_AGREEMENT_SIGNED",
        # Additive (spec/16 LR-4 FSM side-effects, exact spec names): the
        # PaymentLink FSM (spec/16 State Machines §2) and the SandboxSignup
        # FSM (§4) write one audit row per transition.
        "PAYMENT_LINK_CREATED",
        "PAYMENT_LINK_PAID",
        "PAYMENT_LINK_EXPIRED",
        "PAYMENT_LINK_CANCELLED",
        "SANDBOX_SIGNUP_CREATED",
        "SANDBOX_SIGNUP_VERIFIED",
        "SANDBOX_SIGNUP_PROVISIONED",
        "SANDBOX_SIGNUP_EXPIRED",
        "SANDBOX_SIGNUP_REVOKED",
        # Additive (LA-L7 class; spec/18 merchant offer FSM side-effects):
        # OfferService writes one audit row for create/lifecycle transitions.
        "OFFER_STATE_TRANSITION",
        # Additive (LA-L7 class; spec/17 subscription FSM side-effects):
        # SubscriptionService writes subscription and cycle transition rows.
        "SUBSCRIPTION_STATE_TRANSITION",
        "SUBSCRIPTION_CYCLE_STATE_TRANSITION",
        # Additive (LA-L7 class; spec/17 disbursement FSM side-effects):
        # DisbursementService writes batch and item transition rows.
        "DISBURSEMENT_BATCH_STATE_TRANSITION",
        "DISBURSEMENT_ITEM_STATE_TRANSITION",
        # Additive (LA-L7 class; spec/19 participant onboarding side-effects):
        # ParticipantOnboardingService writes participant/document/conformance
        # rows through the shared ledger-backed AuditPort.
        "PARTICIPANT_APPLICATION_SUBMITTED",
        "PARTICIPANT_DOCS_REQUESTED",
        "PARTICIPANT_DOCUMENT_UPLOADED",
        "PARTICIPANT_DOCUMENT_VERIFIED",
        "PARTICIPANT_DOCUMENT_REJECTED",
        "PARTICIPANT_LICENSE_INVALID",
        "PARTICIPANT_MOU_SIGNED",
        "PARTICIPANT_CONFORMANCE_STARTED",
        "PARTICIPANT_CONFORMANCE_PASSED",
        "PARTICIPANT_CONFORMANCE_FAILED",
        "PARTICIPANT_CONFORMANCE_EXHAUSTED",
        "PARTICIPANT_ACTIVATION_REQUESTED",
        "PARTICIPANT_ACTIVATED",
        "PARTICIPANT_SUSPENDED",
        "PARTICIPANT_REINSTATED",
        "PARTICIPANT_TERMINATED",
        "PARTICIPANT_REJECTED",
        "CONFORMANCE_RUN_EVIDENCE_MISMATCH",
        "CONFORMANCE_RUN_STARTED",
        "CONFORMANCE_RUN_COMPLETED",
        # Additive (LA-L7 class; spec/19 PSO-2 FSM side-effects, exact spec
        # names): the netting engine writes these through the shared ledger
        # audit path (errata N19-11).
        "NETTING_COMPLETED",
        "NETTING_INTEGRITY_FAILURE",
        "WINDOW_PARTICIPANT_SUSPENDED",
        "WINDOW_UNWOUND",
    }
)

ACTOR_TYPES: frozenset[str] = frozenset(
    {"OPERATOR", "CUSTOMER", "MERCHANT", "SERVICE", "BB_INSPECTOR"}
)


# ---------------------------------------------------------------------------
# Input specs (binding signatures, spec/03 API surface)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PostingSpec:
    account_id: str
    side: str  # "DEBIT" | "CREDIT"
    amount_minor: int  # paisa; > 0; NEVER float
    currency: str = "BDT"


@dataclass(frozen=True, slots=True)
class JournalEntrySpec:
    reference_id: str
    reference_type: str
    entry_type: str
    description: str
    produced_by: str
    idempotency_key: str
    postings: tuple[PostingSpec, ...]


@dataclass(frozen=True, slots=True)
class AuditEventSpec:
    """One audit_events row to append (spec/03 audit journal).

    ``payload`` may contain PII — the ledger redacts it at write
    (PII-redaction-at-write; the redacted form is what gets hashed/stored).
    """

    event_type: str
    actor_id: str
    actor_type: str
    subject_type: str
    subject_id: str
    from_state: str | None = None
    to_state: str | None = None
    payload: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Stored rows (identical shape across the in-memory and Postgres stores)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AccountRow:
    account_id: str
    account_type: str
    account_subtype: str
    currency: str
    owner_id: str | None
    owner_type: str | None
    parent_id: str | None
    purpose_tag: str | None
    is_system: bool
    created_at: datetime
    closed_at: datetime | None = None
    schema_version: int = 1


@dataclass(frozen=True, slots=True)
class JournalEntryRow:
    entry_id: str
    entry_index: int  # store-assigned, monotonically increasing
    reference_id: str
    reference_type: str
    entry_type: str
    description: str
    produced_by: str
    produced_at: datetime
    idempotency_key: str
    schema_version: int = 1


@dataclass(frozen=True, slots=True)
class PostingRow:
    posting_id: str
    entry_id: str
    account_id: str
    side: str
    amount_minor: int
    currency: str
    produced_at: datetime
    schema_version: int = 1


@dataclass(frozen=True, slots=True)
class AccountHoldRow:
    hold_id: str
    account_id: str
    hold_reserve_account_id: str
    source_account_id: str
    reference_id: str
    amount_minor: int
    hold_reason: str
    status: str
    hold_opened_at: datetime
    hold_expires_at: datetime
    hold_closed_at: datetime | None
    open_je_id: str
    close_je_id: str | None = None
    schema_version: int = 1


@dataclass(frozen=True, slots=True)
class ChainEntryRow:
    chain_domain: str  # 'MONEY' | 'AUDIT'
    chain_index: int  # contiguous within domain; genesis = 0
    entry_type: str
    payload_hash: str
    payload_pointer: str
    prev_chain_hash: str
    amount_minor_sum: int
    chain_hash: str
    is_checkpoint: bool
    checkpoint_sequence_in_domain: int | None
    checkpoint_sig: str | None
    checkpoint_public_key_b64: str | None
    producer: str
    produced_at: datetime
    schema_version: int = 1


@dataclass(frozen=True, slots=True)
class CheckpointRow:
    checkpoint_id: str
    chain_domain: str
    checkpoint_sequence_in_domain: int
    chain_index_from: int
    chain_index_to: int
    chain_hash_at_checkpoint: str
    checkpoint_sig: str
    checkpoint_public_key_b64: str
    entries_in_segment: int
    signed_at: datetime
    schema_version: int = 1


@dataclass(frozen=True, slots=True)
class OutboxRow:
    event_id: str
    topic: str
    event_type: str
    subject_type: str
    subject_id: str
    payload_json: str  # canonical_json of the event envelope payload
    producer: str
    occurred_at: datetime
    published: bool = False
    schema_version: int = 1


@dataclass(frozen=True, slots=True)
class AuditEventRow:
    """audit_events row — PII-redacted at write, chained in the AUDIT domain.

    ``payload_json`` holds the REDACTED payload (canonical JSON text); it
    stands in for the spec/03 ``audit_payload_store`` table until the
    object-store uploader exists (errata row LA-L8).
    """

    event_id: str
    entry_index: int  # store-assigned
    event_type: str
    actor_id: str  # PII-redacted at write
    actor_type: str
    subject_type: str
    subject_id: str
    from_state: str | None
    to_state: str | None
    payload_hash: str  # sha256_canonical(redacted_payload)
    payload_pointer: str
    payload_json: str  # canonical JSON of the redacted payload
    prev_chain_hash: str
    chain_hash: str
    pii_redacted: bool
    occurred_at: datetime
    schema_version: int = 1


# ---------------------------------------------------------------------------
# verify_chain result (spec/03 ChainVerification FSM)
# ---------------------------------------------------------------------------

VERIFY_COMPLETED = "COMPLETED"
VERIFY_FAILED_BROKEN_CHAIN = "FAILED_BROKEN_CHAIN"
VERIFY_FAILED_INVALID_CHECKPOINT_SIG = "FAILED_INVALID_CHECKPOINT_SIG"
VERIFY_FAILED_ERROR = "FAILED_ERROR"


@dataclass(frozen=True, slots=True)
class ChainVerificationResult:
    status: str
    entries_checked: int = 0
    first_broken_index: int | None = None
    checkpoints_verified: int = 0
    detail: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == VERIFY_COMPLETED


@dataclass(frozen=True, slots=True)
class TrialBalance:
    total_debit_minor: int
    total_credit_minor: int

    @property
    def balanced(self) -> bool:
        return self.total_debit_minor == self.total_credit_minor


@dataclass(frozen=True, slots=True)
class WriteGateState:
    engaged: bool
    reason: str | None = None
    engaged_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ChainTip:
    chain_index: int
    chain_hash: str


@dataclass(frozen=True, slots=True)
class AccountSpec:
    """Input for create_account; account_id is derived content-addressed
    from {owner_id, account_subtype, currency, purpose_tag} (spec/03 DDL)."""

    account_type: str
    account_subtype: str
    owner_id: str | None = None
    owner_type: str | None = None
    parent_id: str | None = None
    purpose_tag: str | None = None
    is_system: bool = False
    currency: str = "BDT"

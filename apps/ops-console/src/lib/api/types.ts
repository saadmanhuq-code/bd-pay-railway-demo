// Typed models for every resource the ops console renders.
// Sources: spec/15-ops-console-and-portal.md, spec/00-conventions.md §4,
// spec/10 §API surface (connector registry).

// ---------------------------------------------------------------------------
// Conventions §4 — error envelope + cursor pagination
// ---------------------------------------------------------------------------

export type ErrorType =
  | "invalid_request"
  | "authentication"
  | "authorization"
  | "rate_limit"
  | "idempotency_conflict"
  | "limit_exceeded"
  | "sanctions_block"
  | "aml_block"
  | "connector_error"
  | "conflict"
  | "not_found"
  | "internal";

export interface ErrorEnvelope {
  error: {
    type: ErrorType;
    code: string;
    message: string;
    request_id: string;
    doc_url: string;
  };
}

export interface Paginated<T> {
  data: T[];
  next_cursor: string | null;
}

// ---------------------------------------------------------------------------
// ApprovalRequest — the four-eyes spine (spec/15 §State machines 1)
// ---------------------------------------------------------------------------

export type ApprovalState =
  | "PENDING_SECOND_APPROVER"
  | "APPROVED"
  | "APPROVED_EXECUTION_FAILED"
  | "REJECTED"
  | "EXPIRED"
  | "WITHDRAWN";

export const APPROVAL_STATES: ApprovalState[] = [
  "PENDING_SECOND_APPROVER",
  "APPROVED",
  "APPROVED_EXECUTION_FAILED",
  "REJECTED",
  "EXPIRED",
  "WITHDRAWN",
];

export type ApprovalActionType =
  | "refund_over_threshold"
  | "merchant_activation"
  | "merchant_edd_clearance"
  | "kyc_manual_review"
  | "settlement_batch_retry_large"
  | "settlement_release_override"
  | "mdr_fee_rule_change"
  | "aml_case_close"
  | "str_withdrawal"
  | "sanctions_clear_or_unfreeze"
  | "camlco_freeze"
  | "camlco_unfreeze"
  | "rule_pack_promotion"
  | "signing_key_ceremony"
  | "connector_mode_to_production"
  | "connector_manual_result"
  | "rail_dictionary_activation"
  | "npsb_session_manual_action"
  | "compensation_resolution"
  | "dispute_resolution_money"
  | "bulk_disbursement_release"
  | "operator_create_or_role_change"
  | "bb_incident_report_release";

export const APPROVAL_ACTION_TYPES: ApprovalActionType[] = [
  "refund_over_threshold",
  "merchant_activation",
  "merchant_edd_clearance",
  "kyc_manual_review",
  "settlement_batch_retry_large",
  "settlement_release_override",
  "mdr_fee_rule_change",
  "aml_case_close",
  "str_withdrawal",
  "sanctions_clear_or_unfreeze",
  "camlco_freeze",
  "camlco_unfreeze",
  "rule_pack_promotion",
  "signing_key_ceremony",
  "connector_mode_to_production",
  "connector_manual_result",
  "rail_dictionary_activation",
  "npsb_session_manual_action",
  "compensation_resolution",
  "dispute_resolution_money",
  "bulk_disbursement_release",
  "operator_create_or_role_change",
  "bb_incident_report_release",
];

export interface ApprovalRequest {
  approval_request_id: string;
  action_type: ApprovalActionType;
  subject_type: string;
  subject_id: string;
  payload: Record<string, unknown>;
  payload_hash: string;
  reason: string;
  state: ApprovalState;
  initiator_id: string;
  initiator_name: string;
  approver_id: string | null;
  approver_name: string | null;
  decision_reason: string | null;
  threshold_minor: string | null; // BIGINT-safe: serialized as string
  expires_at: string;
  decided_at: string | null;
  executed_at: string | null;
  created_at: string;
  updated_at: string;
  schema_version: number;
}

// ---------------------------------------------------------------------------
// OpsCase — unified case management (spec/15 §State machines 2)
// ---------------------------------------------------------------------------

export type OpsCaseType =
  | "AML_ALERT"
  | "RECON_EXCEPTION"
  | "DISPUTE"
  | "COMPENSATION"
  | "RTGS_MANUAL"
  | "WEBHOOK_POISON"
  | "SANCTIONS_MANUAL_ENTRY"
  | "INCIDENT"
  | "DATA_BREACH"
  | "OTHER";

export const OPS_CASE_TYPES: OpsCaseType[] = [
  "AML_ALERT",
  "RECON_EXCEPTION",
  "DISPUTE",
  "COMPENSATION",
  "RTGS_MANUAL",
  "WEBHOOK_POISON",
  "SANCTIONS_MANUAL_ENTRY",
  "INCIDENT",
  "DATA_BREACH",
  "OTHER",
];

export type OpsCaseState =
  | "OPEN"
  | "IN_PROGRESS"
  | "WAITING_EXTERNAL"
  | "RESOLVED"
  | "CANCELLED";

export const OPS_CASE_STATES: OpsCaseState[] = [
  "OPEN",
  "IN_PROGRESS",
  "WAITING_EXTERNAL",
  "RESOLVED",
  "CANCELLED",
];

export type CasePriority = "P0" | "P1" | "P2" | "P3";

export interface EvidenceItem {
  pointer: string; // object-store pointer
  sha256: string;
  label: string;
  added_by: string;
  added_at: string;
}

export interface CaseComment {
  author_id: string;
  author_name: string;
  body: string;
  at: string;
}

export interface OpsCase {
  case_id: string;
  case_type: OpsCaseType;
  state: OpsCaseState;
  priority: CasePriority;
  subject_type: string;
  subject_id: string;
  assignee_id: string | null;
  assignee_name: string | null;
  sla_due_at: string;
  sla_breached: boolean;
  evidence: EvidenceItem[];
  comments: CaseComment[];
  resolution_note: string | null;
  opened_at: string;
  resolved_at: string | null;
  updated_at: string;
  schema_version: number;
}

// ---------------------------------------------------------------------------
// Dispute (spec/15 §State machines 3)
// ---------------------------------------------------------------------------

export type DisputeState =
  | "OPEN"
  | "EVIDENCE_PENDING"
  | "UNDER_REVIEW"
  | "RESOLVED_MERCHANT"
  | "RESOLVED_CUSTOMER"
  | "CHARGEBACK_FILED"
  | "CHARGEBACK_CLOSED"
  | "WITHDRAWN";

export const DISPUTE_STATES: DisputeState[] = [
  "OPEN",
  "EVIDENCE_PENDING",
  "UNDER_REVIEW",
  "RESOLVED_MERCHANT",
  "RESOLVED_CUSTOMER",
  "CHARGEBACK_FILED",
  "CHARGEBACK_CLOSED",
  "WITHDRAWN",
];

export type PaymentMethod =
  | "BKASH"
  | "NAGAD"
  | "CARD"
  | "NPSB_IBFT"
  | "BANGLA_QR";

export type DisputeReasonCode =
  | "goods_not_received"
  | "unauthorized"
  | "duplicate"
  | "amount_wrong"
  | "other";

export interface Dispute {
  dispute_id: string;
  payment_intent_id: string;
  merchant_id: string;
  merchant_name: string;
  opened_by: string;
  state: DisputeState;
  amount_minor: string; // BIGINT-safe
  currency: "BDT";
  method: PaymentMethod;
  reason_code: DisputeReasonCode;
  evidence_deadline_at: string | null;
  no_merchant_evidence: boolean;
  evidence: EvidenceItem[];
  refund_id: string | null;
  scheme_case_ref: string | null;
  opened_at: string;
  resolved_at: string | null;
  updated_at: string;
  schema_version: number;
}

// ---------------------------------------------------------------------------
// CompensationQueueItem (spec/15 §State machines 4 — REVERSAL_FAILED queue)
// ---------------------------------------------------------------------------

export type CompensationState =
  | "QUEUED"
  | "INVESTIGATING"
  | "PENDING_APPROVAL"
  | "RESOLVED"
  | "WRITTEN_OFF";

export const COMPENSATION_STATES: CompensationState[] = [
  "QUEUED",
  "INVESTIGATING",
  "PENDING_APPROVAL",
  "RESOLVED",
  "WRITTEN_OFF",
];

export type CompensationResolutionKind =
  | "RAIL_REVERSED_CONFIRMED"
  | "MANUAL_RAIL_REFUND"
  | "LEDGER_ADJUSTMENT"
  | "WRITE_OFF";

export const COMPENSATION_RESOLUTION_KINDS: CompensationResolutionKind[] = [
  "RAIL_REVERSED_CONFIRMED",
  "MANUAL_RAIL_REFUND",
  "LEDGER_ADJUSTMENT",
  "WRITE_OFF",
];

export interface RunbookStep {
  step: number;
  key: string;
  done: boolean;
  done_by: string | null;
  done_at: string | null;
}

export interface CompensationQueueItem {
  compensation_id: string;
  payment_intent_id: string;
  payment_attempt_id: string;
  connector_id: string;
  connector_ref: string;
  amount_minor: string; // BIGINT-safe
  currency: "BDT";
  state: CompensationState;
  resolution_kind: CompensationResolutionKind | null;
  approval_request_id: string | null;
  runbook_checklist: RunbookStep[];
  evidence: EvidenceItem[];
  queued_at: string;
  resolved_at: string | null;
  updated_at: string;
  schema_version: number;
}

// ---------------------------------------------------------------------------
// OperatorAccount + IAM (spec/15 §Role matrix, credential micro-FSM)
// ---------------------------------------------------------------------------

export type OperatorRole =
  | "operator"
  | "compliance"
  | "CAMLCO"
  | "finance"
  | "admin"
  | "auditor"
  | "bfiu_investigator";

export const OPERATOR_ROLES: OperatorRole[] = [
  "operator",
  "compliance",
  "CAMLCO",
  "finance",
  "admin",
  "auditor",
  "bfiu_investigator",
];

export type CredentialState = "ACTIVE" | "LOCKED" | "DISABLED";

export interface Operator {
  operator_id: string;
  email: string;
  display_name: string;
  roles: OperatorRole[];
  acl_denies: string[];
  credential_state: CredentialState;
  failed_attempts: number;
  locked_until: string | null;
  password_set_at: string;
  created_at: string;
  updated_at: string;
  schema_version: number;
}

export interface Session {
  operator: Operator;
  issued_at: string;
  absolute_expires_at: string;
}

// ---------------------------------------------------------------------------
// ConnectorRegistration (spec/10 §API surface — registry read)
// ---------------------------------------------------------------------------

export type ConnectorMode =
  | "MANUAL_LOCAL"
  | "SIMULATOR"
  | "SANDBOX"
  | "PRODUCTION"
  | "DISABLED";

export const CONNECTOR_MODES: ConnectorMode[] = [
  "MANUAL_LOCAL",
  "SIMULATOR",
  "SANDBOX",
  "PRODUCTION",
  "DISABLED",
];

export type HealthStatus = "HEALTHY" | "DEGRADED" | "UNHEALTHY";
export type CircuitState = "CLOSED" | "OPEN" | "HALF_OPEN";
export type CertificationStatus = "PASSED" | "FAILED" | "RUNNING" | "NEVER_RUN";

export interface CertificationRun {
  certification_run_id: string;
  suite: "SIMULATOR" | "SANDBOX";
  status: CertificationStatus;
  started_at: string;
  finished_at: string | null;
  cases_passed: number;
  cases_failed: number;
}

export interface ConnectorRegistration {
  registration_id: string;
  connector_id: string;
  display_name: string;
  protocol: "PAYMENT" | "RAIL" | "COMPLIANCE" | "NOTIFICATION";
  capabilities: string[];
  supported_methods: string[];
  active_mode: ConnectorMode;
  adapter_version: string;
  sdk_version: number;
  enabled: boolean;
  health_status: HealthStatus;
  circuit_state: CircuitState;
  last_health_at: string;
  last_certified_at: string | null;
  certification_status: CertificationStatus;
  certification_history: CertificationRun[];
  config: {
    base_url: string;
    timeout_ms: number;
    credential_refs: string[]; // OpenBao refs only — never secret values
    webhook_verify_ref: string | null;
  };
  created_at: string;
}

// ---------------------------------------------------------------------------
// Dashboards (spec/15 §Dashboards & exports)
// ---------------------------------------------------------------------------

export interface TcsaDashboard {
  as_of: string; // snapshot timestamp — 60s cadence, drives the data-age badge
  required_minor: string;
  available_minor: string;
  coverage_bps: number; // 10000 = exactly covered
  shortfall_minor: string;
  trend: { at: string; coverage_bps: number }[];
  open_compensation_over_attestation_threshold: number;
}

export interface SettlementDashboard {
  as_of: string;
  instructions_by_state: { state: string; count: number; amount_minor: string }[];
  open_batches: number;
  earliest_release_at: string;
  latest_release_at: string;
  working_day_deadline_breaches: number;
}

export interface ConnectorHealthCell {
  connector_id: string;
  display_name: string;
  active_mode: ConnectorMode;
  health_status: HealthStatus;
  circuit_state: CircuitState;
  last_health_at: string;
}

export interface ConnectorsDashboard {
  as_of: string;
  connectors: ConnectorHealthCell[];
}

export interface AmlDashboard {
  as_of: string;
  alert_queue_depth: number;
  alert_age_histogram: { bucket: string; count: number }[];
  str_queue: { str_id: string; subject_id: string; state: string; created_at: string }[];
  goaml_filings: { filing_id: string; state: string; submitted_at: string | null }[];
  sanctions_feed_age_seconds: number;
  sanctions_feed_stale: boolean;
}

export interface SlaDashboard {
  as_of: string;
  route_groups: {
    group: string;
    p50_ms: number;
    p95_ms: number;
    p99_ms: number;
  }[];
}

// ---------------------------------------------------------------------------
// Ledger viewer (read-only)
// ---------------------------------------------------------------------------

export interface JournalPosting {
  posting_id: string;
  account: string;
  side: "DEBIT" | "CREDIT";
  amount_minor: string;
  currency: "BDT";
}

export interface JournalEntry {
  journal_id: string;
  description: string;
  subject_type: string;
  subject_id: string;
  posted_at: string;
  postings: JournalPosting[];
}

export interface LedgerChainEntry {
  chain_seq: number;
  journal_id: string;
  entry_hash: string;
  prev_hash: string;
  chained_at: string;
}

export interface ChainVerifyStatus {
  status: "VERIFIED" | "FAILED";
  last_verified_at: string;
  entries_checked: number;
  first_bad_seq: number | null;
}

// ---------------------------------------------------------------------------
// ReconciliationException (spec/04, rendered here)
// ---------------------------------------------------------------------------

export type ReconExceptionState = "OPEN" | "IN_REVIEW" | "RESOLVED";
export type ReconExceptionKind =
  | "MISSING_AT_RAIL"
  | "MISSING_AT_LEDGER"
  | "AMOUNT_MISMATCH"
  | "DUPLICATE_AT_RAIL"
  | "STATE_MISMATCH";

export interface ReconciliationException {
  exception_id: string;
  recon_file_id: string;
  connector_id: string;
  kind: ReconExceptionKind;
  state: ReconExceptionState;
  ledger_amount_minor: string | null;
  rail_amount_minor: string | null;
  connector_ref: string | null;
  case_id: string | null;
  detected_at: string;
  resolved_at: string | null;
}

// ---------------------------------------------------------------------------
// Exports / deterministic reports (spec/15 §Dashboards & exports)
// ---------------------------------------------------------------------------

export interface ReportItem {
  report_id: string;
  kind:
    | "SETTLEMENT_FILE"
    | "RECON_REPORT"
    | "CERTIFICATION_REPORT"
    | "BB_AUDIT_EXPORT"
    | "BFIU_EVIDENCE_PACK";
  label: string;
  generated_at: string;
  size_bytes: number;
  sha256: string; // matches the X-BDPay-Content-Sha256 header on download
}

// ---------------------------------------------------------------------------
// Mutation request/response shapes
// ---------------------------------------------------------------------------

export interface LoginResult {
  operator: Operator;
  locked_until?: string | null;
}

export interface ModeChangeResult {
  approval_request_id?: string;
  state?: ApprovalState;
  active_mode?: ConnectorMode;
}

// ---------------------------------------------------------------------------
// Sandbox live demo flow (gateway GET /v1/sandbox/demo/fullflow)
// ---------------------------------------------------------------------------

export interface SandboxDemoPosting {
  posting_id: string | null;
  account_id: string | null;
  side: string | null;
  amount_minor: number | null;
  currency: string | null;
  produced_at: string | null;
  schema_version: number | null;
}

export interface SandboxDemoLedgerEntry {
  entry_id: string;
  entry_index: number | null;
  reference_id: string | null;
  reference_type: string | null;
  entry_type: string | null;
  description: string | null;
  produced_by: string | null;
  produced_at: string | null;
  idempotency_key: string | null;
  schema_version: number | null;
  postings: SandboxDemoPosting[];
  chain: Record<string, unknown> | null;
}

export interface SandboxDemoFullflow {
  seed_id: string | null;
  mode: string;
  auth: {
    kind: string;
    principal_id: string;
    merchant_id: string | null;
    operator_id: string | null;
    customer_id: string | null;
  };
  summary: {
    status: string;
    amount_minor: number;
    refunded_minor: number;
    currency: string;
    as_of: string;
  };
  merchant: Record<string, unknown>;
  customer: Record<string, unknown> | null;
  intent: Record<string, unknown> & {
    payment_intent_id: string;
    merchant_id: string;
    customer_id?: string | null;
    amount_minor: number;
    currency: string;
    status: string;
    method?: string;
    created_at?: string;
  };
  attempts: Paginated<Record<string, unknown>>;
  refunds: Paginated<Record<string, unknown>>;
  ledger: {
    available: boolean;
    entries: SandboxDemoLedgerEntry[];
    trial_balance?: Record<string, unknown> | null;
    chain_verify?: Record<string, unknown> | null;
  };
}

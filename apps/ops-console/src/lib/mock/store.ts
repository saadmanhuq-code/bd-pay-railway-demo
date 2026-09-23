// In-app deterministic mock server state. All seed data derives from fixed
// constants — no Math.random / Date.now anywhere in the data path. Mutations
// are FSM-consistent and held in module scope so approve/reject flows are
// demo-able within a server process.

import type {
  AmlDashboard,
  ApprovalActionType,
  ApprovalRequest,
  ApprovalState,
  ChainVerifyStatus,
  CompensationQueueItem,
  CompensationResolutionKind,
  ConnectorMode,
  ConnectorRegistration,
  ConnectorsDashboard,
  Dispute,
  ErrorEnvelope,
  ErrorType,
  EvidenceItem,
  JournalEntry,
  LedgerChainEntry,
  OpsCase,
  Operator,
  ReconciliationException,
  ReportItem,
  RunbookStep,
  SettlementDashboard,
  SlaDashboard,
  TcsaDashboard,
} from "@/lib/api/types";
import { detHex } from "./detHex";

export { detHex };

// ---------------------------------------------------------------------------
// Deterministic helpers
// ---------------------------------------------------------------------------

function id(prefix: string, key: string): string {
  return `${prefix}_${detHex(key)}`;
}

function sha(key: string): string {
  return detHex(`sha256:${key}`, 64);
}

// Seed epoch: anchored once per process to the current UTC hour so relative
// offsets stay deterministic inside a replica (IDs, trend points, row times).
// AgeBadge, session clocks, and dashboard row freshness (last_health_at,
// settlement release windows) use wall-clock helpers below — a long-lived
// Railway replica must not paint multi-hour "5h old", mint already-expired
// sessions, or show yesterday's last-health next to a "2m old" AgeBadge.
function demoSnapshotAt(): string {
  const d = new Date();
  d.setUTCMinutes(0, 0, 0);
  return d.toISOString().replace(".000Z", "Z");
}
export const SNAPSHOT_AT = demoSnapshotAt();
const EPOCH_MS = Date.parse(SNAPSHOT_AT);

function tsAt(offsetMinutes: number): string {
  return new Date(EPOCH_MS + offsetMinutes * 60_000).toISOString().replace(".000Z", "Z");
}

/** AgeBadge as_of — N minutes before wall now (independent of seed EPOCH). */
export function asOfMinutesAgo(minutes: number): string {
  return new Date(Date.now() - minutes * 60_000).toISOString().replace(".000Z", "Z");
}

/** Session issued/expiry window for mock auth responses. */
export function sessionWindow(hoursValid = 8): { issued_at: string; absolute_expires_at: string } {
  const issued = Date.now();
  return {
    issued_at: new Date(issued).toISOString().replace(".000Z", "Z"),
    absolute_expires_at: new Date(issued + hoursValid * 3_600_000).toISOString().replace(".000Z", "Z"),
  };
}

// Deterministic mutation clock: base constant + monotonic counter.
let mutationSeq = 0;
function mutationTs(): string {
  mutationSeq += 1;
  return new Date(EPOCH_MS + 30 * 60_000 + mutationSeq * 1000).toISOString().replace(".000Z", "Z");
}

const STATUS_BY_TYPE: Record<ErrorType, number> = {
  invalid_request: 400,
  authentication: 401,
  authorization: 403,
  sanctions_block: 403,
  aml_block: 403,
  not_found: 404,
  conflict: 409,
  idempotency_conflict: 409,
  limit_exceeded: 422,
  rate_limit: 429,
  connector_error: 502,
  internal: 500,
};

export interface MockResult<T> {
  status: number;
  body: T | ErrorEnvelope;
}

export function mockError(type: ErrorType, code: string, message: string): MockResult<never> {
  mutationSeq += 1;
  const envelope: ErrorEnvelope = {
    error: {
      type,
      code,
      message,
      request_id: `req_${detHex(`req:${mutationSeq}`)}`,
      doc_url: `https://docs.bdpay.example/errors/${code}`,
    },
  };
  return { status: STATUS_BY_TYPE[type], body: envelope };
}

function ok<T>(body: T): MockResult<T> {
  return { status: 200, body };
}

// ---------------------------------------------------------------------------
// Operators
// ---------------------------------------------------------------------------

// The logged-in console user is always this fixed demo operator so that
// initiator≠approver behavior is demonstrable against the fixtures.
export const DEMO_OPERATOR_ID = id("oper", "you@bdpay.example");

function makeOperator(
  key: string,
  email: string,
  name: string,
  roles: Operator["roles"],
  credentialState: Operator["credential_state"] = "ACTIVE",
  lockedUntil: string | null = null,
): Operator {
  return {
    operator_id: id("oper", key),
    email,
    display_name: name,
    roles,
    acl_denies: [],
    credential_state: credentialState,
    failed_attempts: credentialState === "LOCKED" ? 3 : 0,
    locked_until: lockedUntil,
    password_set_at: tsAt(-60 * 24 * 30),
    created_at: tsAt(-60 * 24 * 200),
    updated_at: tsAt(-60),
    schema_version: 1,
  };
}

const operators: Operator[] = [
  makeOperator("you@bdpay.example", "you@bdpay.example", "Console Demo Operator", [
    "operator",
    "compliance",
    "CAMLCO",
    "finance",
    "admin",
  ]),
  makeOperator("farhana@bdpay.example", "farhana.rahman@bdpay.example", "Farhana Rahman", ["finance"]),
  makeOperator("tanvir@bdpay.example", "tanvir.ahmed@bdpay.example", "Tanvir Ahmed", ["operator"]),
  makeOperator("nusrat@bdpay.example", "nusrat.jahan@bdpay.example", "Nusrat Jahan", ["compliance"]),
  makeOperator("kamal@bdpay.example", "kamal.hossain@bdpay.example", "Kamal Hossain", ["CAMLCO"]),
  makeOperator("shahid@bdpay.example", "shahid.islam@bdpay.example", "Shahid Islam", ["admin"]),
  makeOperator("rumana@bdpay.example", "rumana.akter@bdpay.example", "Rumana Akter", ["auditor"]),
  makeOperator("bfiu@bfiu.gov.example", "investigator@bfiu.gov.example", "BFIU Investigator", ["bfiu_investigator"]),
  makeOperator(
    "locked@bdpay.example",
    "locked.account@bdpay.example",
    "Mahmud Karim",
    ["operator"],
    "LOCKED",
    tsAt(25),
  ),
  makeOperator("former@bdpay.example", "former.staff@bdpay.example", "Former Staff", ["operator"], "DISABLED"),
];

function operName(operId: string | null): string | null {
  if (operId === null) return null;
  const found = operators.find((o) => o.operator_id === operId);
  return found ? found.display_name : operId;
}

function oper(key: string): string {
  return id("oper", key);
}

// ---------------------------------------------------------------------------
// Approval requests
// ---------------------------------------------------------------------------

function makeApproval(
  key: string,
  actionType: ApprovalActionType,
  subjectType: string,
  subjectId: string,
  payload: Record<string, unknown>,
  reason: string,
  state: ApprovalState,
  initiatorKey: string,
  openedMinutesAgo: number,
  approverKey: string | null = null,
  thresholdMinor: string | null = null,
): ApprovalRequest {
  const initiatorId = oper(initiatorKey);
  const approverId = approverKey === null ? null : oper(approverKey);
  const decided = state === "REJECTED" || state === "APPROVED" || state === "APPROVED_EXECUTION_FAILED";
  return {
    approval_request_id: id("appr", key),
    action_type: actionType,
    subject_type: subjectType,
    subject_id: subjectId,
    payload,
    payload_hash: sha(`appr-payload:${key}`),
    reason,
    state,
    initiator_id: initiatorId,
    initiator_name: operName(initiatorId) ?? initiatorId,
    approver_id: approverId,
    approver_name: operName(approverId),
    decision_reason: decided ? `Reviewed against policy — ${state.toLowerCase()}` : null,
    threshold_minor: thresholdMinor,
    expires_at: tsAt(-openedMinutesAgo + 24 * 60),
    decided_at: decided ? tsAt(-openedMinutesAgo + 40) : null,
    executed_at: state === "APPROVED" ? tsAt(-openedMinutesAgo + 41) : null,
    created_at: tsAt(-openedMinutesAgo),
    updated_at: tsAt(-openedMinutesAgo + (decided ? 40 : 0)),
    schema_version: 1,
  };
}

const approvals: ApprovalRequest[] = [
  makeApproval(
    "appr-1",
    "refund_over_threshold",
    "Refund",
    "rfnd_" + detHex("rfnd-1"),
    { amount_minor: "7500000", currency: "BDT", payment_intent_id: "pi_" + detHex("pi-1") },
    "Customer double-charged on bKash; merchant confirmed duplicate.",
    "PENDING_SECOND_APPROVER",
    "tanvir@bdpay.example",
    95,
    null,
    "5000000",
  ),
  makeApproval(
    "appr-2",
    "connector_mode_to_production",
    "ConnectorRegistration",
    "bkash_pgw_v2",
    { target_mode: "PRODUCTION", certification_run_id: "certr_" + detHex("certr-1") },
    "Sandbox certification suite passed; BB pilot approval on file.",
    "PENDING_SECOND_APPROVER",
    "tanvir@bdpay.example",
    240,
  ),
  makeApproval(
    "appr-3",
    "compensation_resolution",
    "CompensationQueueItem",
    id("comp", "comp-3"),
    { resolution_kind: "LEDGER_ADJUSTMENT", amount_minor: "1250000" },
    "Rail statement shows partial reversal; adjust ledger to recorded truth.",
    "PENDING_SECOND_APPROVER",
    "farhana@bdpay.example",
    180,
  ),
  makeApproval(
    "appr-4",
    "camlco_freeze",
    "Merchant",
    "mrch_" + detHex("mrch-4"),
    { freeze_scope: "ALL_OUTBOUND", basis: "ATA_2009_S20" },
    "Sanctions screening near-match plus structuring pattern; freeze applied on initiation.",
    "PENDING_SECOND_APPROVER",
    "kamal@bdpay.example",
    30,
  ),
  makeApproval(
    "appr-5",
    "settlement_release_override",
    "SettlementInstruction",
    "sins_" + detHex("sins-5"),
    { adjustment_minor: "98000", direction: "CREDIT_MERCHANT" },
    "Recon ledger adjustment above BDT 500 — fee rounding correction.",
    "PENDING_SECOND_APPROVER",
    "you@bdpay.example",
    55,
    null,
    "50000",
  ),
  makeApproval(
    "appr-6",
    "operator_create_or_role_change",
    "OperatorAccount",
    oper("nusrat@bdpay.example"),
    { add_roles: ["CAMLCO"], remove_roles: [] },
    "Deputy CAMLCO coverage for out-of-hours roster.",
    "PENDING_SECOND_APPROVER",
    "you@bdpay.example",
    20,
  ),
  makeApproval(
    "appr-7",
    "aml_case_close",
    "AmlAlert",
    "aml_" + detHex("aml-7"),
    { disposition: "FALSE_POSITIVE" },
    "Velocity alert explained by Eid salary disbursement pattern.",
    "APPROVED",
    "nusrat@bdpay.example",
    60 * 26,
    "kamal@bdpay.example",
  ),
  makeApproval(
    "appr-8",
    "merchant_activation",
    "Merchant",
    "mrch_" + detHex("mrch-8"),
    { kyb_record_id: "kyb_" + detHex("kyb-8") },
    "KYB complete; trade license and UBO chain verified.",
    "REJECTED",
    "tanvir@bdpay.example",
    60 * 30,
    "nusrat@bdpay.example",
  ),
  makeApproval(
    "appr-9",
    "settlement_batch_retry_large",
    "SettlementBatch",
    "sbat_" + detHex("sbat-9"),
    { amount_minor: "62000000", retry_attempt: 2 },
    "BEFTN return on batch; retry after dictionary correction.",
    "APPROVED_EXECUTION_FAILED",
    "farhana@bdpay.example",
    60 * 7,
    "you@bdpay.example",
    "50000000",
  ),
  makeApproval(
    "appr-10",
    "str_withdrawal",
    "StrReport",
    "str_" + detHex("str-10"),
    { withdrawal_basis: "DUPLICATE_FILING" },
    "Duplicate STR detected before goAML submission.",
    "EXPIRED",
    "kamal@bdpay.example",
    60 * 49,
  ),
  makeApproval(
    "appr-11",
    "mdr_fee_rule_change",
    "FeeRule",
    "feer_" + detHex("feer-11"),
    { mdr_bps_old: 185, mdr_bps_new: 175, merchant_tier: "T2" },
    "Withdrawn — superseded by quarterly fee review.",
    "WITHDRAWN",
    "farhana@bdpay.example",
    60 * 50,
  ),
];

// ---------------------------------------------------------------------------
// Ops cases
// ---------------------------------------------------------------------------

function makeCase(
  key: string,
  caseType: OpsCase["case_type"],
  state: OpsCase["state"],
  priority: OpsCase["priority"],
  subjectType: string,
  subjectId: string,
  slaHours: number,
  openedMinutesAgo: number,
  assigneeKey: string | null,
  evidence: EvidenceItem[] = [],
): OpsCase {
  const assigneeId = assigneeKey === null ? null : oper(assigneeKey);
  const slaDue = tsAt(-openedMinutesAgo + slaHours * 60);
  return {
    case_id: id("case", key),
    case_type: caseType,
    state,
    priority,
    subject_type: subjectType,
    subject_id: subjectId,
    assignee_id: assigneeId,
    assignee_name: operName(assigneeId),
    sla_due_at: slaDue,
    sla_breached: Date.parse(slaDue) < EPOCH_MS && state !== "RESOLVED" && state !== "CANCELLED",
    evidence,
    comments:
      state === "OPEN"
        ? []
        : [
            {
              author_id: assigneeId ?? oper("tanvir@bdpay.example"),
              author_name: operName(assigneeId) ?? "Tanvir Ahmed",
              body: "Initial triage complete; subject record cross-checked.",
              at: tsAt(-openedMinutesAgo + 30),
            },
          ],
    resolution_note: state === "RESOLVED" ? "Resolved through owning-service flow." : null,
    opened_at: tsAt(-openedMinutesAgo),
    resolved_at: state === "RESOLVED" ? tsAt(-openedMinutesAgo + 200) : null,
    updated_at: tsAt(-openedMinutesAgo + 30),
    schema_version: 1,
  };
}

function evid(key: string, label: string, byKey: string, minutesAgo: number): EvidenceItem {
  return {
    pointer: `s3://bdpay-evidence/${detHex(`ptr:${key}`, 16)}/${key}.pdf`,
    sha256: sha(`evidence:${key}`),
    label,
    added_by: oper(byKey),
    added_at: tsAt(-minutesAgo),
  };
}

const cases: OpsCase[] = [
  makeCase("case-1", "COMPENSATION", "OPEN", "P0", "CompensationQueueItem", id("comp", "comp-1"), 4, 300, null),
  makeCase("case-2", "INCIDENT", "IN_PROGRESS", "P0", "ConnectorRegistration", "npsb_ibft_v1", 24, 60 * 20, "tanvir@bdpay.example", [
    evid("case-2-e1", "Circuit-open telemetry extract", "tanvir@bdpay.example", 60 * 18),
  ]),
  makeCase("case-3", "AML_ALERT", "IN_PROGRESS", "P1", "AmlAlert", "aml_" + detHex("aml-3"), 48, 60 * 30, "nusrat@bdpay.example"),
  makeCase("case-4", "RECON_EXCEPTION", "OPEN", "P2", "ReconciliationException", id("rexc", "rexc-2"), 24, 60 * 10, null),
  makeCase("case-5", "DISPUTE", "WAITING_EXTERNAL", "P2", "Dispute", id("dspt", "dspt-2"), 24 * 30, 60 * 24 * 4, "tanvir@bdpay.example"),
  makeCase("case-6", "RTGS_MANUAL", "OPEN", "P0", "RtgsMessage", "rtgsm_" + detHex("rtgsm-6"), 2, 200, null),
  makeCase("case-7", "WEBHOOK_POISON", "IN_PROGRESS", "P2", "WebhookInbound", "winb_" + detHex("winb-7"), 24, 60 * 9, "tanvir@bdpay.example"),
  makeCase("case-8", "SANCTIONS_MANUAL_ENTRY", "OPEN", "P1", "SanctionsHit", "snch_" + detHex("snch-8"), 4, 100, null),
  makeCase("case-9", "DATA_BREACH", "IN_PROGRESS", "P0", "AuditEvent", "audt_" + detHex("audt-9"), 72, 60 * 5, "shahid@bdpay.example", [
    evid("case-9-e1", "Access-log forensic snapshot", "shahid@bdpay.example", 60 * 4),
  ]),
  makeCase("case-10", "AML_ALERT", "RESOLVED", "P2", "AmlAlert", "aml_" + detHex("aml-10"), 48, 60 * 80, "nusrat@bdpay.example"),
  makeCase("case-11", "RECON_EXCEPTION", "RESOLVED", "P3", "ReconciliationException", id("rexc", "rexc-5"), 24, 60 * 70, "farhana@bdpay.example"),
  makeCase("case-12", "OTHER", "CANCELLED", "P3", "Merchant", "mrch_" + detHex("mrch-12"), 24 * 7, 60 * 90, null),
];

// ---------------------------------------------------------------------------
// Disputes
// ---------------------------------------------------------------------------

function makeDispute(
  key: string,
  state: Dispute["state"],
  amountMinor: string,
  method: Dispute["method"],
  reason: Dispute["reason_code"],
  openedMinutesAgo: number,
  evidence: EvidenceItem[] = [],
): Dispute {
  return {
    dispute_id: id("dspt", key),
    payment_intent_id: "pi_" + detHex(`pi:${key}`),
    merchant_id: "mrch_" + detHex(`mrch:${key}`),
    merchant_name: ["Dhaka Mart", "Chittagong Traders", "Sylhet Books", "Khulna Foods", "Rajshahi Silk", "Barisal Electronics"][
      Math.abs(key.charCodeAt(key.length - 1)) % 6
    ] as string,
    opened_by: "cust_" + detHex(`cust:${key}`),
    state,
    amount_minor: amountMinor,
    currency: "BDT",
    method,
    reason_code: reason,
    evidence_deadline_at: state === "EVIDENCE_PENDING" ? tsAt(-openedMinutesAgo + 7 * 24 * 60) : null,
    no_merchant_evidence: false,
    evidence,
    refund_id: state === "RESOLVED_CUSTOMER" ? "rfnd_" + detHex(`rfnd:${key}`) : null,
    scheme_case_ref: state === "CHARGEBACK_FILED" || state === "CHARGEBACK_CLOSED" ? `CB-${detHex(`cb:${key}`, 10).toUpperCase()}` : null,
    opened_at: tsAt(-openedMinutesAgo),
    resolved_at: state.startsWith("RESOLVED") || state === "CHARGEBACK_CLOSED" ? tsAt(-openedMinutesAgo + 600) : null,
    updated_at: tsAt(-openedMinutesAgo + 60),
    schema_version: 1,
  };
}

const disputes: Dispute[] = [
  makeDispute("dspt-1", "OPEN", "450000", "BKASH", "goods_not_received", 60 * 6),
  makeDispute("dspt-2", "EVIDENCE_PENDING", "1280000", "CARD", "unauthorized", 60 * 24 * 4),
  makeDispute("dspt-3", "UNDER_REVIEW", "230000", "NAGAD", "duplicate", 60 * 24 * 6, [
    evid("dspt-3-e1", "Merchant delivery proof", "tanvir@bdpay.example", 60 * 24 * 2),
  ]),
  makeDispute("dspt-4", "UNDER_REVIEW", "9900000", "CARD", "amount_wrong", 60 * 24 * 8, [
    evid("dspt-4-e1", "Card statement extract", "tanvir@bdpay.example", 60 * 24 * 3),
  ]),
  makeDispute("dspt-5", "RESOLVED_CUSTOMER", "150000", "BANGLA_QR", "duplicate", 60 * 24 * 20),
  makeDispute("dspt-6", "CHARGEBACK_FILED", "5600000", "CARD", "unauthorized", 60 * 24 * 12),
  makeDispute("dspt-7", "RESOLVED_MERCHANT", "780000", "NPSB_IBFT", "goods_not_received", 60 * 24 * 25),
];

// ---------------------------------------------------------------------------
// Compensation queue
// ---------------------------------------------------------------------------

function runbook(doneThrough: number, byKey: string | null): RunbookStep[] {
  const keys = ["runbook_1", "runbook_2", "runbook_3", "runbook_4", "runbook_5", "runbook_6"];
  return keys.map((k, idx) => ({
    step: idx + 1,
    key: k,
    done: idx < doneThrough,
    done_by: idx < doneThrough && byKey !== null ? oper(byKey) : null,
    done_at: idx < doneThrough ? tsAt(-300 + idx * 15) : null,
  }));
}

function makeComp(
  key: string,
  state: CompensationQueueItem["state"],
  amountMinor: string,
  connectorId: string,
  queuedMinutesAgo: number,
  doneThrough: number,
  resolutionKind: CompensationResolutionKind | null = null,
  approvalKey: string | null = null,
): CompensationQueueItem {
  return {
    compensation_id: id("comp", key),
    payment_intent_id: "pi_" + detHex(`pi:${key}`),
    payment_attempt_id: "pa_" + detHex(`pa:${key}`),
    connector_id: connectorId,
    connector_ref: `${connectorId.toUpperCase().slice(0, 5)}-${detHex(`ref:${key}`, 12).toUpperCase()}`,
    amount_minor: amountMinor,
    currency: "BDT",
    state,
    resolution_kind: resolutionKind,
    approval_request_id: approvalKey === null ? null : id("appr", approvalKey),
    runbook_checklist: runbook(doneThrough, doneThrough > 0 ? "farhana@bdpay.example" : null),
    evidence:
      doneThrough >= 2
        ? [evid(`${key}-e1`, "Connector query_status response", "farhana@bdpay.example", queuedMinutesAgo - 60)]
        : [],
    queued_at: tsAt(-queuedMinutesAgo),
    resolved_at: state === "RESOLVED" || state === "WRITTEN_OFF" ? tsAt(-queuedMinutesAgo + 400) : null,
    updated_at: tsAt(-queuedMinutesAgo + 30),
    schema_version: 1,
  };
}

const compensation: CompensationQueueItem[] = [
  makeComp("comp-1", "QUEUED", "2750000", "bkash_pgw_v2", 300, 0),
  makeComp("comp-2", "INVESTIGATING", "880000", "nagad_pgw_v1", 60 * 10, 2),
  makeComp("comp-3", "PENDING_APPROVAL", "1250000", "npsb_ibft_v1", 60 * 22, 5, "LEDGER_ADJUSTMENT", "appr-3"),
  makeComp("comp-4", "RESOLVED", "460000", "bkash_pgw_v2", 60 * 60, 6, "RAIL_REVERSED_CONFIRMED", "appr-7"),
  makeComp("comp-5", "WRITTEN_OFF", "95000", "card_acquirer_v1", 60 * 90, 6, "WRITE_OFF", "appr-8"),
];

// ---------------------------------------------------------------------------
// Reconciliation exceptions
// ---------------------------------------------------------------------------

function makeRexc(
  key: string,
  kind: ReconciliationException["kind"],
  state: ReconciliationException["state"],
  ledger: string | null,
  rail: string | null,
  connectorId: string,
  detectedMinutesAgo: number,
  caseKey: string | null,
): ReconciliationException {
  return {
    exception_id: id("rexc", key),
    recon_file_id: "rcnf_" + detHex(`rcnf:${key}`),
    connector_id: connectorId,
    kind,
    state,
    ledger_amount_minor: ledger,
    rail_amount_minor: rail,
    connector_ref: `${connectorId.toUpperCase().slice(0, 5)}-${detHex(`rref:${key}`, 12).toUpperCase()}`,
    case_id: caseKey === null ? null : id("case", caseKey),
    detected_at: tsAt(-detectedMinutesAgo),
    resolved_at: state === "RESOLVED" ? tsAt(-detectedMinutesAgo + 500) : null,
  };
}

const reconExceptions: ReconciliationException[] = [
  makeRexc("rexc-1", "AMOUNT_MISMATCH", "OPEN", "1500000", "1485000", "bkash_pgw_v2", 60 * 8, null),
  makeRexc("rexc-2", "MISSING_AT_LEDGER", "IN_REVIEW", null, "320000", "nagad_pgw_v1", 60 * 10, "case-4"),
  makeRexc("rexc-3", "MISSING_AT_RAIL", "OPEN", "5400000", null, "npsb_ibft_v1", 60 * 4, null),
  makeRexc("rexc-4", "DUPLICATE_AT_RAIL", "OPEN", "210000", "420000", "card_acquirer_v1", 60 * 14, null),
  makeRexc("rexc-5", "STATE_MISMATCH", "RESOLVED", "990000", "990000", "bkash_pgw_v2", 60 * 70, "case-11"),
  makeRexc("rexc-6", "AMOUNT_MISMATCH", "OPEN", "75000", "57000", "beftn_v1", 60 * 2, null),
];

// ---------------------------------------------------------------------------
// Connectors
// ---------------------------------------------------------------------------

function makeConnector(
  connectorId: string,
  displayName: string,
  protocol: ConnectorRegistration["protocol"],
  methods: string[],
  mode: ConnectorMode,
  health: ConnectorRegistration["health_status"],
  circuit: ConnectorRegistration["circuit_state"],
  certStatus: ConnectorRegistration["certification_status"],
): ConnectorRegistration {
  return {
    registration_id: "creg_" + detHex(`creg:${connectorId}`),
    connector_id: connectorId,
    display_name: displayName,
    protocol,
    capabilities: ["submit", "query_status", "reverse", "health_check", "webhook", "recon_file"],
    supported_methods: methods,
    active_mode: mode,
    adapter_version: "1.4.0",
    sdk_version: 1,
    enabled: mode !== "DISABLED",
    health_status: health,
    circuit_state: circuit,
    last_health_at: tsAt(-2),
    last_certified_at: certStatus === "NEVER_RUN" ? null : tsAt(-60 * 48),
    certification_status: certStatus,
    certification_history:
      certStatus === "NEVER_RUN"
        ? []
        : [
            {
              certification_run_id: "certr_" + detHex(`certr:${connectorId}:1`),
              suite: "SIMULATOR",
              status: "PASSED",
              started_at: tsAt(-60 * 120),
              finished_at: tsAt(-60 * 119),
              cases_passed: 42,
              cases_failed: 0,
            },
            {
              certification_run_id: "certr_" + detHex(`certr:${connectorId}:2`),
              suite: "SANDBOX",
              status: certStatus === "FAILED" ? "FAILED" : "PASSED",
              started_at: tsAt(-60 * 49),
              finished_at: tsAt(-60 * 48),
              cases_passed: certStatus === "FAILED" ? 39 : 42,
              cases_failed: certStatus === "FAILED" ? 3 : 0,
            },
          ],
    config: {
      base_url: `https://api.sandbox.${connectorId.replace(/_/g, "-")}.example/v1`,
      timeout_ms: 15000,
      credential_refs: [
        `openbao:secret/connectors/${connectorId}/sandbox/app_key`,
        `openbao:secret/connectors/${connectorId}/sandbox/app_secret`,
      ],
      webhook_verify_ref: `openbao:secret/connectors/${connectorId}/sandbox/ipn_verify`,
    },
    created_at: tsAt(-60 * 24 * 100),
  };
}

const connectors: ConnectorRegistration[] = [
  makeConnector("bkash_pgw_v2", "bKash Tokenized Checkout PGW", "PAYMENT", ["BKASH"], "SANDBOX", "HEALTHY", "CLOSED", "PASSED"),
  makeConnector("nagad_pgw_v1", "Nagad Payment Gateway", "PAYMENT", ["NAGAD"], "SANDBOX", "DEGRADED", "HALF_OPEN", "PASSED"),
  makeConnector("npsb_ibft_v1", "NPSB IBFT Rail", "RAIL", ["NPSB_IBFT"], "PRODUCTION", "UNHEALTHY", "OPEN", "PASSED"),
  makeConnector("card_acquirer_v1", "Card Acquirer Gateway", "PAYMENT", ["CARD"], "SANDBOX", "HEALTHY", "CLOSED", "FAILED"),
  makeConnector("beftn_v1", "BEFTN Batch Rail", "RAIL", ["BEFTN"], "PRODUCTION", "HEALTHY", "CLOSED", "PASSED"),
  makeConnector("rtgs_v1", "BD-RTGS Rail", "RAIL", ["RTGS"], "PRODUCTION", "HEALTHY", "CLOSED", "PASSED"),
  makeConnector("rocket_manual_v1", "Rocket (aggregator manual)", "PAYMENT", ["ROCKET"], "MANUAL_LOCAL", "HEALTHY", "CLOSED", "NEVER_RUN"),
];

// ---------------------------------------------------------------------------
// Ledger
// ---------------------------------------------------------------------------

function makeJournal(key: string, description: string, subjectType: string, subjectKey: string, amountMinor: string, postedMinutesAgo: number, debitAcct: string, creditAcct: string): JournalEntry {
  return {
    journal_id: "jrnl_" + detHex(`jrnl:${key}`),
    description,
    subject_type: subjectType,
    subject_id: detHex(`subj:${subjectKey}`, 24),
    posted_at: tsAt(-postedMinutesAgo),
    postings: [
      {
        posting_id: "post_" + detHex(`post:${key}:d`),
        account: debitAcct,
        side: "DEBIT",
        amount_minor: amountMinor,
        currency: "BDT",
      },
      {
        posting_id: "post_" + detHex(`post:${key}:c`),
        account: creditAcct,
        side: "CREDIT",
        amount_minor: amountMinor,
        currency: "BDT",
      },
    ],
  };
}

const journalEntries: JournalEntry[] = [
  makeJournal("j1", "Customer charge captured (bKash)", "PaymentAttempt", "pa-j1", "5000000", 30, "rail_receivable:bkash", "merchant_payable:mrch_a"),
  makeJournal("j2", "MDR fee accrual", "PaymentAttempt", "pa-j1", "92500", 30, "merchant_payable:mrch_a", "fee_income:mdr"),
  makeJournal("j3", "Refund executed", "Refund", "rfnd-j3", "450000", 120, "merchant_payable:mrch_b", "rail_payable:bkash"),
  makeJournal("j4", "Settlement batch release", "SettlementBatch", "sbat-j4", "182000000", 300, "merchant_payable:mrch_a", "settlement_clearing:beftn"),
  makeJournal("j5", "Trust account top-up (TCSA)", "TcsaSnapshot", "tcsa-j5", "250000000", 600, "settlement_clearing:beftn", "trust_account:bb"),
  makeJournal("j6", "Compensation ledger adjustment", "CompensationQueueItem", "comp-4", "460000", 900, "suspense:compensation", "rail_receivable:bkash"),
];

const GENESIS_HASH = "0".repeat(64);

const chainEntries: LedgerChainEntry[] = journalEntries
  .slice()
  .reverse()
  .map((j, idx, arr) => ({
    chain_seq: idx + 1,
    journal_id: j.journal_id,
    entry_hash: sha(`chain:${idx + 1}:${j.journal_id}`),
    prev_hash: idx === 0 ? GENESIS_HASH : sha(`chain:${idx}:${arr[idx - 1]!.journal_id}`),
    chained_at: j.posted_at,
  }));

const chainVerifyStatus: ChainVerifyStatus = {
  status: "VERIFIED",
  last_verified_at: tsAt(-5),
  entries_checked: chainEntries.length,
  first_bad_seq: null,
};

// ---------------------------------------------------------------------------
// Reports
// ---------------------------------------------------------------------------

const reports: ReportItem[] = [
  {
    report_id: "rprt_" + detHex("rprt-1"),
    kind: "SETTLEMENT_FILE",
    label: "BEFTN outward file 2026-06-11 (batch sbat_…)",
    generated_at: tsAt(-60 * 18),
    size_bytes: 48213,
    sha256: sha("report-1"),
  },
  {
    report_id: "rprt_" + detHex("rprt-2"),
    kind: "RECON_REPORT",
    label: "Daily reconciliation report 2026-06-11 (all rails)",
    generated_at: tsAt(-60 * 16),
    size_bytes: 102400,
    sha256: sha("report-2"),
  },
  {
    report_id: "rprt_" + detHex("rprt-3"),
    kind: "CERTIFICATION_REPORT",
    label: "bkash_pgw_v2 SANDBOX certification run",
    generated_at: tsAt(-60 * 48),
    size_bytes: 23987,
    sha256: sha("report-3"),
  },
  {
    report_id: "rprt_" + detHex("rprt-4"),
    kind: "BB_AUDIT_EXPORT",
    label: "BB inspector audit-event export W23",
    generated_at: tsAt(-60 * 30),
    size_bytes: 5242880,
    sha256: sha("report-4"),
  },
  {
    report_id: "rprt_" + detHex("rprt-5"),
    kind: "BFIU_EVIDENCE_PACK",
    label: "BFIU evidence pack case_… (ZIP + manifest.json)",
    generated_at: tsAt(-60 * 26),
    size_bytes: 18874368,
    sha256: sha("report-5"),
  },
];

// ---------------------------------------------------------------------------
// Read API
// ---------------------------------------------------------------------------

export function getOperators(): Operator[] {
  return operators;
}

export function getDemoOperator(): Operator {
  return operators[0]!;
}

export function listApprovalsFiltered(params: { state?: string; action_type?: string; mine?: string }): ApprovalRequest[] {
  let rows = approvals.slice();
  if (params.state) rows = rows.filter((r) => r.state === params.state);
  if (params.action_type) rows = rows.filter((r) => r.action_type === params.action_type);
  if (params.mine === "initiated") rows = rows.filter((r) => r.initiator_id === DEMO_OPERATOR_ID);
  if (params.mine === "awaiting_me")
    rows = rows.filter((r) => r.state === "PENDING_SECOND_APPROVER" && r.initiator_id !== DEMO_OPERATOR_ID);
  return rows.sort((a, b) => (a.created_at < b.created_at ? 1 : -1));
}

export function findApproval(apprId: string): ApprovalRequest | undefined {
  return approvals.find((a) => a.approval_request_id === apprId);
}

export function listCasesFiltered(params: { type?: string; state?: string; priority?: string; sla?: string }): OpsCase[] {
  let rows = cases.slice();
  if (params.type) rows = rows.filter((r) => r.case_type === params.type);
  if (params.state) rows = rows.filter((r) => r.state === params.state);
  if (params.priority) rows = rows.filter((r) => r.priority === params.priority);
  if (params.sla === "breached") rows = rows.filter((r) => r.sla_breached);
  return rows.sort((a, b) => (a.priority < b.priority ? -1 : a.priority > b.priority ? 1 : a.sla_due_at < b.sla_due_at ? -1 : 1));
}

export function findCase(caseId: string): OpsCase | undefined {
  return cases.find((c) => c.case_id === caseId);
}

export function listDisputesFiltered(params: { state?: string; method?: string }): Dispute[] {
  let rows = disputes.slice();
  if (params.state) rows = rows.filter((r) => r.state === params.state);
  if (params.method) rows = rows.filter((r) => r.method === params.method);
  return rows.sort((a, b) => (a.opened_at < b.opened_at ? 1 : -1));
}

export function findDispute(dsptId: string): Dispute | undefined {
  return disputes.find((d) => d.dispute_id === dsptId);
}

export function listCompensationFiltered(params: { state?: string }): CompensationQueueItem[] {
  let rows = compensation.slice();
  if (params.state) rows = rows.filter((r) => r.state === params.state);
  return rows.sort((a, b) => (a.queued_at < b.queued_at ? -1 : 1));
}

export function findCompensation(compId: string): CompensationQueueItem | undefined {
  return compensation.find((c) => c.compensation_id === compId);
}

export function listReconFiltered(params: { state?: string; kind?: string }): ReconciliationException[] {
  let rows = reconExceptions.slice();
  if (params.state) rows = rows.filter((r) => r.state === params.state);
  if (params.kind) rows = rows.filter((r) => r.kind === params.kind);
  return rows.sort((a, b) => (a.detected_at < b.detected_at ? 1 : -1));
}

export function findRecon(exceptionId: string): ReconciliationException | undefined {
  return reconExceptions.find((r) => r.exception_id === exceptionId);
}

export function getConnectors(): ConnectorRegistration[] {
  return connectors;
}

export function findConnector(connectorId: string): ConnectorRegistration | undefined {
  return connectors.find((c) => c.connector_id === connectorId);
}

export function getJournalEntries(): JournalEntry[] {
  return journalEntries;
}

export function getChainEntries(): LedgerChainEntry[] {
  return chainEntries;
}

export function getChainVerifyStatus(): ChainVerifyStatus {
  return chainVerifyStatus;
}

export function getReports(): ReportItem[] {
  return reports;
}

// ---------------------------------------------------------------------------
// Dashboards
// ---------------------------------------------------------------------------

export function tcsaDashboard(): TcsaDashboard {
  return {
    as_of: asOfMinutesAgo(1),
    required_minor: "48250000000",
    available_minor: "49100000000",
    coverage_bps: 10176,
    shortfall_minor: "0",
    trend: [
      { at: tsAt(-300), coverage_bps: 10110 },
      { at: tsAt(-240), coverage_bps: 10095 },
      { at: tsAt(-180), coverage_bps: 10150 },
      { at: tsAt(-120), coverage_bps: 10162 },
      { at: tsAt(-60), coverage_bps: 10171 },
      { at: tsAt(-1), coverage_bps: 10176 },
    ],
    open_compensation_over_attestation_threshold: 3,
  };
}

export function settlementDashboard(): SettlementDashboard {
  return {
    as_of: asOfMinutesAgo(3),
    instructions_by_state: [
      { state: "PENDING", count: 412, amount_minor: "1284500000" },
      { state: "BATCHED", count: 1108, amount_minor: "5230900000" },
      { state: "RELEASED", count: 240, amount_minor: "182000000" },
      { state: "CONFIRMED", count: 9320, amount_minor: "44120000000" },
      { state: "RETURNED", count: 14, amount_minor: "9120000" },
    ],
    open_batches: 6,
    earliest_release_at: new Date(Date.now() + 120 * 60_000).toISOString().replace(".000Z", "Z"),
    latest_release_at: new Date(Date.now() + 60 * 24 * 3 * 60_000).toISOString().replace(".000Z", "Z"),
    working_day_deadline_breaches: 0,
  };
}

export function connectorsDashboard(): ConnectorsDashboard {
  // last_health_at must track wall clock like as_of — boot-frozen tsAt(-2)
  // otherwise AgeBadge says "2m old" while the column shows yesterday.
  return {
    as_of: asOfMinutesAgo(2),
    connectors: connectors.map((c) => ({
      connector_id: c.connector_id,
      display_name: c.display_name,
      active_mode: c.active_mode,
      health_status: c.health_status,
      circuit_state: c.circuit_state,
      last_health_at: asOfMinutesAgo(2),
    })),
  };
}

export function amlDashboard(): AmlDashboard {
  return {
    as_of: asOfMinutesAgo(4),
    alert_queue_depth: 17,
    alert_age_histogram: [
      { bucket: "0-6h", count: 5 },
      { bucket: "6-12h", count: 4 },
      { bucket: "12-24h", count: 4 },
      { bucket: "24-48h", count: 3 },
      { bucket: ">48h", count: 1 },
    ],
    str_queue: [
      { str_id: "str_" + detHex("str-q1"), subject_id: "cust_" + detHex("cust-q1"), state: "DRAFT", created_at: tsAt(-60 * 9) },
      { str_id: "str_" + detHex("str-q2"), subject_id: "mrch_" + detHex("mrch-q2"), state: "CAMLCO_REVIEW", created_at: tsAt(-60 * 20) },
      { str_id: "str_" + detHex("str-q3"), subject_id: "cust_" + detHex("cust-q3"), state: "READY_TO_FILE", created_at: tsAt(-60 * 30) },
    ],
    goaml_filings: [
      { filing_id: "goml_" + detHex("goml-1"), state: "QUEUED", submitted_at: null },
      { filing_id: "goml_" + detHex("goml-2"), state: "SUBMITTED", submitted_at: tsAt(-60 * 14) },
      { filing_id: "goml_" + detHex("goml-3"), state: "ACKNOWLEDGED", submitted_at: tsAt(-60 * 40) },
    ],
    sanctions_feed_age_seconds: 5400,
    sanctions_feed_stale: false,
  };
}

export function slaDashboard(): SlaDashboard {
  return {
    as_of: asOfMinutesAgo(2),
    route_groups: [
      { group: "payment-intents", p50_ms: 42, p95_ms: 180, p99_ms: 420 },
      { group: "refunds", p50_ms: 51, p95_ms: 210, p99_ms: 530 },
      { group: "connector-callbacks", p50_ms: 18, p95_ms: 95, p99_ms: 240 },
      { group: "dashboards", p50_ms: 33, p95_ms: 120, p99_ms: 300 },
      { group: "approvals", p50_ms: 39, p95_ms: 140, p99_ms: 350 },
    ],
  };
}

// ---------------------------------------------------------------------------
// Mutations (FSM-consistent, module-scope memory)
// ---------------------------------------------------------------------------

export function approveApprovalMock(apprId: string, actorId: string, decisionReason: string): MockResult<ApprovalRequest> {
  const a = findApproval(apprId);
  if (!a) return mockError("not_found", "approval_not_found", "No such approval request.");
  if (a.state !== "PENDING_SECOND_APPROVER" && a.state !== "APPROVED_EXECUTION_FAILED") {
    return mockError("conflict", "approval_already_decided", "Another operator decided this request first.");
  }
  if (a.initiator_id === actorId && a.state === "PENDING_SECOND_APPROVER") {
    return mockError("authorization", "initiator_cannot_approve", "Initiator and approver must be different operators.");
  }
  const now = mutationTs();
  a.state = "APPROVED";
  a.approver_id = actorId;
  a.approver_name = operName(actorId);
  a.decision_reason = decisionReason;
  a.decided_at = now;
  a.executed_at = now;
  a.updated_at = now;
  return ok(a);
}

export function rejectApprovalMock(apprId: string, actorId: string, decisionReason: string): MockResult<ApprovalRequest> {
  const a = findApproval(apprId);
  if (!a) return mockError("not_found", "approval_not_found", "No such approval request.");
  if (a.state !== "PENDING_SECOND_APPROVER") {
    return mockError("conflict", "approval_already_decided", "Another operator decided this request first.");
  }
  if (a.initiator_id === actorId) {
    return mockError("authorization", "initiator_cannot_decide", "Initiator may only withdraw a pending request.");
  }
  const now = mutationTs();
  a.state = "REJECTED";
  a.approver_id = actorId;
  a.approver_name = operName(actorId);
  a.decision_reason = decisionReason;
  a.decided_at = now;
  a.updated_at = now;
  return ok(a);
}

export function withdrawApprovalMock(apprId: string, actorId: string): MockResult<ApprovalRequest> {
  const a = findApproval(apprId);
  if (!a) return mockError("not_found", "approval_not_found", "No such approval request.");
  if (a.state !== "PENDING_SECOND_APPROVER") {
    return mockError("conflict", "approval_not_pending", "Only pending requests can be withdrawn.");
  }
  if (a.initiator_id !== actorId) {
    return mockError("authorization", "withdraw_initiator_only", "Only the initiator may withdraw.");
  }
  const now = mutationTs();
  a.state = "WITHDRAWN";
  a.decided_at = now;
  a.updated_at = now;
  return ok(a);
}

function createApprovalForAction(
  actionType: ApprovalActionType,
  subjectType: string,
  subjectId: string,
  payload: Record<string, unknown>,
  reason: string,
  initiatorId: string,
): ApprovalRequest {
  const now = mutationTs();
  const a: ApprovalRequest = {
    approval_request_id: `appr_${detHex(`runtime:${actionType}:${subjectId}:${mutationSeq}`)}`,
    action_type: actionType,
    subject_type: subjectType,
    subject_id: subjectId,
    payload,
    payload_hash: sha(`runtime-payload:${actionType}:${subjectId}:${mutationSeq}`),
    reason,
    state: "PENDING_SECOND_APPROVER",
    initiator_id: initiatorId,
    initiator_name: operName(initiatorId) ?? initiatorId,
    approver_id: null,
    approver_name: null,
    decision_reason: null,
    threshold_minor: null,
    expires_at: new Date(Date.parse(now) + 24 * 3600 * 1000).toISOString().replace(".000Z", "Z"),
    decided_at: null,
    executed_at: null,
    created_at: now,
    updated_at: now,
    schema_version: 1,
  };
  approvals.unshift(a);
  return a;
}

export function assignCaseMock(caseId: string, actorId: string, assigneeId: string): MockResult<OpsCase> {
  const c = findCase(caseId);
  if (!c) return mockError("not_found", "case_not_found", "No such case.");
  if (c.state !== "OPEN" && c.state !== "IN_PROGRESS" && c.state !== "WAITING_EXTERNAL") {
    return mockError("conflict", "case_terminal", "Case is already terminal.");
  }
  const now = mutationTs();
  c.assignee_id = assigneeId;
  c.assignee_name = operName(assigneeId);
  if (c.state === "OPEN") c.state = "IN_PROGRESS";
  c.updated_at = now;
  return ok(c);
}

export function commentCaseMock(caseId: string, actorId: string, body: string): MockResult<OpsCase> {
  const c = findCase(caseId);
  if (!c) return mockError("not_found", "case_not_found", "No such case.");
  if (body.trim().length === 0) return mockError("invalid_request", "empty_comment", "Comment body required.");
  const now = mutationTs();
  c.comments.push({ author_id: actorId, author_name: operName(actorId) ?? actorId, body, at: now });
  c.updated_at = now;
  return ok(c);
}

export function attachCaseEvidenceMock(caseId: string, actorId: string, pointer: string, sha256: string, label: string): MockResult<OpsCase> {
  const c = findCase(caseId);
  if (!c) return mockError("not_found", "case_not_found", "No such case.");
  if (!/^[0-9a-f]{64}$/i.test(sha256)) {
    return mockError("invalid_request", "bad_sha256", "Evidence sha256 must be 64 hex characters.");
  }
  const now = mutationTs();
  c.evidence.push({ pointer, sha256: sha256.toLowerCase(), label, added_by: actorId, added_at: now });
  c.updated_at = now;
  return ok(c);
}

export function resolveCaseMock(caseId: string, actorId: string, resolutionNote: string): MockResult<OpsCase> {
  const c = findCase(caseId);
  if (!c) return mockError("not_found", "case_not_found", "No such case.");
  if (c.state !== "IN_PROGRESS" && c.state !== "WAITING_EXTERNAL") {
    return mockError("conflict", "case_not_resolvable", "Only IN_PROGRESS or WAITING_EXTERNAL cases can be resolved.");
  }
  if (resolutionNote.trim().length === 0) {
    return mockError("invalid_request", "resolution_note_required", "A resolution note is required.");
  }
  const now = mutationTs();
  c.state = "RESOLVED";
  c.resolution_note = resolutionNote;
  c.resolved_at = now;
  c.updated_at = now;
  return ok(c);
}

export function requestDisputeEvidenceMock(dsptId: string): MockResult<Dispute> {
  const d = findDispute(dsptId);
  if (!d) return mockError("not_found", "dispute_not_found", "No such dispute.");
  if (d.state !== "OPEN") return mockError("conflict", "dispute_not_open", "Evidence can only be requested on OPEN disputes.");
  const now = mutationTs();
  d.state = "EVIDENCE_PENDING";
  d.evidence_deadline_at = new Date(Date.parse(now) + 7 * 24 * 3600 * 1000).toISOString().replace(".000Z", "Z");
  d.updated_at = now;
  return ok(d);
}

export function submitDisputeEvidenceMock(dsptId: string, actorId: string, pointer: string, sha256: string, label: string): MockResult<Dispute> {
  const d = findDispute(dsptId);
  if (!d) return mockError("not_found", "dispute_not_found", "No such dispute.");
  if (d.state !== "EVIDENCE_PENDING") {
    return mockError("conflict", "evidence_window_closed", "Evidence is only accepted while EVIDENCE_PENDING.");
  }
  if (!/^[0-9a-f]{64}$/i.test(sha256)) {
    return mockError("invalid_request", "bad_sha256", "Evidence sha256 must be 64 hex characters.");
  }
  const now = mutationTs();
  d.evidence.push({ pointer, sha256: sha256.toLowerCase(), label, added_by: actorId, added_at: now });
  d.updated_at = now;
  return ok(d);
}

export function markDisputeUnderReviewMock(dsptId: string): MockResult<Dispute> {
  const d = findDispute(dsptId);
  if (!d) return mockError("not_found", "dispute_not_found", "No such dispute.");
  if (d.state !== "EVIDENCE_PENDING") {
    return mockError("conflict", "dispute_wrong_state", "Only EVIDENCE_PENDING disputes can move to UNDER_REVIEW.");
  }
  const now = mutationTs();
  if (d.evidence.length === 0) d.no_merchant_evidence = true;
  d.state = "UNDER_REVIEW";
  d.updated_at = now;
  return ok(d);
}

export function resolveDisputeMock(
  dsptId: string,
  actorId: string,
  outcome: "RESOLVED_MERCHANT" | "RESOLVED_CUSTOMER",
  note: string,
): MockResult<Dispute> {
  const d = findDispute(dsptId);
  if (!d) return mockError("not_found", "dispute_not_found", "No such dispute.");
  if (d.state !== "UNDER_REVIEW") {
    return mockError("conflict", "dispute_wrong_state", "Only UNDER_REVIEW disputes can be resolved.");
  }
  const now = mutationTs();
  // Money-moving outcome detours through the four-eyes spine.
  createApprovalForAction(
    "dispute_resolution_money",
    "Dispute",
    d.dispute_id,
    { outcome, amount_minor: d.amount_minor, note },
    note || "Dispute resolution",
    actorId,
  );
  d.state = outcome;
  if (outcome === "RESOLVED_CUSTOMER") {
    d.refund_id = `rfnd_${detHex(`runtime-rfnd:${d.dispute_id}:${mutationSeq}`)}`;
  }
  d.resolved_at = now;
  d.updated_at = now;
  return ok(d);
}

export function fileChargebackMock(dsptId: string): MockResult<Dispute> {
  const d = findDispute(dsptId);
  if (!d) return mockError("not_found", "dispute_not_found", "No such dispute.");
  if (d.method !== "CARD") {
    return mockError("invalid_request", "chargeback_card_only", "file_chargeback is guarded method=CARD.");
  }
  if (d.state !== "UNDER_REVIEW") {
    return mockError("conflict", "dispute_wrong_state", "Chargebacks file from UNDER_REVIEW only.");
  }
  const now = mutationTs();
  d.state = "CHARGEBACK_FILED";
  d.scheme_case_ref = `CB-${detHex(`runtime-cb:${d.dispute_id}:${mutationSeq}`, 10).toUpperCase()}`;
  d.updated_at = now;
  return ok(d);
}

export function firstTouchCompensationMock(compId: string, actorId: string): MockResult<CompensationQueueItem> {
  const item = findCompensation(compId);
  if (!item) return mockError("not_found", "compensation_not_found", "No such compensation item.");
  if (item.state !== "QUEUED") return mockError("conflict", "comp_wrong_state", "First touch applies to QUEUED items only.");
  const now = mutationTs();
  item.state = "INVESTIGATING";
  item.updated_at = now;
  return ok(item);
}

export function setRunbookStepMock(compId: string, actorId: string, step: number, done: boolean): MockResult<CompensationQueueItem> {
  const item = findCompensation(compId);
  if (!item) return mockError("not_found", "compensation_not_found", "No such compensation item.");
  if (item.state !== "INVESTIGATING") {
    return mockError("conflict", "comp_wrong_state", "Runbook steps are editable while INVESTIGATING.");
  }
  const target = item.runbook_checklist.find((s) => s.step === step);
  if (!target) return mockError("invalid_request", "bad_runbook_step", "Runbook step out of range.");
  const now = mutationTs();
  target.done = done;
  target.done_by = done ? actorId : null;
  target.done_at = done ? now : null;
  item.updated_at = now;
  return ok(item);
}

export function attachCompensationEvidenceMock(compId: string, actorId: string, pointer: string, sha256: string, label: string): MockResult<CompensationQueueItem> {
  const item = findCompensation(compId);
  if (!item) return mockError("not_found", "compensation_not_found", "No such compensation item.");
  if (!/^[0-9a-f]{64}$/i.test(sha256)) {
    return mockError("invalid_request", "bad_sha256", "Evidence sha256 must be 64 hex characters.");
  }
  const now = mutationTs();
  item.evidence.push({ pointer, sha256: sha256.toLowerCase(), label, added_by: actorId, added_at: now });
  item.updated_at = now;
  return ok(item);
}

export function resolveCompensationMock(
  compId: string,
  actorId: string,
  kind: CompensationResolutionKind,
  note: string,
): MockResult<CompensationQueueItem> {
  const item = findCompensation(compId);
  if (!item) return mockError("not_found", "compensation_not_found", "No such compensation item.");
  if (item.state !== "INVESTIGATING") {
    return mockError("conflict", "comp_wrong_state", "Resolution proposal requires INVESTIGATING state.");
  }
  if (item.evidence.length === 0) {
    return mockError("invalid_request", "evidence_required", "A resolution proposal requires attached evidence.");
  }
  const now = mutationTs();
  const appr = createApprovalForAction(
    "compensation_resolution",
    "CompensationQueueItem",
    item.compensation_id,
    { resolution_kind: kind, amount_minor: item.amount_minor, note },
    note || "Compensation resolution",
    actorId,
  );
  item.state = "PENDING_APPROVAL";
  item.resolution_kind = kind;
  item.approval_request_id = appr.approval_request_id;
  item.updated_at = now;
  return ok(item);
}

export function resolveReconExceptionMock(exceptionId: string, actorId: string, note: string): MockResult<ReconciliationException> {
  const r = findRecon(exceptionId);
  if (!r) return mockError("not_found", "recon_exception_not_found", "No such reconciliation exception.");
  if (r.state === "RESOLVED") return mockError("conflict", "already_resolved", "Exception is already resolved.");
  const now = mutationTs();
  // Resolution flows through the case workflow: ensure a linked case exists.
  if (r.case_id === null) {
    const newCase: OpsCase = {
      case_id: `case_${detHex(`runtime-case:${r.exception_id}:${mutationSeq}`)}`,
      case_type: "RECON_EXCEPTION",
      state: "IN_PROGRESS",
      priority: "P2",
      subject_type: "ReconciliationException",
      subject_id: r.exception_id,
      assignee_id: actorId,
      assignee_name: operName(actorId),
      sla_due_at: new Date(Date.parse(now) + 24 * 3600 * 1000).toISOString().replace(".000Z", "Z"),
      sla_breached: false,
      evidence: [],
      comments: [{ author_id: actorId, author_name: operName(actorId) ?? actorId, body: note, at: now }],
      resolution_note: null,
      opened_at: now,
      resolved_at: null,
      updated_at: now,
      schema_version: 1,
    };
    cases.unshift(newCase);
    r.case_id = newCase.case_id;
  }
  r.state = "RESOLVED";
  r.resolved_at = now;
  return ok(r);
}

export function requestModeChangeMock(
  connectorId: string,
  actorId: string,
  targetMode: ConnectorMode,
  reason: string,
): MockResult<{ approval_request_id?: string; state?: ApprovalState; active_mode?: ConnectorMode }> {
  const c = findConnector(connectorId);
  if (!c) return mockError("not_found", "connector_not_found", "No such connector registration.");
  if (targetMode === c.active_mode) {
    return mockError("invalid_request", "mode_unchanged", "Target mode equals the active mode.");
  }
  if (reason.trim().length === 0) {
    return mockError("invalid_request", "reason_required", "A reason is required for mode changes.");
  }
  const now = mutationTs();
  if (targetMode === "PRODUCTION") {
    const existing = approvals.find(
      (a) =>
        a.action_type === "connector_mode_to_production" &&
        a.subject_id === connectorId &&
        (a.state === "PENDING_SECOND_APPROVER" || a.state === "APPROVED_EXECUTION_FAILED"),
    );
    if (existing) {
      return mockError("conflict", "live_approval_exists", "One live request per (action_type, subject): an approval is already pending.");
    }
    const appr = createApprovalForAction(
      "connector_mode_to_production",
      "ConnectorRegistration",
      connectorId,
      { target_mode: targetMode, reason },
      reason,
      actorId,
    );
    return { status: 202, body: { approval_request_id: appr.approval_request_id, state: appr.state } };
  }
  c.active_mode = targetMode;
  c.enabled = targetMode !== "DISABLED";
  c.last_health_at = now;
  return ok({ active_mode: c.active_mode });
}

export function resetCircuitMock(connectorId: string): MockResult<ConnectorRegistration> {
  const c = findConnector(connectorId);
  if (!c) return mockError("not_found", "connector_not_found", "No such connector registration.");
  if (c.circuit_state !== "OPEN") {
    return mockError("conflict", "circuit_not_open", "Only an OPEN breaker can be reset to HALF_OPEN.");
  }
  const now = mutationTs();
  c.circuit_state = "HALF_OPEN";
  c.last_health_at = now;
  return ok(c);
}

export function disableOperatorMock(operId: string, actorId: string): MockResult<Operator> {
  const target = operators.find((o) => o.operator_id === operId);
  if (!target) return mockError("not_found", "operator_not_found", "No such operator.");
  if (target.operator_id === actorId) {
    return mockError("invalid_request", "cannot_disable_self", "An operator cannot disable their own account.");
  }
  if (target.credential_state === "DISABLED") {
    return mockError("conflict", "already_disabled", "Operator is already disabled.");
  }
  const now = mutationTs();
  target.credential_state = "DISABLED";
  target.updated_at = now;
  return ok(target);
}

export function camlcoFreezeMock(actorId: string, subjectType: string, subjectId: string, reason: string): MockResult<ApprovalRequest> {
  if (subjectId.trim().length === 0 || reason.trim().length === 0) {
    return mockError("invalid_request", "freeze_fields_required", "Subject and reason are required.");
  }
  // Freeze-first, review-second: freeze applies immediately on initiation;
  // the admin pair confirms via the created ApprovalRequest within 24h.
  const appr = createApprovalForAction(
    "camlco_freeze",
    subjectType,
    subjectId,
    { freeze_scope: "ALL_OUTBOUND", applied_immediately: true, basis: "ATA_2009" },
    reason,
    actorId,
  );
  return { status: 202, body: appr };
}

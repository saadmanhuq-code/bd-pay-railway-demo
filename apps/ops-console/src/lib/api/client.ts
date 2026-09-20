// Typed fetch client. GET helpers + named mutation functions ONLY.
// Every mutation:
//   - takes a totpCode argument (TOTP step-up per BB ICT §9.2),
//   - posts an Idempotency-Key header (conventions §4),
//   - is registered in src/lib/api/mutations.ts (no-write-guard manifest).
// This file is the ONLY module allowed to issue non-GET fetches
// (enforced by scripts/no-write-guard.mjs at build time).

import type {
  AmlDashboard,
  ApprovalRequest,
  ChainVerifyStatus,
  CompensationQueueItem,
  CompensationResolutionKind,
  ConnectorMode,
  ConnectorRegistration,
  ConnectorsDashboard,
  Dispute,
  ErrorEnvelope,
  JournalEntry,
  LedgerChainEntry,
  LoginResult,
  ModeChangeResult,
  OpsCase,
  Operator,
  Paginated,
  ReconciliationException,
  ReportItem,
  SandboxDemoFullflow,
  Session,
  SettlementDashboard,
  SlaDashboard,
  TcsaDashboard,
} from "./types";

import { isMockEnabled } from "@bdpay/edge/env-flag";

// Base URL from NEXT_PUBLIC_BDPAY_API_BASE. In development the in-app
// deterministic mock routes (/api/mock) are the default target. In a
// production build the mock is NEVER the silent default: if the real gateway
// base is unset we fail loudly rather than serve the dev mock backend (SEC-01).
// Setting NEXT_PUBLIC_BDPAY_API_BASE is the production opt-out of the mock;
// a truthy NEXT_PUBLIC_BDPAY_ENABLE_MOCK is an explicit escape hatch for
// prod-mode smoke tests. NOTE: only NEXT_PUBLIC_* and NODE_ENV are inlined into
// the browser bundle by Next.js, so this CLIENT opt-in must use the public
// prefix. The opt-in is parsed with the STRICT boolean isMockEnabled(), so
// NEXT_PUBLIC_BDPAY_ENABLE_MOCK=0 / =false stay DISABLED rather than being read
// as truthy non-empty strings; only "1"/"true"/"yes"/"on" opt in.
//
// SEC-01 two-var model (server/client separation — see src/lib/mock/guard.ts):
//   - NEXT_PUBLIC_BDPAY_ENABLE_MOCK (this file, client) only routes the browser
//     fetch client to /api/mock. By design it does NOT, on its own, re-enable
//     the server mock route handlers — that is gated solely on the SERVER-ONLY
//     BDPAY_ENABLE_MOCK. A public/client-exposed var must never be able to
//     re-open the server-side passwordless auth bypass.
//   - To run mock end-to-end in production you must set BOTH: BDPAY_ENABLE_MOCK
//     (server serves /api/mock) and NEXT_PUBLIC_BDPAY_ENABLE_MOCK (client
//     targets it). Both default off in production.
function resolveBase(): string {
  const configured = process.env.NEXT_PUBLIC_BDPAY_API_BASE;
  if (configured) return configured;
  if (process.env.NODE_ENV === "production" && !isMockEnabled(process.env.NEXT_PUBLIC_BDPAY_ENABLE_MOCK)) {
    throw new Error(
      "NEXT_PUBLIC_BDPAY_API_BASE is not set in a production build. Refusing to " +
        "fall back to the in-app dev mock backend (SEC-01). Set the real gateway " +
        "base URL, or set NEXT_PUBLIC_BDPAY_ENABLE_MOCK to explicitly opt in.",
    );
  }
  return "/api/mock";
}

const BASE: string = resolveBase();

/** True when the browser client is pointed at the in-app /api/mock simulator. */
export const USING_MOCK_API: boolean =
  BASE === "/api/mock" || BASE.startsWith("/api/mock/");

export class ApiError extends Error {
  readonly status: number;
  readonly envelope: ErrorEnvelope | null;

  constructor(status: number, envelope: ErrorEnvelope | null) {
    super(envelope?.error.message ?? `request failed with status ${status}`);
    this.status = status;
    this.envelope = envelope;
  }
}

async function parseError(res: Response): Promise<never> {
  let envelope: ErrorEnvelope | null = null;
  try {
    envelope = (await res.json()) as ErrorEnvelope;
  } catch {
    envelope = null;
  }
  throw new ApiError(res.status, envelope);
}

async function apiGet<T>(path: string, params?: Record<string, string>): Promise<T> {
  const qs = params
    ? Object.entries(params).filter(([, v]) => v !== "")
    : [];
  const query = qs.length > 0 ? `?${new URLSearchParams(Object.fromEntries(qs)).toString()}` : "";
  const res = await fetch(`${BASE}${path}${query}`, {
    method: "GET",
    credentials: "same-origin",
    headers: { Accept: "application/json" },
  });
  if (!res.ok) return parseError(res);
  return (await res.json()) as T;
}

interface MutateOptions {
  totpCode?: string;
}

async function mutate<T>(path: string, body: Record<string, unknown>, opts: MutateOptions = {}): Promise<T> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    Accept: "application/json",
    "Idempotency-Key": newIdempotencyKey(),
  };
  if (opts.totpCode !== undefined) {
    headers["X-BDPay-TOTP"] = opts.totpCode;
  }
  const res = await fetch(`${BASE}${path}`, {
    method: "POST",
    credentials: "same-origin",
    headers,
    body: JSON.stringify(body),
  });
  if (!res.ok) return parseError(res);
  return (await res.json()) as T;
}

function newIdempotencyKey(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return crypto.randomUUID();
  }
  const t = Date.now().toString(16);
  return `idem-${t}-${Math.floor(performance.now() * 1000).toString(16)}`;
}

// ---------------------------------------------------------------------------
// Session / auth
// ---------------------------------------------------------------------------

export function getSession(): Promise<Session> {
  return apiGet<Session>("/v1/auth/operator/session");
}

export function login(email: string, password: string, totpCode: string): Promise<LoginResult> {
  return mutate<LoginResult>("/v1/auth/operator/login", { email, password, totp_code: totpCode });
}

export function logout(): Promise<{ ok: boolean }> {
  return mutate<{ ok: boolean }>("/v1/auth/operator/logout", {});
}

// ---------------------------------------------------------------------------
// Approvals
// ---------------------------------------------------------------------------

export function listApprovals(params: {
  state?: string;
  action_type?: string;
  mine?: "initiated" | "awaiting_me" | "";
  cursor?: string;
}): Promise<Paginated<ApprovalRequest>> {
  return apiGet("/v1/approval-requests", {
    state: params.state ?? "",
    action_type: params.action_type ?? "",
    mine: params.mine ?? "",
    cursor: params.cursor ?? "",
  });
}

export function getApproval(apprId: string): Promise<ApprovalRequest> {
  return apiGet(`/v1/approval-requests/${encodeURIComponent(apprId)}`);
}

export function approveApproval(apprId: string, decisionReason: string, totpCode: string): Promise<ApprovalRequest> {
  return mutate(`/v1/approval-requests/${encodeURIComponent(apprId)}/approve`, { decision_reason: decisionReason }, { totpCode });
}

export function rejectApproval(apprId: string, decisionReason: string, totpCode: string): Promise<ApprovalRequest> {
  return mutate(`/v1/approval-requests/${encodeURIComponent(apprId)}/reject`, { decision_reason: decisionReason }, { totpCode });
}

export function withdrawApproval(apprId: string, totpCode: string): Promise<ApprovalRequest> {
  return mutate(`/v1/approval-requests/${encodeURIComponent(apprId)}/withdraw`, {}, { totpCode });
}

// ---------------------------------------------------------------------------
// Ops cases
// ---------------------------------------------------------------------------

export function listCases(params: {
  type?: string;
  state?: string;
  priority?: string;
  sla?: string;
  cursor?: string;
}): Promise<Paginated<OpsCase>> {
  return apiGet("/v1/ops-cases", {
    type: params.type ?? "",
    state: params.state ?? "",
    priority: params.priority ?? "",
    sla: params.sla ?? "",
    cursor: params.cursor ?? "",
  });
}

export function getCase(caseId: string): Promise<OpsCase> {
  return apiGet(`/v1/ops-cases/${encodeURIComponent(caseId)}`);
}

export function assignCase(caseId: string, assigneeId: string, totpCode: string): Promise<OpsCase> {
  return mutate(`/v1/ops-cases/${encodeURIComponent(caseId)}/assign`, { assignee_id: assigneeId }, { totpCode });
}

export function commentCase(caseId: string, body: string, totpCode: string): Promise<OpsCase> {
  return mutate(`/v1/ops-cases/${encodeURIComponent(caseId)}/comment`, { body }, { totpCode });
}

export function attachCaseEvidence(caseId: string, pointer: string, sha256: string, label: string, totpCode: string): Promise<OpsCase> {
  return mutate(`/v1/ops-cases/${encodeURIComponent(caseId)}/attach-evidence`, { pointer, sha256, label }, { totpCode });
}

export function resolveCase(caseId: string, resolutionNote: string, totpCode: string): Promise<OpsCase> {
  return mutate(`/v1/ops-cases/${encodeURIComponent(caseId)}/resolve`, { resolution_note: resolutionNote }, { totpCode });
}

// ---------------------------------------------------------------------------
// Disputes
// ---------------------------------------------------------------------------

export function listDisputes(params: { state?: string; method?: string; cursor?: string }): Promise<Paginated<Dispute>> {
  return apiGet("/v1/disputes", {
    state: params.state ?? "",
    method: params.method ?? "",
    cursor: params.cursor ?? "",
  });
}

export function getDispute(dsptId: string): Promise<Dispute> {
  return apiGet(`/v1/disputes/${encodeURIComponent(dsptId)}`);
}

export function requestDisputeEvidence(dsptId: string, totpCode: string): Promise<Dispute> {
  return mutate(`/v1/disputes/${encodeURIComponent(dsptId)}/request-evidence`, {}, { totpCode });
}

export function submitDisputeEvidence(dsptId: string, pointer: string, sha256: string, label: string, totpCode: string): Promise<Dispute> {
  return mutate(`/v1/disputes/${encodeURIComponent(dsptId)}/evidence`, { pointer, sha256, label }, { totpCode });
}

export function markDisputeUnderReview(dsptId: string, totpCode: string): Promise<Dispute> {
  return mutate(`/v1/disputes/${encodeURIComponent(dsptId)}/mark-under-review`, {}, { totpCode });
}

export function resolveDispute(dsptId: string, outcome: "RESOLVED_MERCHANT" | "RESOLVED_CUSTOMER", note: string, totpCode: string): Promise<Dispute> {
  return mutate(`/v1/disputes/${encodeURIComponent(dsptId)}/resolve`, { outcome, note }, { totpCode });
}

export function fileChargeback(dsptId: string, totpCode: string): Promise<Dispute> {
  return mutate(`/v1/disputes/${encodeURIComponent(dsptId)}/file-chargeback`, {}, { totpCode });
}

// ---------------------------------------------------------------------------
// Compensation queue
// ---------------------------------------------------------------------------

export function listCompensation(params: { state?: string; cursor?: string }): Promise<Paginated<CompensationQueueItem>> {
  return apiGet("/v1/compensation-queue", { state: params.state ?? "", cursor: params.cursor ?? "" });
}

export function getCompensation(compId: string): Promise<CompensationQueueItem> {
  return apiGet(`/v1/compensation-queue/${encodeURIComponent(compId)}`);
}

export function firstTouchCompensation(compId: string, totpCode: string): Promise<CompensationQueueItem> {
  return mutate(`/v1/compensation-queue/${encodeURIComponent(compId)}/first-touch`, {}, { totpCode });
}

export function setCompensationRunbookStep(compId: string, step: number, done: boolean, totpCode: string): Promise<CompensationQueueItem> {
  return mutate(`/v1/compensation-queue/${encodeURIComponent(compId)}/runbook-step`, { step, done }, { totpCode });
}

export function attachCompensationEvidence(compId: string, pointer: string, sha256: string, label: string, totpCode: string): Promise<CompensationQueueItem> {
  return mutate(`/v1/compensation-queue/${encodeURIComponent(compId)}/attach-evidence`, { pointer, sha256, label }, { totpCode });
}

export function resolveCompensation(compId: string, kind: CompensationResolutionKind, note: string, totpCode: string): Promise<CompensationQueueItem> {
  return mutate(`/v1/compensation-queue/${encodeURIComponent(compId)}/resolve`, { resolution_kind: kind, note }, { totpCode });
}

// ---------------------------------------------------------------------------
// Reconciliation exceptions
// ---------------------------------------------------------------------------

export function listReconExceptions(params: { state?: string; kind?: string; cursor?: string }): Promise<Paginated<ReconciliationException>> {
  return apiGet("/v1/recon-exceptions", {
    state: params.state ?? "",
    kind: params.kind ?? "",
    cursor: params.cursor ?? "",
  });
}

export function getReconException(exceptionId: string): Promise<ReconciliationException> {
  return apiGet(`/v1/recon-exceptions/${encodeURIComponent(exceptionId)}`);
}

export function resolveReconException(exceptionId: string, note: string, totpCode: string): Promise<ReconciliationException> {
  return mutate(`/v1/recon-exceptions/${encodeURIComponent(exceptionId)}/resolve`, { note }, { totpCode });
}

// ---------------------------------------------------------------------------
// Ledger viewer (read-only — GETs only by design)
// ---------------------------------------------------------------------------

export function listJournalEntries(params: { cursor?: string }): Promise<Paginated<JournalEntry>> {
  return apiGet("/v1/ledger/journal-entries", { cursor: params.cursor ?? "" });
}

export function listChainEntries(params: { cursor?: string }): Promise<Paginated<LedgerChainEntry>> {
  return apiGet("/v1/ledger/chain-entries", { cursor: params.cursor ?? "" });
}

export function getChainVerifyStatus(): Promise<ChainVerifyStatus> {
  return apiGet("/v1/ledger/chain-verify-status");
}

// ---------------------------------------------------------------------------
// Connectors
// ---------------------------------------------------------------------------

export function listConnectors(params: { cursor?: string }): Promise<Paginated<ConnectorRegistration>> {
  return apiGet("/v1/connectors", { cursor: params.cursor ?? "" });
}

export function getConnector(connectorId: string): Promise<ConnectorRegistration> {
  return apiGet(`/v1/connectors/${encodeURIComponent(connectorId)}`);
}

export function requestConnectorModeChange(connectorId: string, targetMode: ConnectorMode, reason: string, totpCode: string): Promise<ModeChangeResult> {
  return mutate(`/v1/connectors/${encodeURIComponent(connectorId)}/mode`, { target_mode: targetMode, reason }, { totpCode });
}

export function resetCircuit(connectorId: string, totpCode: string): Promise<ConnectorRegistration> {
  return mutate(`/v1/connectors/${encodeURIComponent(connectorId)}/circuit/reset`, {}, { totpCode });
}

// ---------------------------------------------------------------------------
// Operators (IAM)
// ---------------------------------------------------------------------------

export function listOperators(params: { cursor?: string }): Promise<Paginated<Operator>> {
  return apiGet("/v1/operators", { cursor: params.cursor ?? "" });
}

export function disableOperator(operId: string, reason: string, totpCode: string): Promise<Operator> {
  return mutate(`/v1/operators/${encodeURIComponent(operId)}/disable`, { reason }, { totpCode });
}

// ---------------------------------------------------------------------------
// CAMLCO
// ---------------------------------------------------------------------------

export function camlcoFreeze(subjectType: string, subjectId: string, reason: string, totpCode: string): Promise<ApprovalRequest> {
  return mutate("/v1/camlco/freeze", { subject_type: subjectType, subject_id: subjectId, reason }, { totpCode });
}

// ---------------------------------------------------------------------------
// Dashboards & exports
// ---------------------------------------------------------------------------

export function getTcsaDashboard(): Promise<TcsaDashboard> {
  return apiGet("/v1/dashboards/tcsa");
}

export function getSettlementDashboard(): Promise<SettlementDashboard> {
  return apiGet("/v1/dashboards/settlement");
}

export function getConnectorsDashboard(): Promise<ConnectorsDashboard> {
  return apiGet("/v1/dashboards/connectors");
}

export function getAmlDashboard(): Promise<AmlDashboard> {
  return apiGet("/v1/dashboards/aml");
}

export function getSlaDashboard(): Promise<SlaDashboard> {
  return apiGet("/v1/dashboards/sla");
}

export function listReports(): Promise<Paginated<ReportItem>> {
  return apiGet("/v1/reports");
}

export function getSandboxDemoFullflow(): Promise<SandboxDemoFullflow> {
  return apiGet("/v1/sandbox/demo/fullflow");
}

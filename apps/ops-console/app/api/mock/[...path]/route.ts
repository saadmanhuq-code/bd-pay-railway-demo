// Single dispatcher for the in-app deterministic mock server. All paths
// mirror the spec/15 + spec/10 API surface under /api/mock/v1/...
// Auth: any email + server-verified mock TOTP; session via an HMAC-signed
// httpOnly cookie. Mutations require X-BDPay-TOTP step-up and Idempotency-Key.

import { NextRequest, NextResponse } from "next/server";
import type { Paginated, Session } from "@/lib/api/types";
import {
  DEMO_OPERATOR_ID,
  SNAPSHOT_AT,
  amlDashboard,
  approveApprovalMock,
  assignCaseMock,
  attachCaseEvidenceMock,
  attachCompensationEvidenceMock,
  camlcoFreezeMock,
  commentCaseMock,
  connectorsDashboard,
  disableOperatorMock,
  fileChargebackMock,
  findApproval,
  findCase,
  findCompensation,
  findConnector,
  findDispute,
  findRecon,
  firstTouchCompensationMock,
  getChainEntries,
  getChainVerifyStatus,
  getConnectors,
  getDemoOperator,
  getJournalEntries,
  getOperators,
  getReports,
  listApprovalsFiltered,
  listCasesFiltered,
  listCompensationFiltered,
  listDisputesFiltered,
  listReconFiltered,
  markDisputeUnderReviewMock,
  mockError,
  rejectApprovalMock,
  requestDisputeEvidenceMock,
  requestModeChangeMock,
  resetCircuitMock,
  resolveCaseMock,
  resolveCompensationMock,
  resolveDisputeMock,
  resolveReconExceptionMock,
  setRunbookStepMock,
  settlementDashboard,
  slaDashboard,
  submitDisputeEvidenceMock,
  tcsaDashboard,
  withdrawApprovalMock,
  type MockResult,
} from "@/lib/mock/store";
import { mockDisabled } from "@/lib/mock/guard";
import {
  OPS_SESSION_COOKIE,
  clearOpsSessionCookieOptions,
  hasValidOpsSession,
  issueOpsSession,
  opsSessionCookieOptions,
} from "@/lib/mock/session";
import { verifyMockTotp, type MockTotpResult } from "@bdpay/edge/mock/totp";

export const dynamic = "force-dynamic";

const SESSION_COOKIE = OPS_SESSION_COOKIE;

function json<T>(result: MockResult<T>): NextResponse {
  return NextResponse.json(result.body, { status: result.status });
}

function page<T>(rows: T[]): NextResponse {
  const body: Paginated<T> = { data: rows, next_cursor: null };
  return NextResponse.json(body, { status: 200 });
}

async function hasSession(req: NextRequest): Promise<boolean> {
  return hasValidOpsSession(req);
}

function unauthenticated(): NextResponse {
  return json(mockError("authentication", "session_required", "Sign in to use the ops console."));
}

function totpFailure(result: Exclude<MockTotpResult, "ok">, invalidCode: string, invalidMessage: string): NextResponse {
  if (result === "secret_missing") {
    return json(
      mockError(
        "internal",
        "mock_totp_secret_missing",
        "Mock TOTP secret is not configured.",
      ),
    );
  }
  if (result === "secret_invalid") {
    return json(
      mockError(
        "internal",
        "mock_totp_secret_invalid",
        "Mock TOTP secret is invalid.",
      ),
    );
  }
  return json(mockError("authentication", invalidCode, invalidMessage));
}

function requireTotp(code: string, invalidCode: string, invalidMessage: string): NextResponse | null {
  const result = verifyMockTotp(code);
  return result === "ok" ? null : totpFailure(result, invalidCode, invalidMessage);
}

interface Ctx {
  params: Promise<{ path: string[] }>;
}

// ---------------------------------------------------------------------------
// GET
// ---------------------------------------------------------------------------

export async function GET(req: NextRequest, ctx: Ctx): Promise<NextResponse> {
  const disabled = mockDisabled();
  if (disabled) return disabled;
  const { path: p } = await ctx.params;
  const q = req.nextUrl.searchParams;
  if (p[0] !== "v1") return json(mockError("not_found", "unknown_route", "Unknown mock route."));

  if (p[1] === "auth" && p[2] === "operator" && p[3] === "session") {
    if (!(await hasSession(req))) return unauthenticated();
    const body: Session = {
      operator: getDemoOperator(),
      issued_at: SNAPSHOT_AT,
      absolute_expires_at: new Date(Date.parse(SNAPSHOT_AT) + 8 * 3_600_000).toISOString().replace(".000Z", "Z"),
    };
    return NextResponse.json(body, { status: 200 });
  }

  if (!(await hasSession(req))) return unauthenticated();

  switch (p[1]) {
    case "approval-requests": {
      if (p.length === 2) {
        return page(
          listApprovalsFiltered({
            state: q.get("state") ?? "",
            action_type: q.get("action_type") ?? "",
            mine: q.get("mine") ?? "",
          }),
        );
      }
      const found = findApproval(p[2] ?? "");
      return found ? NextResponse.json(found) : json(mockError("not_found", "approval_not_found", "No such approval request."));
    }
    case "ops-cases": {
      if (p.length === 2) {
        return page(
          listCasesFiltered({
            type: q.get("type") ?? "",
            state: q.get("state") ?? "",
            priority: q.get("priority") ?? "",
            sla: q.get("sla") ?? "",
          }),
        );
      }
      const found = findCase(p[2] ?? "");
      return found ? NextResponse.json(found) : json(mockError("not_found", "case_not_found", "No such case."));
    }
    case "disputes": {
      if (p.length === 2) {
        return page(listDisputesFiltered({ state: q.get("state") ?? "", method: q.get("method") ?? "" }));
      }
      const found = findDispute(p[2] ?? "");
      return found ? NextResponse.json(found) : json(mockError("not_found", "dispute_not_found", "No such dispute."));
    }
    case "compensation-queue": {
      if (p.length === 2) return page(listCompensationFiltered({ state: q.get("state") ?? "" }));
      const found = findCompensation(p[2] ?? "");
      return found ? NextResponse.json(found) : json(mockError("not_found", "compensation_not_found", "No such item."));
    }
    case "recon-exceptions": {
      if (p.length === 2) return page(listReconFiltered({ state: q.get("state") ?? "", kind: q.get("kind") ?? "" }));
      const found = findRecon(p[2] ?? "");
      return found ? NextResponse.json(found) : json(mockError("not_found", "recon_exception_not_found", "No such exception."));
    }
    case "ledger": {
      if (p[2] === "journal-entries") return page(getJournalEntries());
      if (p[2] === "chain-entries") return page(getChainEntries());
      if (p[2] === "chain-verify-status") return NextResponse.json(getChainVerifyStatus());
      break;
    }
    case "connectors": {
      if (p.length === 2) return page(getConnectors());
      const found = findConnector(p[2] ?? "");
      return found ? NextResponse.json(found) : json(mockError("not_found", "connector_not_found", "No such connector."));
    }
    case "operators": {
      if (p.length === 2) return page(getOperators());
      break;
    }
    case "dashboards": {
      if (p[2] === "tcsa") return NextResponse.json(tcsaDashboard());
      if (p[2] === "settlement") return NextResponse.json(settlementDashboard());
      if (p[2] === "connectors") return NextResponse.json(connectorsDashboard());
      if (p[2] === "aml") return NextResponse.json(amlDashboard());
      if (p[2] === "sla") return NextResponse.json(slaDashboard());
      break;
    }
    case "reports": {
      if (p.length === 2) return page(getReports());
      break;
    }
  }
  return json(mockError("not_found", "unknown_route", "Unknown mock route."));
}

// ---------------------------------------------------------------------------
// POST
// ---------------------------------------------------------------------------

export async function POST(req: NextRequest, ctx: Ctx): Promise<NextResponse> {
  const disabled = mockDisabled();
  if (disabled) return disabled;
  const { path: p } = await ctx.params;
  if (p[0] !== "v1") return json(mockError("not_found", "unknown_route", "Unknown mock route."));

  let body: Record<string, unknown> = {};
  try {
    body = (await req.json()) as Record<string, unknown>;
  } catch {
    body = {};
  }
  const str = (key: string): string => (typeof body[key] === "string" ? (body[key] as string) : "");

  // --- auth ---------------------------------------------------------------
  if (p[1] === "auth" && p[2] === "operator" && p[3] === "login") {
    const email = str("email");
    const totp = str("totp_code");
    if (!email.includes("@")) {
      return json(mockError("invalid_request", "email_required", "A valid email is required."));
    }
    const totpDenied = requireTotp(
      totp,
      "totp_invalid",
      "TOTP verification failed. 3 consecutive failures lock the account for 30 minutes (BB ICT §6.2.6).",
    );
    if (totpDenied) {
      return totpDenied;
    }
    const token = await issueOpsSession(DEMO_OPERATOR_ID);
    if (token === null) {
      return json(
        mockError(
          "internal",
          "mock_session_secret_missing",
          "Mock session signing secret is not configured.",
        ),
      );
    }
    const res = NextResponse.json({ operator: getDemoOperator() }, { status: 200 });
    res.cookies.set(SESSION_COOKIE, token, opsSessionCookieOptions());
    return res;
  }
  if (p[1] === "auth" && p[2] === "operator" && p[3] === "logout") {
    const res = NextResponse.json({ ok: true }, { status: 200 });
    res.cookies.set(SESSION_COOKIE, "", clearOpsSessionCookieOptions());
    return res;
  }

  if (!(await hasSession(req))) return unauthenticated();

  // TOTP step-up on every mutation (BB ICT §9.2).
  const stepUpDenied = requireTotp(
    req.headers.get("x-bdpay-totp") ?? "",
    "totp_step_up_required",
    "Mutation requires a valid TOTP step-up code.",
  );
  if (stepUpDenied) return stepUpDenied;
  if (!req.headers.get("idempotency-key")) {
    return json(mockError("invalid_request", "idempotency_key_required", "Mutations require an Idempotency-Key header."));
  }

  const actor = DEMO_OPERATOR_ID;
  const idSeg = p[2] ?? "";
  const action = p[3] ?? "";

  switch (p[1]) {
    case "approval-requests": {
      if (action === "approve") return json(approveApprovalMock(idSeg, actor, str("decision_reason")));
      if (action === "reject") return json(rejectApprovalMock(idSeg, actor, str("decision_reason")));
      if (action === "withdraw") return json(withdrawApprovalMock(idSeg, actor));
      break;
    }
    case "ops-cases": {
      if (action === "assign") return json(assignCaseMock(idSeg, actor, str("assignee_id") || actor));
      if (action === "comment") return json(commentCaseMock(idSeg, actor, str("body")));
      if (action === "attach-evidence") return json(attachCaseEvidenceMock(idSeg, actor, str("pointer"), str("sha256"), str("label")));
      if (action === "resolve") return json(resolveCaseMock(idSeg, actor, str("resolution_note")));
      break;
    }
    case "disputes": {
      if (action === "request-evidence") return json(requestDisputeEvidenceMock(idSeg));
      if (action === "evidence") return json(submitDisputeEvidenceMock(idSeg, actor, str("pointer"), str("sha256"), str("label")));
      if (action === "mark-under-review") return json(markDisputeUnderReviewMock(idSeg));
      if (action === "resolve") {
        const outcome = str("outcome");
        if (outcome !== "RESOLVED_MERCHANT" && outcome !== "RESOLVED_CUSTOMER") {
          return json(mockError("invalid_request", "bad_outcome", "outcome must be RESOLVED_MERCHANT or RESOLVED_CUSTOMER."));
        }
        return json(resolveDisputeMock(idSeg, actor, outcome, str("note")));
      }
      if (action === "file-chargeback") return json(fileChargebackMock(idSeg));
      break;
    }
    case "compensation-queue": {
      if (action === "first-touch") return json(firstTouchCompensationMock(idSeg, actor));
      if (action === "runbook-step") {
        const step = typeof body["step"] === "number" ? (body["step"] as number) : 0;
        const done = body["done"] === true;
        return json(setRunbookStepMock(idSeg, actor, step, done));
      }
      if (action === "attach-evidence") return json(attachCompensationEvidenceMock(idSeg, actor, str("pointer"), str("sha256"), str("label")));
      if (action === "resolve") {
        const kind = str("resolution_kind");
        if (kind !== "RAIL_REVERSED_CONFIRMED" && kind !== "MANUAL_RAIL_REFUND" && kind !== "LEDGER_ADJUSTMENT" && kind !== "WRITE_OFF") {
          return json(mockError("invalid_request", "bad_resolution_kind", "Unknown compensation resolution kind."));
        }
        return json(resolveCompensationMock(idSeg, actor, kind, str("note")));
      }
      break;
    }
    case "recon-exceptions": {
      if (action === "resolve") return json(resolveReconExceptionMock(idSeg, actor, str("note")));
      break;
    }
    case "connectors": {
      if (action === "mode") {
        const mode = str("target_mode");
        if (mode !== "MANUAL_LOCAL" && mode !== "SIMULATOR" && mode !== "SANDBOX" && mode !== "PRODUCTION" && mode !== "DISABLED") {
          return json(mockError("invalid_request", "bad_mode", "Unknown connector mode."));
        }
        return json(requestModeChangeMock(idSeg, actor, mode, str("reason")));
      }
      if (action === "circuit" && p[4] === "reset") return json(resetCircuitMock(idSeg));
      break;
    }
    case "operators": {
      if (action === "disable") return json(disableOperatorMock(idSeg, actor));
      break;
    }
    case "camlco": {
      if (idSeg === "freeze" || p[2] === "freeze") {
        return json(camlcoFreezeMock(actor, str("subject_type") || "Merchant", str("subject_id"), str("reason")));
      }
      break;
    }
  }
  return json(mockError("not_found", "unknown_route", "Unknown mock route."));
}

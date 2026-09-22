// Single dispatcher for the in-app deterministic mock server. All paths
// mirror the portal API surface under /api/mock/v1/...
// NOTE: this catch-all lives at app/api/mock/v1/[...path] so it coexists
// with the dedicated app/api/mock/v1/{payment-links,offers,...} route
// modules. A sibling catch-all at app/api/mock/[...path] is shadowed by
// the static `v1/` segment and never receives /api/mock/v1/* (Next App
// Router). Path segments here are AFTER /v1/ (auth/..., dashboards/...).
// Auth: any email + server-verified mock TOTP; session cookie is an
// HMAC-signed token whose payload carries the merchant persona. Mutations
// require X-BDPay-TOTP step-up and an Idempotency-Key header — except
// login/logout/signup (pre-auth surface).

import { createHash } from "node:crypto";
import { NextRequest, NextResponse } from "next/server";
import type { DisbursementRow, Env, Paginated, Session } from "@/lib/api/types";
import {
  SNAPSHOT_AT,
  approveBatchMock,
  createApiKeyMock,
  createBatchMock,
  createWebhookEndpointMock,
  deleteWebhookEndpointMock,
  findApplication,
  findDispute,
  getDemoApplication,
  getMember,
  getMerchantQrMock,
  getSettingsMock,
  isPersona,
  issueStaticQrMock,
  listApiKeysMock,
  listBatchesMock,
  listDisputesMock,
  listMerchantQrsMock,
  listNotificationsMock,
  listReportsMock,
  listWebhookDeliveriesMock,
  listWebhookEndpointsMock,
  merchantDashboard,
  mockError,
  qrLifecycleMock,
  qrRenderAssetMock,
  rejectBatchMock,
  replayWebhookDeliveryMock,
  revokeApiKeyMock,
  rotateApiKeyMock,
  signupMock,
  submitDisputeEvidenceMock,
  testWebhookEndpointMock,
  uploadKybDocumentMock,
  DEMO_MERCHANT,
  type MockResult,
} from "@/lib/mock/store";
import { mockDisabled } from "@/lib/mock/guard";
import {
  PORTAL_SESSION_COOKIE,
  clearPortalSessionCookieOptions,
  issuePortalSession,
  portalSessionCookieOptions,
  readPortalSessionPersona,
} from "@/lib/mock/session";
import { verifyMockTotp, type MockTotpResult } from "@bdpay/edge/mock/totp";

export const dynamic = "force-dynamic";

const SESSION_COOKIE = PORTAL_SESSION_COOKIE;

function json<T>(result: MockResult<T>): NextResponse {
  return NextResponse.json(result.body, { status: result.status });
}

function page<T>(rows: T[]): NextResponse {
  const body: Paginated<T> = { data: rows, next_cursor: null };
  return NextResponse.json(body, { status: 200 });
}

async function sessionPersona(req: NextRequest): Promise<string> {
  return (await readPortalSessionPersona(req)) ?? "";
}

function unauthenticated(): NextResponse {
  return json(mockError("authentication", "session_required", "Sign in to use the developer portal."));
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

// Rendered QR asset (engine render contract: deterministic bytes, public
// cache, X-BDPay-Content-Sha256 over the exact payload bytes).
function qrAsset(merchantQrId: string, kind: "png" | "svg" | "kit_pdf"): NextResponse {
  const asset = qrRenderAssetMock(merchantQrId, kind);
  if (!asset) return json(mockError("not_found", "qr_not_found", "Merchant QR not found."));
  return new NextResponse(Buffer.from(asset.content), {
    status: 200,
    headers: {
      "Content-Type": asset.mediaType,
      "Cache-Control": "public, max-age=300",
      "X-BDPay-Content-Sha256": createHash("sha256").update(asset.content).digest("hex"),
    },
  });
}

function envOf(q: URLSearchParams): Env {
  return q.get("env") === "live" ? "live" : "sandbox";
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

  const persona = await sessionPersona(req);

  if (p[0] === "auth" && p[1] === "merchant" && p[2] === "session") {
    if (!isPersona(persona)) return unauthenticated();
    const body: Session = {
      member: getMember(persona),
      merchant: DEMO_MERCHANT,
      issued_at: SNAPSHOT_AT,
      absolute_expires_at: new Date(Date.parse(SNAPSHOT_AT) + 8 * 3_600_000).toISOString().replace(".000Z", "Z"),
    };
    return NextResponse.json(body, { status: 200 });
  }

  // Pre-auth applicant view: an applicant tracks their application by id
  // before the merchant account exists.
  if (p[0] === "onboarding" && p[1] === "applications" && typeof p[2] === "string" && p[2] !== "current") {
    const app = findApplication(p[2]);
    return app ? NextResponse.json(app) : json(mockError("not_found", "application_not_found", "No such application."));
  }

  if (!isPersona(persona)) return unauthenticated();

  switch (p[0]) {
    case "onboarding": {
      if (p[1] === "applications" && p[2] === "current") {
        return NextResponse.json(getDemoApplication());
      }
      break;
    }
    case "dashboards": {
      if (p[1] === "merchant") return NextResponse.json(merchantDashboard(envOf(q)));
      break;
    }
    case "api-keys": {
      if (p.length === 1) return page(listApiKeysMock(envOf(q)));
      break;
    }
    case "webhook-endpoints": {
      if (p.length === 1) return page(listWebhookEndpointsMock(envOf(q)));
      break;
    }
    case "webhook-deliveries": {
      if (p.length === 1) return page(listWebhookDeliveriesMock(envOf(q), q.get("webhook_id") ?? ""));
      break;
    }
    case "merchants": {
      // GET /v1/merchants/{merchant_id}/qr-codes — real engine route shape.
      if (p.length === 3 && p[2] === "qr-codes") return json(listMerchantQrsMock(p[1] ?? ""));
      break;
    }
    case "qr-codes": {
      // GET /v1/qr-codes/{merchant_qr_id} and the render asset routes.
      if (p.length === 2) return json(getMerchantQrMock(p[1] ?? ""));
      if (p.length === 3) {
        const qrId = p[1] ?? "";
        if (p[2] === "render.png") return qrAsset(qrId, "png");
        if (p[2] === "render.svg") return qrAsset(qrId, "svg");
        if (p[2] === "kit.pdf") return qrAsset(qrId, "kit_pdf");
      }
      break;
    }
    case "disputes": {
      if (p.length === 1) return page(listDisputesMock({ state: q.get("state") ?? "", env: q.get("env") ?? "" }));
      const found = findDispute(p[1] ?? "");
      return found ? NextResponse.json(found) : json(mockError("not_found", "dispute_not_found", "No such dispute."));
    }
    case "disbursement-batches": {
      if (p.length === 1) return page(listBatchesMock(envOf(q)));
      break;
    }
    case "reports": {
      if (p.length === 1) return page(listReportsMock(envOf(q)));
      break;
    }
    case "notifications": {
      if (p.length === 1) return page(listNotificationsMock());
      break;
    }
    case "settings": {
      if (p.length === 1) return NextResponse.json(getSettingsMock());
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

  let body: Record<string, unknown> = {};
  try {
    body = (await req.json()) as Record<string, unknown>;
  } catch {
    body = {};
  }
  const str = (key: string): string => (typeof body[key] === "string" ? (body[key] as string) : "");
  const strOrNull = (key: string): string | null => (typeof body[key] === "string" ? (body[key] as string) : null);
  const arr = (key: string): string[] =>
    Array.isArray(body[key]) ? (body[key] as unknown[]).filter((x): x is string => typeof x === "string") : [];
  const env: Env = str("env") === "live" ? "live" : "sandbox";

  // --- auth + signup (pre-auth: no session/TOTP-header requirement) --------
  if (p[0] === "auth" && p[1] === "merchant" && p[2] === "login") {
    const email = str("email");
    const totp = str("totp_code");
    const persona = str("persona");
    if (!email.includes("@")) {
      return json(mockError("invalid_request", "email_required", "A valid email is required."));
    }
    const totpDenied = requireTotp(
      totp,
      "totp_invalid",
      "TOTP verification failed. 3 consecutive failures lock the account for 30 minutes (BB ICT §6.2.6).",
    );
    if (totpDenied) return totpDenied;
    if (!isPersona(persona)) {
      return json(mockError("invalid_request", "persona_invalid", "Choose a valid merchant persona."));
    }
    const token = await issuePortalSession(persona);
    if (token === null) {
      return json(
        mockError(
          "internal",
          "mock_session_secret_missing",
          "Mock session signing secret is not configured.",
        ),
      );
    }
    const res = NextResponse.json({ member: getMember(persona) }, { status: 200 });
    res.cookies.set(SESSION_COOKIE, token, portalSessionCookieOptions());
    return res;
  }
  if (p[0] === "auth" && p[1] === "merchant" && p[2] === "logout") {
    const res = NextResponse.json({ ok: true }, { status: 200 });
    res.cookies.set(SESSION_COOKIE, "", clearPortalSessionCookieOptions());
    return res;
  }
  if (p[0] === "merchants" && p[1] === "signup") {
    return json(
      signupMock({
        legal_name: str("legal_name"),
        trade_name: str("trade_name"),
        trade_name_bn: str("trade_name_bn"),
        bin_number: str("bin_number"),
        contact_email: str("contact_email"),
        contact_phone: str("contact_phone"),
        mcc: str("mcc"),
        city: str("city"),
      }),
    );
  }

  // Pre-auth applicant document upload (TOTP header still required — the
  // applicant enrolled TOTP at signup); session optional for this one route.
  const personaStr = await sessionPersona(req);
  const isApplicantUpload = p[0] === "onboarding" && p[1] === "applications" && p[3] === "documents";

  if (!isApplicantUpload && !isPersona(personaStr)) return unauthenticated();

  // TOTP step-up on every authenticated mutation (BB ICT §9.2).
  const stepUpDenied = requireTotp(
    req.headers.get("x-bdpay-totp") ?? "",
    "totp_step_up_required",
    "Mutation requires a valid TOTP step-up code.",
  );
  if (stepUpDenied) return stepUpDenied;
  if (!req.headers.get("idempotency-key")) {
    return json(mockError("invalid_request", "idempotency_key_required", "Mutations require an Idempotency-Key header."));
  }

  const actor = isPersona(personaStr) ? getMember(personaStr) : null;
  const idSeg = p[1] ?? "";
  const action = p[2] ?? "";

  switch (p[0]) {
    case "onboarding": {
      if (isApplicantUpload) {
        return json(uploadKybDocumentMock(p[2] ?? "", str("doc_type"), str("pointer"), str("sha256")));
      }
      break;
    }
    case "api-keys": {
      if (p.length === 1) return json(createApiKeyMock(env, str("key_name"), arr("scopes")));
      if (action === "rotate") return json(rotateApiKeyMock(idSeg));
      if (action === "revoke") return json(revokeApiKeyMock(idSeg));
      break;
    }
    case "webhook-endpoints": {
      if (p.length === 1) return json(createWebhookEndpointMock(env, str("url"), arr("enabled_events"), str("description")));
      if (action === "delete") return json(deleteWebhookEndpointMock(idSeg));
      if (action === "test") return json(testWebhookEndpointMock(idSeg));
      break;
    }
    case "webhook-deliveries": {
      if (action === "replay") return json(replayWebhookDeliveryMock(idSeg));
      break;
    }
    case "merchants": {
      // POST /v1/merchants/{merchant_id}/qr-codes — issue a static QR.
      if (p.length === 3 && p[2] === "qr-codes") {
        return json(issueStaticQrMock(idSeg, str("label"), strOrNull("store_id"), strOrNull("terminal_id")));
      }
      break;
    }
    case "qr-codes": {
      // POST /v1/qr-codes/{merchant_qr_id}/suspend | reactivate | revoke.
      if (action === "suspend" || action === "reactivate" || action === "revoke") {
        return json(qrLifecycleMock(idSeg, action, strOrNull("reason")));
      }
      break;
    }
    case "disputes": {
      if (action === "evidence" && actor) {
        return json(submitDisputeEvidenceMock(idSeg, actor.member_id, str("pointer"), str("sha256"), str("label")));
      }
      break;
    }
    case "disbursement-batches": {
      if (p.length === 1 && actor) {
        const rows = Array.isArray(body["rows"]) ? (body["rows"] as DisbursementRow[]) : [];
        return json(createBatchMock(env, rows, actor));
      }
      if (action === "approve" && actor) return json(approveBatchMock(idSeg, actor));
      if (action === "reject" && actor) return json(rejectBatchMock(idSeg, actor, str("reason")));
      break;
    }
  }
  return json(mockError("not_found", "unknown_route", "Unknown mock route."));
}

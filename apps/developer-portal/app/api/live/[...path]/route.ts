import { NextRequest, NextResponse } from "next/server";
import { demoApiKey, gatewayUrl } from "@bdpay/edge/live-proxy-core";
import { error, gatewayJson, passthrough, proxyGateway } from "@bdpay/edge/live-proxy";
import type {
  ApiKey,
  Env,
  MerchantDashboard,
  MerchantMember,
  MerchantSummary,
  Paginated,
  PaymentIntentSummary,
  Persona,
  SandboxDemoFullflow,
  Session,
} from "@/lib/api/types";
import type { CertificationMatrix, PublicStatusFeed } from "@/lib/api/launchTypes";
import {
  PORTAL_SESSION_COOKIE,
  PORTAL_SESSION_TTL_SECONDS,
  clearPortalSessionCookieOptions,
  issuePortalSession,
  portalSessionCookieOptions,
  readPortalSessionPersona,
} from "@/lib/mock/session";
import {
  isLiveApiKeyMutation,
  liveKeyManagementRequiresMerchantAuthEnvelope,
} from "@/lib/api/live-key-management-guard";
import { verifyLiveMerchantLogin } from "@/lib/api/live-merchant-login";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

interface Ctx {
  params: Promise<{ path: string[] }>;
}

const PERSONA_SET = new Set<string>(["owner", "developer", "finance_maker", "finance_checker"]);

function isPersona(value: string): value is Persona {
  return PERSONA_SET.has(value);
}

async function fetchDemoFlow(req: NextRequest): Promise<SandboxDemoFullflow | NextResponse> {
  const apiKey = demoApiKey();
  if (!apiKey) return error(500, "live_demo_api_key_missing", "Live demo API key is not configured.");
  const url = gatewayUrl(["v1", "sandbox", "demo", "fullflow"], req.nextUrl.search);
  if (url === null) return error(500, "live_gateway_base_missing", "Live gateway base URL is not configured.");
  const upstream = await fetch(url, {
    method: "GET",
    cache: "no-store",
    headers: {
      Accept: "application/json",
      Authorization: `Bearer ${apiKey}`,
    },
  });
  if (!upstream.ok) {
    return passthrough(upstream);
  }
  return (await upstream.json()) as SandboxDemoFullflow;
}

function connectorMap(raw: Record<string, unknown>): Record<string, Record<string, unknown>> {
  const connectors = raw.connectors;
  return connectors && typeof connectors === "object" && !Array.isArray(connectors)
    ? (connectors as Record<string, Record<string, unknown>>)
    : {};
}

function normalizePublicStatus(raw: Record<string, unknown>): PublicStatusFeed {
  const asOf = typeof raw.generated_at === "string" ? raw.generated_at : new Date().toISOString();
  return {
    as_of: asOf,
    data: Object.entries(connectorMap(raw)).map(([connectorId, row]) => ({
      connector_id: connectorId,
      display_name: typeof row.display_name === "string" ? row.display_name : connectorId,
      active_mode: row.active_mode === "SANDBOX" || row.active_mode === "PRODUCTION" ? row.active_mode : "SIMULATOR",
      health_status:
        row.health_status === "HEALTHY" || row.health_status === "UNHEALTHY" || row.health_status === "DEGRADED"
          ? row.health_status
          : "DEGRADED",
      circuit_state:
        row.circuit_state === "CLOSED" || row.circuit_state === "HALF_OPEN" || row.circuit_state === "OPEN"
          ? row.circuit_state
          : "OPEN",
      uptime_24h_bps: typeof row.uptime_24h_bps === "number" ? row.uptime_24h_bps : 0,
      uptime_7d_bps: typeof row.uptime_7d_bps === "number" ? row.uptime_7d_bps : 0,
      last_health_at: typeof row.last_health_at === "string" ? row.last_health_at : asOf,
    })),
  };
}

function normalizeCertificationMatrix(raw: Record<string, unknown>): CertificationMatrix {
  const asOf = typeof raw.generated_at === "string" ? raw.generated_at : new Date().toISOString();
  return {
    as_of: asOf,
    data: Object.entries(connectorMap(raw)).map(([connectorId, row]) => ({
      connector_id: connectorId,
      display_name: typeof row.display_name === "string" ? row.display_name : connectorId,
      certification_status:
        row.certification_status === "CERTIFIED" || row.certification_status === "EXPIRED"
          ? row.certification_status
          : "PENDING",
      last_certified_at: typeof row.last_certified_at === "string" ? row.last_certified_at : null,
      adapter_version: typeof row.adapter_version === "string" ? row.adapter_version : "unknown",
      checks:
        row.checks && typeof row.checks === "object" && !Array.isArray(row.checks)
          ? (row.checks as CertificationMatrix["data"][number]["checks"])
          : {},
      report_hash: typeof row.report_hash === "string" ? row.report_hash : null,
    })),
  };
}

function asEnv(value: unknown): Env {
  return value === "live" ? "live" : "sandbox";
}

// Real backend reads (sandbox demo flow dropped for these two surfaces).
// The gateway returns canonical env labels ("live"/"test"); the portal renders
// the "sandbox" label for test keys, so test -> sandbox here. Secret material is
// already stripped by the gateway view; this normalizer never synthesizes it.
function normalizeApiKey(row: Record<string, unknown>): ApiKey {
  return {
    key_id: String(row.key_id ?? ""),
    env: asEnv(row.env),
    key_prefix: row.key_prefix === "bdpk_live_" ? "bdpk_live_" : "bdpk_test_",
    key_name: String(row.key_name ?? ""),
    scopes: Array.isArray(row.scopes) ? (row.scopes as string[]) : [],
    last_used_at: typeof row.last_used_at === "string" ? row.last_used_at : null,
    created_at: typeof row.created_at === "string" ? row.created_at : "",
    expires_at: typeof row.expires_at === "string" ? row.expires_at : null,
    status:
      row.status === "ROTATION_PENDING" || row.status === "REVOKED" || row.status === "EXPIRED"
        ? row.status
        : "ACTIVE",
  };
}

function normalizeApiKeyPage(raw: Record<string, unknown>): Paginated<ApiKey> {
  const data = Array.isArray(raw.data) ? (raw.data as Record<string, unknown>[]) : [];
  return { data: data.map(normalizeApiKey), next_cursor: null };
}

// Real merchant dashboard: the gateway computes volume/counts/success-rate/
// settlement/recent intents from the real stores, scoped to the authenticated
// merchant. Coerce the recent-intent method to the portal's non-null contract
// (null -> neutral NPSB_IBFT, matching the prior fullflow fallback).
function normalizeDashboard(raw: Record<string, unknown>): MerchantDashboard {
  const settlement =
    raw.settlement && typeof raw.settlement === "object" ? (raw.settlement as Record<string, unknown>) : {};
  const rawIntents = Array.isArray(raw.recent_intents) ? (raw.recent_intents as Record<string, unknown>[]) : [];
  const recent_intents: PaymentIntentSummary[] = rawIntents.map((row) => ({
    payment_intent_id: String(row.payment_intent_id ?? ""),
    env: asEnv(row.env),
    amount_minor: String(row.amount_minor ?? "0"),
    currency: "BDT",
    method: (row.method as PaymentIntentSummary["method"]) ?? "NPSB_IBFT",
    status: (row.status as PaymentIntentSummary["status"]) ?? "CREATED",
    created_at: typeof row.created_at === "string" ? row.created_at : "",
  }));
  return {
    as_of: typeof raw.as_of === "string" ? raw.as_of : new Date().toISOString(),
    env: asEnv(raw.env),
    volume_today_minor: String(raw.volume_today_minor ?? "0"),
    volume_7d_minor: String(raw.volume_7d_minor ?? "0"),
    volume_30d_minor: String(raw.volume_30d_minor ?? "0"),
    count_today: typeof raw.count_today === "number" ? raw.count_today : 0,
    success_rate_bps: typeof raw.success_rate_bps === "number" ? raw.success_rate_bps : 0,
    settlement: {
      pending_minor: String(settlement.pending_minor ?? "0"),
      next_release_at: typeof settlement.next_release_at === "string" ? settlement.next_release_at : "",
      last_settled_minor: String(settlement.last_settled_minor ?? "0"),
      last_settled_at: typeof settlement.last_settled_at === "string" ? settlement.last_settled_at : "",
    },
    recent_intents,
  };
}

function merchantName(flow?: SandboxDemoFullflow): string {
  const displayName = flow?.merchant.display_name;
  const tradeName = flow?.merchant.trade_name;
  return typeof displayName === "string" ? displayName : typeof tradeName === "string" ? tradeName : "BDPay Demo Merchant";
}

function merchantSummary(flow?: SandboxDemoFullflow): MerchantSummary {
  const now = new Date().toISOString();
  const merchantId = typeof flow?.intent.merchant_id === "string" ? flow.intent.merchant_id : process.env.BDPAY_LIVE_DEMO_MERCHANT_ID || "mrch_demo";
  return {
    merchant_id: merchantId,
    legal_name: merchantName(flow),
    trade_name: merchantName(flow),
    trade_name_bn: "বিডিপে ডেমো মার্চেন্ট",
    status: "ACTIVE",
    mcc: "5411",
    city: "Sandbox",
    created_at: flow?.summary.as_of || now,
  };
}

function member(persona: Persona, email = "developer@bdpay.example"): MerchantMember {
  return {
    member_id: `memb_live_${persona}`,
    email,
    display_name: "BDPay Demo Developer",
    persona,
  };
}

function sessionBody(persona: Persona, flow?: SandboxDemoFullflow): Session {
  return {
    member: member(persona),
    merchant: merchantSummary(flow),
    issued_at: new Date().toISOString(),
    absolute_expires_at: new Date(Date.now() + PORTAL_SESSION_TTL_SECONDS * 1000).toISOString(),
  };
}

export async function GET(req: NextRequest, ctx: Ctx): Promise<NextResponse> {
  const { path: p } = await ctx.params;
  if (p[0] !== "v1") return error(404, "unknown_route", "Unknown live route.");

  if (p[1] === "public") {
    if (p[2] === "status" && p.length === 3) {
      const raw = await gatewayJson(req, p);
      return raw instanceof NextResponse ? raw : NextResponse.json(normalizePublicStatus(raw), { status: 200 });
    }
    if (p[2] === "certification-matrix" && p.length === 3) {
      const raw = await gatewayJson(req, p);
      return raw instanceof NextResponse ? raw : NextResponse.json(normalizeCertificationMatrix(raw), { status: 200 });
    }
    return proxyGateway(req, p);
  }

  const persona = await readPortalSessionPersona(req);

  // Merchant browser session (spec/15 §Session decision): the portal keeps its
  // own signed-cookie adapter (issuePortalSession). Login is fail-closed RFC 6238
  // against BDPAY_LIVE_MERCHANT_* env; this is not a full merchant-member store.
  if (p[1] === "auth" && p[2] === "merchant" && p[3] === "session") {
    if (!persona) return error(401, "session_required", "Sign in to use the developer portal.");
    const flow = await fetchDemoFlow(req);
    return flow instanceof NextResponse ? flow : NextResponse.json(sessionBody(persona, flow), { status: 200 });
  }

  if (p[1] === "sandbox" && p[2] === "demo" && p[3] === "fullflow") {
    const flow = await fetchDemoFlow(req);
    return flow instanceof NextResponse ? flow : NextResponse.json(flow, { status: 200 });
  }

  if (!persona) return error(401, "session_required", "Sign in to use the developer portal.");

  // Real backend reads (the demo fullflow shim is NOT used for these surfaces).
  // The portal server authenticates to the gateway with the configured demo
  // merchant API key (Bearer); the gateway scopes the response to that key's
  // merchant_id and returns live store data. Non-2xx upstream responses pass
  // through so the portal can render a clear error (e.g. a demo key missing
  // apikey:read surfaces a 403 rather than fabricated data).
  const token = demoApiKey() || undefined;

  if (p[1] === "api-keys" && p.length === 2) {
    const raw = await gatewayJson(req, p, { token });
    return raw instanceof NextResponse ? raw : NextResponse.json(normalizeApiKeyPage(raw), { status: 200 });
  }
  if (p[1] === "dashboards" && p[2] === "merchant") {
    const raw = await gatewayJson(req, p, { token });
    return raw instanceof NextResponse ? raw : NextResponse.json(normalizeDashboard(raw), { status: 200 });
  }
  return error(404, "unknown_route", "Unknown live route.");
}

export async function POST(req: NextRequest, ctx: Ctx): Promise<NextResponse> {
  const { path: p } = await ctx.params;
  if (p[0] !== "v1") return error(404, "unknown_route", "Unknown live route.");

  if (p[1] === "auth" && p[2] === "merchant" && p[3] === "logout") {
    const res = NextResponse.json({ ok: true }, { status: 200 });
    res.cookies.set(PORTAL_SESSION_COOKIE, "", clearPortalSessionCookieOptions());
    return res;
  }

  // Live API-key mutations must fail closed until the portal has real
  // merchant-member auth/step-up. The secure gateway endpoints remain live for
  // direct merchant API-key callers; this BFF must not mutate through the
  // shared demo key.
  if (isLiveApiKeyMutation("POST", p)) {
    const persona = await readPortalSessionPersona(req);
    if (!persona) return error(401, "session_required", "Sign in to use the developer portal.");
    return NextResponse.json(liveKeyManagementRequiresMerchantAuthEnvelope(), { status: 403 });
  }

  if (p[1] !== "auth" || p[2] !== "merchant" || p[3] !== "login") {
    return error(404, "unknown_route", "Unknown live route.");
  }

  let body: Record<string, unknown> = {};
  try {
    body = (await req.json()) as Record<string, unknown>;
  } catch {
    body = {};
  }
  const email = typeof body.email === "string" ? body.email : "";
  const personaRaw = typeof body.persona === "string" ? body.persona : "";
  const totpCode = typeof body.totp_code === "string" ? body.totp_code : "";
  const decision = await verifyLiveMerchantLogin({
    email,
    totpCode,
    persona: personaRaw,
    env: {
      BDPAY_LIVE_MERCHANT_EMAIL: process.env.BDPAY_LIVE_MERCHANT_EMAIL,
      BDPAY_LIVE_MERCHANT_TOTP_SECRET_B32: process.env.BDPAY_LIVE_MERCHANT_TOTP_SECRET_B32,
    },
  });
  if (!decision.ok) return error(decision.status, decision.code, decision.message);
  const token = await issuePortalSession(decision.persona);
  if (token === null) return error(500, "session_secret_missing", "Session signing secret is not configured.");
  const res = NextResponse.json({ member: member(decision.persona, decision.email) }, { status: 200 });
  res.cookies.set(PORTAL_SESSION_COOKIE, token, portalSessionCookieOptions());
  return res;
}

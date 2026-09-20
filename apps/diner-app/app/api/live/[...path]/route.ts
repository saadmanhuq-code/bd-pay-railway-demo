import { NextRequest, NextResponse } from "next/server";
import { demoApiKey, gatewayUrl } from "@bdpay/edge/live-proxy-core";
import { error, gatewayBaseMissing, passthrough, unknownRoute } from "@bdpay/edge/live-proxy";
import type { DinerSession, Paginated, PaymentIntentView } from "@/lib/api/types";
import {
  DINER_SESSION_COOKIE,
  clearDinerSessionCookieOptions,
  dinerSessionCookieOptions,
  issueDinerSession,
  verifyDinerSession,
} from "@/lib/mock/session";
import { liveJwtSecret, signDinerJwt } from "@/lib/live/jwt";
import { enrichIntentView } from "@/lib/live/history";
import {
  canonicalizePhone,
  challengeRefusalInProduction,
  isProductionMode,
  issueChallenge,
  maskPhone,
  normalizeDigits,
  verifyChallenge,
} from "@/lib/live/otp";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

interface Ctx {
  params: Promise<{ path: string[] }>;
}

const BD_MOBILE_RE = /^(?:\+?880|0)?1[3-9]\d{8}$/;

function customerBody(customerId: string, phoneMasked = ""): DinerSession["customer"] {
  return {
    customer_id: customerId,
    display_name: "BDPay Diner",
    phone_masked: phoneMasked,
  };
}

function sessionBody(customerId: string): DinerSession {
  return {
    customer: customerBody(customerId),
    issued_at: new Date().toISOString(),
    absolute_expires_at: new Date(Date.now() + 12 * 60 * 60 * 1000).toISOString(),
  };
}

async function customerJwt(
  customerId: string,
  scope: string[] = ["payment:read"],
): Promise<string | null> {
  const secret = liveJwtSecret();
  if (!secret) return null;
  const now = Math.floor(Date.now() / 1000);
  return signDinerJwt(
    {
      sub: customerId,
      scope,
      kyc_tier: "REGULAR",
      jti: `diner-live-${customerId}-${now}`,
      iat: now,
      exp: now + 12 * 60 * 60,
    },
    secret,
  );
}

async function authorizedUpstream(
  req: NextRequest,
  path: string[],
): Promise<NextResponse> {
  const sessionToken = req.cookies.get(DINER_SESSION_COOKIE)?.value;
  if (!sessionToken) return error(401, "session_required", "Sign in to use the diner app.");
  const payload = await verifyDinerSession(sessionToken);
  if (!payload) return error(401, "session_required", "Sign in to use the diner app.");

  const token = await customerJwt(payload.sub);
  if (!token) {
    return error(500, "live_jwt_secret_missing", "Live diner JWT signing secret is not configured.");
  }

  const url = gatewayUrl(path, req.nextUrl.search);
  if (url === null) return gatewayBaseMissing();

  const upstream = await fetch(url, {
    method: "GET",
    cache: "no-store",
    headers: {
      Accept: "application/json",
      Authorization: `Bearer ${token}`,
    },
  });

  if (!upstream.ok) {
    return passthrough(upstream);
  }

  // For payment-intents listing, enrich the response with the spec/18 offer
  // block and redemption_state so the History screen renders consistently.
  if (path.length === 2 && path[0] === "v1" && path[1] === "payment-intents") {
    try {
      const page = (await upstream.json()) as Paginated<PaymentIntentView>;
      page.data = page.data.map((intent) => enrichLiveIntent(intent, payload.sub));
      return NextResponse.json(page, { status: 200 });
    } catch {
      // Fall through to passthrough if enrichment fails.
    }
  }

  return passthrough(upstream);
}

function enrichLiveIntent(intent: PaymentIntentView, customerId: string): PaymentIntentView {
  return enrichIntentView(intent, { defaultCustomerId: customerId });
}

export async function GET(req: NextRequest, ctx: Ctx): Promise<NextResponse> {
  const { path: p } = await ctx.params;
  if (p[0] !== "v1") return unknownRoute();

  if (p[1] === "auth" && p[2] === "diner" && p[3] === "session") {
    const session = await requireDinerSession(req);
    if (session === null) return error(401, "session_required", "Sign in to use the diner app.");
    return NextResponse.json(sessionBody(session.sub), { status: 200 });
  }

  // Real product reads — authenticated, proxied to the gateway.
  if (
    (p[1] === "diner" && p[2] === "merchants" && p.length === 3) ||
    (p[1] === "offers" && p[2] === "eligible" && p.length === 3) ||
    (p[1] === "payment-intents" && p.length === 2)
  ) {
    return authorizedUpstream(req, p);
  }

  // Specific payment-intent reads are not part of the current diner live
  // surface; return 404 rather than falling back to demo data.
  return unknownRoute();
}

/**
 * Findings 340/356 (diner-checkout-customer-principal-rejected, dynamic QR
 * lacks the required gateway authorization): the gateway's route policy
 * (`bdpay/gateway/policy.py`) denies customer tokens outright for
 * `payment_intents.create` and `qr.issue_dynamic` (`customer_scopes=None` on
 * both) and additionally requires a merchant key for intent creation
 * (`principal.kind != "merchant_key"` is rejected in
 * `create_payment_intent`). Only `payment_intents.confirm` accepts a customer
 * JWT. These two routes must be forwarded as the configured merchant key.
 */
function isMerchantKeyCheckoutPath(p: string[]): boolean {
  if (p[0] !== "v1") return false;
  if (p[1] === "payment-intents" && p.length === 2) return true;
  if (p[1] === "qr-codes" && p[2] === "dynamic" && p.length === 3) return true;
  return false;
}

/** Confirmation is the one checkout write the gateway policy accepts a
 * customer JWT for (`customer_scopes=("payment:write",)` on
 * `payment_intents.confirm`). */
function isConfirmPath(p: string[]): boolean {
  return p[1] === "payment-intents" && p.length === 4 && p[3] === "confirm" && Boolean(p[2]);
}

/** The three normal checkout POSTs spec/18 gives the diner app. */
export function isCheckoutPath(p: string[]): boolean {
  if (p[0] !== "v1") return false;
  return isMerchantKeyCheckoutPath(p) || isConfirmPath(p);
}

/** Forward a write to the gateway as the authenticated customer principal
 * (confirmation only — see `isConfirmPath`). */
async function authorizedCustomerWrite(
  req: NextRequest,
  path: string[],
  upstreamBody: Record<string, unknown>,
  customerId: string,
): Promise<NextResponse> {
  const token = await customerJwt(customerId, ["payment:read", "payment:write"]);
  if (token === null) {
    return error(500, "live_jwt_secret_missing", "Live diner JWT signing secret is not configured.");
  }
  const url = gatewayUrl(path, req.nextUrl.search);
  if (url === null) {
    return gatewayBaseMissing();
  }
  const upstream = await fetch(url, {
    method: "POST",
    cache: "no-store",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
      Authorization: `Bearer ${token}`,
      // The gateway requires an idempotency key on every payment write.
      "Idempotency-Key":
        req.headers.get("Idempotency-Key") || defaultIdempotencyKey(path, customerId),
    },
    body: JSON.stringify(upstreamBody),
  });
  return passthrough(upstream);
}

/**
 * Forward a write to the gateway as the configured MERCHANT key (intent
 * creation and dynamic-QR issuance — see `isMerchantKeyCheckoutPath`). The
 * key is read server-side only via `demoApiKey()` (env
 * `BDPAY_LIVE_DEMO_API_KEY`, the same variable developer-portal and
 * ops-console already use for their own server-to-gateway calls) and is
 * never sent to the browser. The customer id is the verified session's
 * subject, already folded into `upstreamBody` by the caller — never taken
 * from the client body.
 */
async function authorizedMerchantWrite(
  req: NextRequest,
  path: string[],
  upstreamBody: Record<string, unknown>,
  customerId: string,
): Promise<NextResponse> {
  const apiKey = demoApiKey();
  if (!apiKey) {
    return error(500, "live_demo_api_key_missing", "Live demo API key is not configured.");
  }
  const url = gatewayUrl(path, req.nextUrl.search);
  if (url === null) {
    return gatewayBaseMissing();
  }
  const upstream = await fetch(url, {
    method: "POST",
    cache: "no-store",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
      Authorization: `Bearer ${apiKey}`,
      // The gateway requires an idempotency key on every payment write.
      "Idempotency-Key":
        req.headers.get("Idempotency-Key") || defaultIdempotencyKey(path, customerId),
    },
    body: JSON.stringify(upstreamBody),
  });
  return passthrough(upstream);
}

/**
 * The key used when the client sends no `Idempotency-Key` of its own.
 *
 * Confirmation is idempotent ON THE INTENT: a retried confirm of the same
 * payment intent is the same operation, so its key is derived from the intent
 * and the customer — a fresh random key there would let a retry capture twice.
 * Creating an intent or issuing a dynamic QR is NOT idempotent on its body: two
 * identical carts are two real purchases, so those keep a fresh key rather than
 * being silently deduplicated by the gateway. Clients that need retry-safe
 * creation should send their own key; this is only the fallback.
 */
function defaultIdempotencyKey(path: string[], customerId: string): string {
  if (path.length === 4 && path[3] === "confirm") {
    return `diner-confirm-${customerId}-${path[2]}`;
  }
  return `diner-${path.join("-")}-${crypto.randomUUID()}`;
}

async function requireDinerSession(req: NextRequest): Promise<{ sub: string } | null> {
  const sessionToken = req.cookies.get(DINER_SESSION_COOKIE)?.value;
  if (!sessionToken) return null;
  return verifyDinerSession(sessionToken);
}

export async function POST(req: NextRequest, ctx: Ctx): Promise<NextResponse> {
  const { path: p } = await ctx.params;
  if (p[0] !== "v1") return unknownRoute();

  let body: Record<string, unknown> = {};
  try {
    body = (await req.json()) as Record<string, unknown>;
  } catch {
    body = {};
  }
  const str = (key: string): string => (typeof body[key] === "string" ? (body[key] as string) : "");

  if (p[1] === "auth" && p[2] === "diner") {
    if (p[3] === "otp" && p[4] === "request") {
      const rawPhone = normalizeDigits(str("phone")).replace(/[\s-]/g, "");
      if (!BD_MOBILE_RE.test(rawPhone)) {
        return error(400, "phone_invalid", "A Bangladeshi mobile number is required.");
      }
      // Finding 341: 017…, +88017…, 88017… and 17… are the SAME phone —
      // canonicalize before this spelling ever reaches challenge storage.
      const phone = canonicalizePhone(rawPhone);
      // F08: production refuses a shared fixture OTP outright, and refuses to
      // mint a code it has no way to deliver.
      const refusal = challengeRefusalInProduction();
      if (refusal !== null) return error(500, refusal.code, refusal.message);
      const challenge = await issueChallenge(phone);
      if (!isProductionMode()) {
        // Dev-only, server-side: the code reaches the developer through the
        // server console, never through the response body.
        console.info(`[diner-app] dev OTP for ${maskPhone(phone)}: ${challenge.deliverableCode}`);
      }
      return NextResponse.json(
        { challenge_id: challenge.challengeId, expires_at: challenge.expiresAt },
        { status: 200 },
      );
    }
    if (p[3] === "otp" && p[4] === "verify") {
      const rawPhone = normalizeDigits(str("phone")).replace(/[\s-]/g, "");
      if (!BD_MOBILE_RE.test(rawPhone)) {
        return error(400, "phone_invalid", "A Bangladeshi mobile number is required.");
      }
      // Finding 341: same canonicalization as the request step, so all four
      // spellings resolve to the one challenge, customer id and attempt
      // budget minted above.
      const phone = canonicalizePhone(rawPhone);
      const verified = await verifyChallenge(phone, normalizeDigits(str("otp_code")));
      if (!verified.ok) return error(401, verified.code, "OTP verification failed.");
      const token = await issueDinerSession(verified.customerId);
      if (token === null) return error(500, "session_secret_missing", "Session signing secret is not configured.");
      const res = NextResponse.json(
        { customer: customerBody(verified.customerId, maskPhone(phone)) },
        { status: 200 },
      );
      res.cookies.set(DINER_SESSION_COOKIE, token, dinerSessionCookieOptions());
      return res;
    }
    if (p[3] === "logout") {
      const res = NextResponse.json({ ok: true }, { status: 200 });
      res.cookies.set(DINER_SESSION_COOKIE, "", clearDinerSessionCookieOptions());
      return res;
    }
  }

  // F09 / findings 340, 356: the normal diner checkout the merchant page
  // actually calls — reservation/net-price intent, dynamic-amount QR,
  // confirmation. These used to fall through to 404 (review F09 / evidence
  // E34, E36), then to a customer-JWT forward the gateway policy rejects for
  // two of the three (create and dynamic QR require a merchant key —
  // `bdpay/gateway/policy.py`'s `customer_scopes=None`). Intent creation and
  // dynamic-QR issuance now forward as the configured merchant key;
  // confirmation keeps the customer JWT (the one write the policy accepts
  // one for). Every case: the customer id comes from the verified session
  // cookie, never from the request body.
  if (isCheckoutPath(p)) {
    const session = await requireDinerSession(req);
    if (session === null) return error(401, "session_required", "Sign in to use the diner app.");
    const { customer_id: _ignored, ...clientBody } = body;
    const upstreamBody = { ...clientBody, customer_id: session.sub };
    return isMerchantKeyCheckoutPath(p)
      ? authorizedMerchantWrite(req, p, upstreamBody, session.sub)
      : authorizedCustomerWrite(req, p, upstreamBody, session.sub);
  }

  // SANDBOX-only demo one-tap payment: the server injects the authenticated
  // customer id so the created intent is scoped to this diner's History.
  if (p[1] === "sandbox" && p[2] === "demo" && p[3] === "diner-pay" && p.length === 4) {
    const session = await requireDinerSession(req);
    if (!session) return error(401, "session_required", "Sign in to use the diner app.");

    const url = gatewayUrl(p, req.nextUrl.search);
    if (url === null) return gatewayBaseMissing();

    const idem = req.headers.get("Idempotency-Key") || "";
    const upstreamBody = {
      ...body,
      customer_id: session.sub,
    };

    const upstream = await fetch(url, {
      method: "POST",
      cache: "no-store",
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
        "Idempotency-Key": idem,
      },
      body: JSON.stringify(upstreamBody),
    });

    return passthrough(upstream);
  }

  return unknownRoute();
}

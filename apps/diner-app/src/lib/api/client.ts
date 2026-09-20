// Typed fetch client. GET helpers + named mutation functions ONLY.
// Every mutation posts an Idempotency-Key header (conventions §4) and is
// registered in src/lib/api/mutations.ts (no-write-guard manifest).
// This file is the ONLY module allowed to issue non-GET fetches
// (enforced by scripts/no-write-guard.mjs at build time).
//
// Auth posture (spec/18 + errata S18-E16/S18-E18): every diner surface runs
// under the customer principal (customer JWT at the real gateway; an
// httpOnly session cookie in the dev mock). Offer discovery requires the
// customer scope `payment:read`; reservation (D1) refuses guests with
// 401 authentication/offer_requires_login.

import type {
  CheckoutMethod,
  DinerMerchant,
  DinerPayResult,
  DinerSession,
  DynamicQr,
  EligibleOffer,
  ErrorEnvelope,
  OtpChallenge,
  Paginated,
  PaymentIntentView,
  SandboxDemoFullflow,
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

export class ApiError extends Error {
  readonly status: number;
  readonly envelope: ErrorEnvelope | null;

  constructor(status: number, envelope: ErrorEnvelope | null) {
    super(envelope?.error.message ?? `request failed with status ${status}`);
    this.status = status;
    this.envelope = envelope;
  }

  get code(): string | null {
    return this.envelope?.error.code ?? null;
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
  const qs = params ? Object.entries(params).filter(([, v]) => v !== "") : [];
  const query = qs.length > 0 ? `?${new URLSearchParams(Object.fromEntries(qs)).toString()}` : "";
  const res = await fetch(`${BASE}${path}${query}`, {
    method: "GET",
    credentials: "same-origin",
    headers: { Accept: "application/json" },
  });
  if (!res.ok) return parseError(res);
  return (await res.json()) as T;
}

async function mutate<T>(path: string, body: Record<string, unknown>): Promise<T> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    Accept: "application/json",
    "Idempotency-Key": newIdempotencyKey(),
  };
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
  // Client-supplied idempotency keys (conventions §3 note) — browser-side
  // randomness is fine here; the deterministic-ID rule binds server IDs.
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return crypto.randomUUID();
  }
  const t = Date.now().toString(16);
  return `idem-${t}-${Math.floor(performance.now() * 1000).toString(16)}`;
}

// ---------------------------------------------------------------------------
// Session / auth (D1 diner login — BD mobile + OTP; errata S18-E18)
// ---------------------------------------------------------------------------

export function getDinerSession(): Promise<DinerSession> {
  return apiGet<DinerSession>("/v1/auth/diner/session");
}

export function requestOtp(phone: string): Promise<OtpChallenge> {
  return mutate<OtpChallenge>("/v1/auth/diner/otp/request", { phone });
}

export function verifyOtp(phone: string, otpCode: string): Promise<{ customer: DinerSession["customer"] }> {
  return mutate("/v1/auth/diner/otp/verify", { phone, otp_code: otpCode });
}

export function dinerLogout(): Promise<{ ok: boolean }> {
  return mutate<{ ok: boolean }>("/v1/auth/diner/logout", {});
}

// ---------------------------------------------------------------------------
// Discovery (merchant browse + spec/18 GET /v1/offers/eligible)
// ---------------------------------------------------------------------------

// Restaurant directory. spec/18 keeps discovery UX (maps/geo/ranking) out of
// scope until activation; this is the activation-time directory seam — the
// dev mock serves a deterministic list (errata S18-E17).
export function listDinerMerchants(params: { area?: string; q?: string }): Promise<Paginated<DinerMerchant>> {
  return apiGet("/v1/diner/merchants", { area: params.area ?? "", q: params.q ?? "" });
}

export function eligibleOffers(params: {
  merchant_id: string;
  method?: CheckoutMethod | "";
  example_amount_minor?: number | null;
  at?: string;
}): Promise<{ data: EligibleOffer[] }> {
  return apiGet("/v1/offers/eligible", {
    merchant_id: params.merchant_id,
    method: params.method ?? "",
    example_amount_minor:
      params.example_amount_minor != null ? String(params.example_amount_minor) : "",
    at: params.at ?? "",
  });
}

// ---------------------------------------------------------------------------
// Reservation -> payment (spec/18 extension to POST /v1/payment-intents)
// ---------------------------------------------------------------------------

export function createOfferIntent(params: {
  merchant_id: string;
  offer_id: string;
  gross_amount_minor: number;
  method: CheckoutMethod;
}): Promise<PaymentIntentView> {
  return mutate<PaymentIntentView>("/v1/payment-intents", {
    merchant_id: params.merchant_id,
    offer_id: params.offer_id,
    gross_amount_minor: params.gross_amount_minor,
    method: params.method,
    currency: "BDT",
  });
}

export function getPaymentIntent(paymentIntentId: string): Promise<PaymentIntentView> {
  return apiGet(`/v1/payment-intents/${encodeURIComponent(paymentIntentId)}`);
}

// Customer-scoped listing — the gateway scopes the customer principal to its
// own intents; the offer block rides each row (errata S18-E19: redemption
// history is this listing, no new endpoint minted).
export function listMyPaymentIntents(params: { cursor?: string }): Promise<Paginated<PaymentIntentView>> {
  return apiGet("/v1/payment-intents", { cursor: params.cursor ?? "" });
}

export function confirmPaymentIntent(paymentIntentId: string): Promise<PaymentIntentView> {
  return mutate<PaymentIntentView>(
    `/v1/payment-intents/${encodeURIComponent(paymentIntentId)}/confirm`,
    {},
  );
}

// ---------------------------------------------------------------------------
// Dynamic QR handoff (spec/13; offers REQUIRE dynamic-amount QR — spec/18
// regulatory mapping: static-QR-only merchants cannot run offers)
// ---------------------------------------------------------------------------

export function issueDynamicQr(paymentIntentId: string): Promise<DynamicQr> {
  return mutate<DynamicQr>("/v1/qr-codes/dynamic", {
    payment_intent_id: paymentIntentId,
    ttl_seconds: 300,
  });
}

export function getSandboxDemoFullflow(): Promise<SandboxDemoFullflow> {
  return apiGet("/v1/sandbox/demo/fullflow");
}

// Sandbox-only demo: one-tap create→confirm→capture through the simulator.
// This is NOT a production customer-initiated payment surface; it exists only
// in SANDBOX_PUBLIC deployments (spec/16 LR-4 §F demo family).
export function demoDinerPay(params: {
  merchant_id: string;
  amount_minor: number;
  payment_method?: CheckoutMethod;
  offer_id?: string;
}): Promise<DinerPayResult> {
  return mutate<DinerPayResult>("/v1/sandbox/demo/diner-pay", {
    merchant_id: params.merchant_id,
    amount_minor: params.amount_minor,
    payment_method: params.payment_method ?? "BANGLA_QR",
    offer_id: params.offer_id ?? "",
  });
}

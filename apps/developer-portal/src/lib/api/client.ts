// Typed fetch client. GET helpers + named mutation functions ONLY.
// Every mutation:
//   - takes a totpCode argument (TOTP step-up per BB ICT §9.2; merchant
//     mutations require step-up per spec/15 §Session — read-only does not),
//   - posts an Idempotency-Key header (conventions §4),
//   - is registered in src/lib/api/mutations.ts (no-write-guard manifest).
// This file is the ONLY module allowed to issue non-GET fetches
// (enforced by scripts/no-write-guard.mjs at build time).

import type {
  ApiKey,
  ApiKeyCreated,
  ApiKeyRotated,
  DisbursementBatch,
  DisbursementRow,
  Dispute,
  Env,
  ErrorEnvelope,
  LoginResult,
  MerchantDashboard,
  MerchantSettings,
  OnboardingApplication,
  MerchantQr,
  MerchantQrList,
  Paginated,
  Persona,
  PortalNotification,
  ReportItem,
  SandboxDemoFullflow,
  StaticQrIssued,
  Session,
  SignupResult,
  WebhookDelivery,
  WebhookEndpoint,
  WebhookEndpointCreated,
} from "./types";
import type {
  CertificationMatrix,
  CheckoutMethod,
  PaymentLink,
  PublicLinkIntentResult,
  PublicPaymentLinkView,
  PublicStatusFeed,
  SandboxSignupCreated,
  SandboxSignupVerified,
} from "./launchTypes";
import type { CreateOfferInput, EditOfferInput, Offer, OfferDetail } from "./offerTypes";

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
// Session / auth (merchant browser session per spec/15 §Session decision)
// ---------------------------------------------------------------------------

export function getSession(): Promise<Session> {
  return apiGet<Session>("/v1/auth/merchant/session");
}

export function login(email: string, password: string, totpCode: string, persona: Persona): Promise<LoginResult> {
  return mutate<LoginResult>("/v1/auth/merchant/login", {
    email,
    password,
    totp_code: totpCode,
    persona,
  });
}

export function logout(): Promise<{ ok: boolean }> {
  return mutate<{ ok: boolean }>("/v1/auth/merchant/logout", {});
}

// ---------------------------------------------------------------------------
// Self-onboarding (spec/08 — pre-auth signup, then KYB document checklist)
// ---------------------------------------------------------------------------

export interface SignupProfile {
  legal_name: string;
  trade_name: string;
  trade_name_bn: string;
  bin_number: string;
  contact_email: string;
  contact_phone: string;
  mcc: string;
  city: string;
}

export function signup(profile: SignupProfile): Promise<SignupResult> {
  return mutate<SignupResult>("/v1/merchants/signup", { ...profile });
}

export function getOnboardingApplication(applicationId: string): Promise<OnboardingApplication> {
  return apiGet(`/v1/onboarding/applications/${encodeURIComponent(applicationId)}`);
}

export function getMyOnboardingApplication(): Promise<OnboardingApplication> {
  return apiGet("/v1/onboarding/applications/current");
}

export function uploadKybDocument(
  applicationId: string,
  docType: string,
  pointer: string,
  sha256: string,
  totpCode: string,
): Promise<OnboardingApplication> {
  return mutate(
    `/v1/onboarding/applications/${encodeURIComponent(applicationId)}/documents`,
    { doc_type: docType, pointer, sha256 },
    { totpCode },
  );
}

// ---------------------------------------------------------------------------
// Dashboard
// ---------------------------------------------------------------------------

export function getMerchantDashboard(env: Env): Promise<MerchantDashboard> {
  return apiGet("/v1/dashboards/merchant", { env });
}

// ---------------------------------------------------------------------------
// API keys (spec/01 §B; revoke is modeled as a sub-resource POST because the
// portal client is restricted to GET/POST by the no-write-guard)
// ---------------------------------------------------------------------------

export function listApiKeys(env: Env): Promise<Paginated<ApiKey>> {
  return apiGet("/v1/api-keys", { env });
}

export function createApiKey(env: Env, keyName: string, scopes: string[], totpCode: string): Promise<ApiKeyCreated> {
  return mutate("/v1/api-keys", { env, key_name: keyName, scopes }, { totpCode });
}

export function rotateApiKey(keyId: string, totpCode: string): Promise<ApiKeyRotated> {
  // In-place immediate rotation: the gateway replaces the secret hash in place
  // (old secret stops working at once); no grace_period_hours is sent.
  return mutate(`/v1/api-keys/${encodeURIComponent(keyId)}/rotate`, {}, { totpCode });
}

export function revokeApiKey(keyId: string, totpCode: string): Promise<ApiKey> {
  return mutate(`/v1/api-keys/${encodeURIComponent(keyId)}/revoke`, {}, { totpCode });
}

// ---------------------------------------------------------------------------
// Webhook endpoints + deliveries (spec/01 §I)
// ---------------------------------------------------------------------------

export function listWebhookEndpoints(env: Env): Promise<Paginated<WebhookEndpoint>> {
  return apiGet("/v1/webhook-endpoints", { env });
}

export function listWebhookDeliveries(env: Env, webhookId?: string): Promise<Paginated<WebhookDelivery>> {
  return apiGet("/v1/webhook-deliveries", { env, webhook_id: webhookId ?? "" });
}

export function createWebhookEndpoint(
  env: Env,
  url: string,
  enabledEvents: string[],
  description: string,
  totpCode: string,
): Promise<WebhookEndpointCreated> {
  return mutate("/v1/webhook-endpoints", { env, url, enabled_events: enabledEvents, description }, { totpCode });
}

export function deleteWebhookEndpoint(webhookId: string, totpCode: string): Promise<WebhookEndpoint> {
  return mutate(`/v1/webhook-endpoints/${encodeURIComponent(webhookId)}/delete`, {}, { totpCode });
}

export function testWebhookEndpoint(webhookId: string, totpCode: string): Promise<WebhookDelivery> {
  return mutate(`/v1/webhook-endpoints/${encodeURIComponent(webhookId)}/test`, {}, { totpCode });
}

export function replayWebhookDelivery(deliveryId: string, totpCode: string): Promise<WebhookDelivery> {
  return mutate(`/v1/webhook-deliveries/${encodeURIComponent(deliveryId)}/replay`, {}, { totpCode });
}

// ---------------------------------------------------------------------------
// Bangla QR (spec/13 — real engine routes). Static QRs are issued under the
// merchant resource; lifecycle actions are sub-resource POSTs on
// /v1/qr-codes/{merchant_qr_id}. TLV payloads come from the engine ONLY.
// ---------------------------------------------------------------------------

export function listMerchantQrs(merchantId: string): Promise<MerchantQrList> {
  return apiGet(`/v1/merchants/${encodeURIComponent(merchantId)}/qr-codes`);
}

export function getMerchantQr(merchantQrId: string): Promise<MerchantQr> {
  return apiGet(`/v1/qr-codes/${encodeURIComponent(merchantQrId)}`);
}

export function issueStaticQr(
  merchantId: string,
  label: string,
  storeId: string | null,
  terminalId: string | null,
  totpCode: string,
): Promise<StaticQrIssued> {
  return mutate(
    `/v1/merchants/${encodeURIComponent(merchantId)}/qr-codes`,
    { label, store_id: storeId, terminal_id: terminalId },
    { totpCode },
  );
}

export function suspendQr(merchantQrId: string, reason: string | null, totpCode: string): Promise<MerchantQr> {
  return mutate(`/v1/qr-codes/${encodeURIComponent(merchantQrId)}/suspend`, { reason }, { totpCode });
}

export function reactivateQr(merchantQrId: string, totpCode: string): Promise<MerchantQr> {
  return mutate(`/v1/qr-codes/${encodeURIComponent(merchantQrId)}/reactivate`, {}, { totpCode });
}

export function revokeQr(merchantQrId: string, reason: string | null, totpCode: string): Promise<MerchantQr> {
  return mutate(`/v1/qr-codes/${encodeURIComponent(merchantQrId)}/revoke`, { reason }, { totpCode });
}

// Rendered assets (PNG/SVG/kit PDF) are consumed as <img>/<a href> targets,
// not via fetch; render_urls in responses are BASE-relative engine paths.
export function qrAssetUrl(renderPath: string): string {
  return `${BASE}${renderPath}`;
}

// ---------------------------------------------------------------------------
// Disputes — merchant side (spec/15 dispute routes)
// ---------------------------------------------------------------------------

export function listDisputes(params: { state?: string; env?: Env }): Promise<Paginated<Dispute>> {
  return apiGet("/v1/disputes", { state: params.state ?? "", env: params.env ?? "" });
}

export function getDispute(dsptId: string): Promise<Dispute> {
  return apiGet(`/v1/disputes/${encodeURIComponent(dsptId)}`);
}

export function submitDisputeEvidence(
  dsptId: string,
  pointer: string,
  sha256: string,
  label: string,
  totpCode: string,
): Promise<Dispute> {
  return mutate(`/v1/disputes/${encodeURIComponent(dsptId)}/evidence`, { pointer, sha256, label }, { totpCode });
}

// ---------------------------------------------------------------------------
// Bulk disbursement (maker-checker; spec/15 bulk_disbursement_release)
// ---------------------------------------------------------------------------

export function listDisbursementBatches(env: Env): Promise<Paginated<DisbursementBatch>> {
  return apiGet("/v1/disbursement-batches", { env });
}

export function createDisbursementBatch(env: Env, rows: DisbursementRow[], totpCode: string): Promise<DisbursementBatch> {
  return mutate("/v1/disbursement-batches", { env, rows: rows as unknown as Record<string, unknown>[] }, { totpCode });
}

export function approveDisbursementBatch(batchId: string, totpCode: string): Promise<DisbursementBatch> {
  return mutate(`/v1/disbursement-batches/${encodeURIComponent(batchId)}/approve`, {}, { totpCode });
}

export function rejectDisbursementBatch(batchId: string, reason: string, totpCode: string): Promise<DisbursementBatch> {
  return mutate(`/v1/disbursement-batches/${encodeURIComponent(batchId)}/reject`, { reason }, { totpCode });
}

// ---------------------------------------------------------------------------
// Reports & notifications & settings (read-only)
// ---------------------------------------------------------------------------

export function listReports(env: Env): Promise<Paginated<ReportItem>> {
  return apiGet("/v1/reports", { env });
}

export function listNotifications(): Promise<Paginated<PortalNotification>> {
  return apiGet("/v1/notifications");
}

export function getMerchantSettings(): Promise<MerchantSettings> {
  return apiGet("/v1/settings");
}

// ---------------------------------------------------------------------------
// Public surfaces (spec/16 §E) — the status page and certification-badge UI
// render EXCLUSIVELY from these three endpoints; no second data path.
// ---------------------------------------------------------------------------

export function getPublicStatus(): Promise<PublicStatusFeed> {
  return apiGet("/v1/public/status");
}

export function getPublicCertificationMatrix(): Promise<CertificationMatrix> {
  return apiGet("/v1/public/certification-matrix");
}

// The badge endpoint is consumed as an <img> src, not via fetch.
export function publicBadgeUrl(connectorId: string): string {
  return `${BASE}/v1/public/badges/${encodeURIComponent(connectorId)}.svg`;
}

// ---------------------------------------------------------------------------
// Payment links (spec/16 §C) — merchant CRUD + public hosted-checkout calls
// ---------------------------------------------------------------------------

export interface CreatePaymentLinkInput {
  amount_minor: string | null;
  amount_min_minor: string | null;
  amount_max_minor: string | null;
  description: string;
  single_use: boolean;
  expiry_hours: number | null;
}

export function listPaymentLinks(env: Env): Promise<Paginated<PaymentLink>> {
  return apiGet("/v1/payment-links", { env });
}

export function getPaymentLink(plinkId: string): Promise<PaymentLink> {
  return apiGet(`/v1/payment-links/${encodeURIComponent(plinkId)}`);
}

export function createPaymentLink(env: Env, input: CreatePaymentLinkInput, totpCode: string): Promise<PaymentLink> {
  return mutate("/v1/payment-links", { env, ...input }, { totpCode });
}

export function cancelPaymentLink(plinkId: string, totpCode: string): Promise<PaymentLink> {
  return mutate(`/v1/payment-links/${encodeURIComponent(plinkId)}/cancel`, {}, { totpCode });
}

// Public (no auth, no TOTP — spec/16 §C: idempotency is server-generated
// from public_code + payer inputs; the client Idempotency-Key is ignored).
export function getPublicPaymentLink(publicCode: string): Promise<PublicPaymentLinkView> {
  return apiGet(`/v1/public/payment-links/${encodeURIComponent(publicCode)}`);
}

export function createPublicLinkIntent(
  publicCode: string,
  amountMinor: string | null,
  method: CheckoutMethod,
): Promise<PublicLinkIntentResult> {
  return mutate(`/v1/public/payment-links/${encodeURIComponent(publicCode)}/intents`, {
    amount_minor: amountMinor,
    method,
  });
}

// ---------------------------------------------------------------------------
// Dining-wedge offers (spec/18 — real /v1/offers surface, routes_offers.py).
// No env parameter: an offer is keyed to the merchant API key's environment
// (errata S18-E19). The gateway create model is extra="forbid" — bodies carry
// EXACTLY the spec/18 fields. The Idempotency-Key header added by mutate()
// is the off_<sha> preimage's created_idem_key on the real engine.
// ---------------------------------------------------------------------------

export function listOffers(state?: string): Promise<Paginated<Offer>> {
  return apiGet("/v1/offers", { state: state ?? "" });
}

// Includes the live counter readout {redeemed_today, redeemed_total,
// reserved_now} — counts only, never customer identities (PDPO posture).
export function getOffer(offerId: string): Promise<OfferDetail> {
  return apiGet(`/v1/offers/${encodeURIComponent(offerId)}`);
}

export function createOffer(input: CreateOfferInput, totpCode: string): Promise<Offer> {
  return mutate("/v1/offers", { ...input }, { totpCode });
}

export function activateOffer(offerId: string, totpCode: string): Promise<Offer> {
  return mutate(`/v1/offers/${encodeURIComponent(offerId)}/activate`, {}, { totpCode });
}

export function pauseOffer(offerId: string, totpCode: string): Promise<Offer> {
  return mutate(`/v1/offers/${encodeURIComponent(offerId)}/pause`, {}, { totpCode });
}

export function getSandboxDemoFullflow(): Promise<SandboxDemoFullflow> {
  return apiGet("/v1/sandbox/demo/fullflow");
}

export function resumeOffer(offerId: string, totpCode: string): Promise<Offer> {
  return mutate(`/v1/offers/${encodeURIComponent(offerId)}/resume`, {}, { totpCode });
}

export function archiveOffer(offerId: string, totpCode: string): Promise<Offer> {
  return mutate(`/v1/offers/${encodeURIComponent(offerId)}/archive`, {}, { totpCode });
}

// Edit-by-versioning (errata S18-E5/E17): economic changes mint version n+1;
// title/title_bn mutate in place. Sub-resource POST because portal clients
// are GET/POST-only (no-write-guard) — same pattern as webhook /delete.
export function editOffer(offerId: string, changes: EditOfferInput, totpCode: string): Promise<Offer> {
  return mutate(`/v1/offers/${encodeURIComponent(offerId)}/edit`, { ...changes }, { totpCode });
}

// ---------------------------------------------------------------------------
// Sandbox signup (spec/16 §F) — public pre-auth surface, no TOTP step-up
// ---------------------------------------------------------------------------

export function createSandboxSignup(email: string, displayName: string): Promise<SandboxSignupCreated> {
  return mutate("/v1/sandbox/signups", { email, display_name: displayName });
}

export function verifySandboxSignup(sbxsId: string, otp: string): Promise<SandboxSignupVerified> {
  return mutate(`/v1/sandbox/signups/${encodeURIComponent(sbxsId)}/verify`, { otp });
}

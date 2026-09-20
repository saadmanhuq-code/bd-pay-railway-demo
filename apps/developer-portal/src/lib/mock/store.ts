// In-app deterministic mock state for the developer portal. All seed data
// derives from fixed constants — no Math.random / Date.now anywhere in the
// data path. Mutations are FSM-consistent and held in module scope so the
// maker-checker and rotate/revoke flows are demo-able within one server
// process.

import type {
  ApiKey,
  ApiKeyCreated,
  ApiKeyRotated,
  DisbursementBatch,
  DisbursementRow,
  Dispute,
  Env,
  ErrorEnvelope,
  ErrorType,
  KybDocument,
  KybState,
  MerchantDashboard,
  MerchantMember,
  MerchantQr,
  MerchantSettings,
  MerchantSummary,
  OnboardingApplication,
  PaymentIntentSummary,
  Persona,
  PortalNotification,
  QrRenderUrls,
  ReportItem,
  StaticQrIssued,
  WebhookDelivery,
  WebhookEndpoint,
  WebhookEndpointCreated,
} from "@/lib/api/types";

// ---------------------------------------------------------------------------
// Deterministic helpers
// ---------------------------------------------------------------------------

const SEED = "bdpay-developer-portal-mock-seed-v1";

/** FNV-1a derived deterministic hex id suffix.
 *
 * Defect fixed 2026-06-13 (spec/18 portal lane, errata S18-E20): the
 * previous expansion loop only ever read the first `length/4` characters of
 * `src` — all inside the constant SEED prefix — so EVERY id in the mock
 * shared one digest (off_/pi_/dspt_/… rows were indistinguishable by id and
 * React keys collided). The digest now absorbs the full input first
 * (true FNV-1a over every char, 32-bit-exact via Math.imul), then expands
 * deterministically to the requested length.
 */
export function detHex(input: string, length = 24): string {
  let h = 0x811c9dc5;
  const src = `${SEED}:${input}`;
  for (let i = 0; i < src.length; i++) {
    h ^= src.charCodeAt(i) & 0xff;
    h = Math.imul(h, 0x01000193) >>> 0;
  }
  let out = "";
  let round = 0;
  while (out.length < length) {
    h ^= round & 0xff;
    h = Math.imul(h, 0x01000193) >>> 0;
    out += (h & 0xffff).toString(16).padStart(4, "0");
    round++;
  }
  return out.slice(0, length);
}

function id(prefix: string, key: string): string {
  return `${prefix}_${detHex(key)}`;
}

function sha(key: string): string {
  return detHex(`sha256:${key}`, 64);
}

// Fixed mock "now": every seeded timestamp is anchored to this constant.
export const SNAPSHOT_AT = "2026-06-12T09:00:00Z";
const EPOCH_MS = Date.parse(SNAPSHOT_AT);

function tsAt(offsetMinutes: number): string {
  return new Date(EPOCH_MS + offsetMinutes * 60_000).toISOString().replace(".000Z", "Z");
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
// Merchant + members (one demo merchant, one member per persona)
// ---------------------------------------------------------------------------

export const DEMO_MERCHANT: MerchantSummary = {
  merchant_id: id("mrch", "dhaka-mart"),
  legal_name: "Dhaka Mart Limited",
  trade_name: "Dhaka Mart",
  trade_name_bn: "ঢাকা মার্ট",
  status: "ACTIVE",
  mcc: "5411",
  city: "Dhaka",
  created_at: tsAt(-60 * 24 * 220),
};

const MEMBERS: Record<Persona, MerchantMember> = {
  owner: {
    member_id: id("mmbr", "owner"),
    email: "rahim.uddin@dhakamart.example",
    display_name: "Rahim Uddin",
    persona: "owner",
  },
  developer: {
    member_id: id("mmbr", "developer"),
    email: "salma.akter@dhakamart.example",
    display_name: "Salma Akter",
    persona: "developer",
  },
  finance_maker: {
    member_id: id("mmbr", "finance-maker"),
    email: "jamil.hasan@dhakamart.example",
    display_name: "Jamil Hasan",
    persona: "finance_maker",
  },
  finance_checker: {
    member_id: id("mmbr", "finance-checker"),
    email: "farida.begum@dhakamart.example",
    display_name: "Farida Begum",
    persona: "finance_checker",
  },
};

export function getMember(persona: Persona): MerchantMember {
  return MEMBERS[persona];
}

export function isPersona(v: string): v is Persona {
  return v === "owner" || v === "developer" || v === "finance_maker" || v === "finance_checker";
}

// ---------------------------------------------------------------------------
// Onboarding applications (spec/08 KYB FSM)
// ---------------------------------------------------------------------------

const KYB_DOC_TYPES: { doc_type: string; label_en: string; label_bn: string }[] = [
  { doc_type: "trade_license", label_en: "Trade licence", label_bn: "ট্রেড লাইসেন্স" },
  { doc_type: "bin_certificate", label_en: "BIN certificate", label_bn: "বিআইএন সনদ" },
  { doc_type: "nid_signatory", label_en: "NID of authorized signatory", label_bn: "অনুমোদিত স্বাক্ষরকারীর এনআইডি" },
  { doc_type: "bank_statement", label_en: "Bank statement (3 months)", label_bn: "ব্যাংক স্টেটমেন্ট (৩ মাস)" },
  { doc_type: "tin_certificate", label_en: "TIN certificate", label_bn: "টিআইএন সনদ" },
];

function seededDoc(docType: string, labelEn: string, labelBn: string, status: KybDocument["status"], offset: number): KybDocument {
  if (status === "REQUIRED") {
    return { doc_type: docType, label_en: labelEn, label_bn: labelBn, status, pointer: null, sha256: null, uploaded_at: null };
  }
  return {
    doc_type: docType,
    label_en: labelEn,
    label_bn: labelBn,
    status,
    pointer: `s3://bdpay-kyb/${detHex(`kyb:${docType}`, 12)}/${docType}.pdf`,
    sha256: sha(`kyb:${docType}`),
    uploaded_at: tsAt(offset),
  };
}

const demoApplication: OnboardingApplication = {
  application_id: id("onap", "dhaka-mart"),
  merchant_id: DEMO_MERCHANT.merchant_id,
  legal_name: DEMO_MERCHANT.legal_name,
  trade_name: DEMO_MERCHANT.trade_name,
  trade_name_bn: DEMO_MERCHANT.trade_name_bn,
  bin_number: "004512873-0205",
  contact_email: "rahim.uddin@dhakamart.example",
  contact_phone: "+8801711000111",
  mcc: "5411",
  city: "Dhaka",
  kyb_status: "ACTIVE",
  state_history: [
    { state: "APPLICATION_SUBMITTED", at: tsAt(-60 * 24 * 230) },
    { state: "DOCUMENTS_PENDING", at: tsAt(-60 * 24 * 229) },
    { state: "KYB_IN_PROGRESS", at: tsAt(-60 * 24 * 227) },
    { state: "SANCTIONS_REVIEW", at: tsAt(-60 * 24 * 225) },
    { state: "RISK_ASSESSED", at: tsAt(-60 * 24 * 224) },
    { state: "AGREEMENT_PENDING", at: tsAt(-60 * 24 * 222) },
    { state: "ACTIVE", at: tsAt(-60 * 24 * 220) },
  ],
  documents: [
    seededDoc("trade_license", "Trade licence", "ট্রেড লাইসেন্স", "ACCEPTED", -60 * 24 * 228),
    seededDoc("bin_certificate", "BIN certificate", "বিআইএন সনদ", "ACCEPTED", -60 * 24 * 228),
    seededDoc("nid_signatory", "NID of authorized signatory", "অনুমোদিত স্বাক্ষরকারীর এনআইডি", "ACCEPTED", -60 * 24 * 228),
    seededDoc("bank_statement", "Bank statement (3 months)", "ব্যাংক স্টেটমেন্ট (৩ মাস)", "ACCEPTED", -60 * 24 * 227),
    seededDoc("trade_license_renewal", "Trade licence renewal (FY 2026-27)", "ট্রেড লাইসেন্স নবায়ন (অর্থবছর ২০২৬-২৭)", "REQUIRED", 0),
  ],
  created_at: tsAt(-60 * 24 * 230),
  updated_at: tsAt(-60 * 24 * 220),
  schema_version: 1,
};

const applications: OnboardingApplication[] = [demoApplication];
let signupSeq = 0;

export function getDemoApplication(): OnboardingApplication {
  return demoApplication;
}

export function findApplication(applicationId: string): OnboardingApplication | undefined {
  return applications.find((a) => a.application_id === applicationId);
}

export interface SignupInput {
  legal_name: string;
  trade_name: string;
  trade_name_bn: string;
  bin_number: string;
  contact_email: string;
  contact_phone: string;
  mcc: string;
  city: string;
}

export function signupMock(input: SignupInput): MockResult<{ application_id: string; kyb_status: KybState }> {
  if (input.legal_name.trim() === "" || input.trade_name.trim() === "") {
    return mockError("invalid_request", "legal_name_required", "legal_name and trade_name are required.");
  }
  if (!input.contact_email.includes("@")) {
    return mockError("invalid_request", "email_invalid", "A valid contact email is required.");
  }
  if (!/^\d{9,15}(-\d{2,6})?$/.test(input.bin_number)) {
    return mockError("invalid_request", "bin_invalid", "BIN must be 9-15 digits with an optional branch suffix.");
  }
  signupSeq += 1;
  const at = mutationTs();
  const app: OnboardingApplication = {
    application_id: id("onap", `signup:${signupSeq}`),
    merchant_id: id("mrch", `signup:${signupSeq}`),
    legal_name: input.legal_name,
    trade_name: input.trade_name,
    trade_name_bn: input.trade_name_bn,
    bin_number: input.bin_number,
    contact_email: input.contact_email,
    contact_phone: input.contact_phone,
    mcc: input.mcc,
    city: input.city,
    kyb_status: "DOCUMENTS_PENDING",
    state_history: [
      { state: "APPLICATION_SUBMITTED", at },
      { state: "DOCUMENTS_PENDING", at },
    ],
    documents: KYB_DOC_TYPES.map((d) => seededDoc(d.doc_type, d.label_en, d.label_bn, "REQUIRED", 0)),
    created_at: at,
    updated_at: at,
    schema_version: 1,
  };
  applications.push(app);
  return ok({ application_id: app.application_id, kyb_status: app.kyb_status });
}

export function uploadKybDocumentMock(
  applicationId: string,
  docType: string,
  pointer: string,
  sha256: string,
): MockResult<OnboardingApplication> {
  const app = findApplication(applicationId);
  if (!app) return mockError("not_found", "application_not_found", "No such onboarding application.");
  const doc = app.documents.find((d) => d.doc_type === docType);
  if (!doc) return mockError("invalid_request", "unknown_doc_type", "This document type is not on the checklist.");
  if (pointer.trim() === "" || !/^[0-9a-f]{64}$/.test(sha256)) {
    return mockError("invalid_request", "bad_evidence", "pointer and a 64-hex sha256 are required.");
  }
  const at = mutationTs();
  doc.status = "UPLOADED";
  doc.pointer = pointer;
  doc.sha256 = sha256;
  doc.uploaded_at = at;
  if (app.kyb_status === "DOCUMENTS_PENDING" && app.documents.every((d) => d.status !== "REQUIRED" && d.status !== "REJECTED")) {
    app.kyb_status = "KYB_IN_PROGRESS";
    app.state_history.push({ state: "KYB_IN_PROGRESS", at });
  }
  app.updated_at = at;
  return ok(app);
}

// ---------------------------------------------------------------------------
// Payment intents + merchant dashboard
// ---------------------------------------------------------------------------

const INTENT_SEED: { env: Env; amount: string; method: PaymentIntentSummary["method"]; status: PaymentIntentSummary["status"]; offset: number }[] = [
  { env: "live", amount: "245000", method: "BKASH", status: "SUCCEEDED", offset: -22 },
  { env: "live", amount: "129900", method: "NAGAD", status: "SUCCEEDED", offset: -58 },
  { env: "live", amount: "560013", method: "CARD", status: "FAILED", offset: -95 },
  { env: "live", amount: "78500", method: "BANGLA_QR", status: "SUCCEEDED", offset: -140 },
  { env: "live", amount: "1500000", method: "NPSB_IBFT", status: "PROCESSING", offset: -180 },
  { env: "live", amount: "33000", method: "BKASH", status: "SUCCEEDED", offset: -260 },
  { env: "live", amount: "412500", method: "CARD", status: "REQUIRES_CAPTURE", offset: -300 },
  { env: "live", amount: "97000", method: "NAGAD", status: "PARTIALLY_REFUNDED", offset: -60 * 26 },
  { env: "live", amount: "215000", method: "BKASH", status: "SUCCEEDED", offset: -60 * 30 },
  { env: "live", amount: "182000", method: "BANGLA_QR", status: "CANCELLED", offset: -60 * 45 },
  { env: "sandbox", amount: "10100", method: "BKASH", status: "SUCCEEDED", offset: -15 },
  { env: "sandbox", amount: "10200", method: "NAGAD", status: "FAILED", offset: -40 },
  { env: "sandbox", amount: "990013", method: "CARD", status: "FAILED", offset: -75 },
  { env: "sandbox", amount: "50000", method: "BANGLA_QR", status: "SUCCEEDED", offset: -120 },
  { env: "sandbox", amount: "10300", method: "BKASH", status: "SUCCEEDED", offset: -200 },
  { env: "sandbox", amount: "75000", method: "NPSB_IBFT", status: "REQUIRES_CONFIRMATION", offset: -240 },
];

const intents: PaymentIntentSummary[] = INTENT_SEED.map((s, i) => ({
  payment_intent_id: id("pi", `intent:${i}`),
  env: s.env,
  amount_minor: s.amount,
  currency: "BDT",
  method: s.method,
  status: s.status,
  created_at: tsAt(s.offset),
}));

export function merchantDashboard(env: Env): MerchantDashboard {
  const mine = intents.filter((x) => x.env === env);
  const succeeded = mine.filter((x) => x.status === "SUCCEEDED" || x.status === "PARTIALLY_REFUNDED");
  const terminal = mine.filter((x) => x.status === "SUCCEEDED" || x.status === "PARTIALLY_REFUNDED" || x.status === "FAILED");
  const sum = (rows: PaymentIntentSummary[]): string =>
    rows.reduce((acc, r) => acc + BigInt(r.amount_minor), 0n).toString();
  const todayCut = EPOCH_MS - 9 * 3_600_000; // mock day starts 00:00 UTC of SNAPSHOT_AT
  const d7Cut = EPOCH_MS - 7 * 24 * 3_600_000;
  const inWindow = (cut: number) => succeeded.filter((r) => Date.parse(r.created_at) >= cut);
  const successRateBps = terminal.length === 0 ? 0 : Math.round((succeeded.length / terminal.length) * 10_000);
  return {
    as_of: SNAPSHOT_AT,
    env,
    volume_today_minor: sum(inWindow(todayCut)),
    volume_7d_minor: sum(inWindow(d7Cut)),
    volume_30d_minor: sum(succeeded),
    count_today: mine.filter((r) => Date.parse(r.created_at) >= todayCut).length,
    success_rate_bps: successRateBps,
    settlement: {
      pending_minor: env === "live" ? "1842500" : "70300",
      next_release_at: tsAt(60 * 26),
      last_settled_minor: env === "live" ? "5230000" : "120000",
      last_settled_at: tsAt(-60 * 22),
    },
    recent_intents: mine.slice(0, 10),
  };
}

// ---------------------------------------------------------------------------
// API keys (spec/01 §B)
// ---------------------------------------------------------------------------

interface KeyRecord extends ApiKey {
  // secret values exist only transiently in Created/Rotated responses
}

const apiKeys: KeyRecord[] = [
  {
    key_id: id("key", "live-primary"),
    env: "live",
    key_prefix: "bdpk_live_",
    key_name: "Production checkout",
    scopes: ["payment:write", "payment:read", "refund:write", "refund:read"],
    last_used_at: tsAt(-12),
    created_at: tsAt(-60 * 24 * 90),
    expires_at: null,
    status: "ACTIVE",
  },
  {
    key_id: id("key", "live-reporting"),
    env: "live",
    key_prefix: "bdpk_live_",
    key_name: "Reporting (read-only)",
    scopes: ["payment:read", "refund:read", "settlement:read"],
    last_used_at: tsAt(-60 * 5),
    created_at: tsAt(-60 * 24 * 60),
    expires_at: null,
    status: "ACTIVE",
  },
  {
    key_id: id("key", "live-old"),
    env: "live",
    key_prefix: "bdpk_live_",
    key_name: "Legacy integration",
    scopes: ["payment:write", "payment:read"],
    last_used_at: tsAt(-60 * 24 * 40),
    created_at: tsAt(-60 * 24 * 200),
    expires_at: tsAt(-60 * 24 * 10),
    status: "REVOKED",
  },
  {
    key_id: id("key", "sandbox-dev"),
    env: "sandbox",
    key_prefix: "bdpk_test_",
    key_name: "Local development",
    scopes: ["payment:write", "payment:read", "refund:write", "refund:read", "webhook:write", "webhook:read"],
    last_used_at: tsAt(-3),
    created_at: tsAt(-60 * 24 * 30),
    expires_at: null,
    status: "ACTIVE",
  },
  {
    key_id: id("key", "sandbox-ci"),
    env: "sandbox",
    key_prefix: "bdpk_test_",
    key_name: "CI pipeline",
    scopes: ["payment:write", "payment:read"],
    last_used_at: tsAt(-60 * 9),
    created_at: tsAt(-60 * 24 * 20),
    expires_at: null,
    status: "ROTATION_PENDING",
  },
];

let keySeq = 0;

export function listApiKeysMock(env: Env): ApiKey[] {
  return apiKeys.filter((k) => k.env === env);
}

export function createApiKeyMock(env: Env, keyName: string, scopes: string[]): MockResult<ApiKeyCreated> {
  if (keyName.trim() === "") return mockError("invalid_request", "key_name_required", "A key name is required.");
  if (scopes.length === 0) return mockError("invalid_request", "scopes_required", "Select at least one scope.");
  keySeq += 1;
  const at = mutationTs();
  const rec: KeyRecord = {
    key_id: id("key", `created:${keySeq}`),
    env,
    key_prefix: env === "live" ? "bdpk_live_" : "bdpk_test_",
    key_name: keyName,
    scopes,
    last_used_at: null,
    created_at: at,
    expires_at: null,
    status: "ACTIVE",
  };
  apiKeys.unshift(rec);
  const secret = `${rec.key_prefix}${detHex(`secret:${rec.key_id}`, 40)}`;
  return ok({ ...rec, secret });
}

export function rotateApiKeyMock(keyId: string): MockResult<ApiKeyRotated> {
  const rec = apiKeys.find((k) => k.key_id === keyId);
  if (!rec) return mockError("not_found", "key_not_found", "No such API key.");
  if (rec.status !== "ACTIVE") {
    return mockError("conflict", "key_not_rotatable", `Only ACTIVE keys can be rotated (current: ${rec.status}).`);
  }
  const at = mutationTs();
  rec.status = "ROTATION_PENDING";
  const graceEnd = new Date(Date.parse(at) + 24 * 3_600_000).toISOString().replace(".000Z", "Z");
  keySeq += 1;
  return ok({
    key_id: rec.key_id,
    new_secret: `${rec.key_prefix}${detHex(`rotated:${rec.key_id}:${keySeq}`, 40)}`,
    old_secret_expires_at: graceEnd,
  });
}

export function revokeApiKeyMock(keyId: string): MockResult<ApiKey> {
  const rec = apiKeys.find((k) => k.key_id === keyId);
  if (!rec) return mockError("not_found", "key_not_found", "No such API key.");
  if (rec.status === "REVOKED") {
    return mockError("conflict", "key_already_revoked", "This key is already revoked.");
  }
  rec.status = "REVOKED";
  rec.expires_at = mutationTs();
  return ok(rec);
}

// ---------------------------------------------------------------------------
// Webhook endpoints + deliveries (spec/01 §I)
// ---------------------------------------------------------------------------

const MAX_ENDPOINTS = 10;

const webhookEndpoints: WebhookEndpoint[] = [
  {
    webhook_id: id("wh", "live-main"),
    env: "live",
    url: "https://api.dhakamart.example/bdpay/webhooks",
    enabled_events: ["payment_intent.succeeded", "payment_intent.failed", "refund.succeeded", "payout.batch_released"],
    description: "Production order fulfilment",
    status: "ACTIVE",
    created_at: tsAt(-60 * 24 * 85),
  },
  {
    webhook_id: id("wh", "sandbox-dev"),
    env: "sandbox",
    url: "https://staging.dhakamart.example/bdpay/webhooks",
    enabled_events: ["payment_intent.succeeded", "payment_intent.failed"],
    description: "Staging integration tests",
    status: "ACTIVE",
    created_at: tsAt(-60 * 24 * 28),
  },
];

function signatureHeader(key: string, atIso: string): string {
  const unix = Math.floor(Date.parse(atIso) / 1000);
  return `t=${unix},v1=${detHex(`sig:${key}:${unix}`, 64)}`;
}

const webhookDeliveries: WebhookDelivery[] = [
  {
    delivery_id: id("whd", "d1"),
    webhook_id: id("wh", "live-main"),
    env: "live",
    event_type: "payment_intent.succeeded",
    status: "DELIVERED",
    attempt: 1,
    response_code: 200,
    signature_header: signatureHeader("d1", tsAt(-21)),
    payload_sha256: sha("whd:d1"),
    delivered_at: tsAt(-21),
  },
  {
    delivery_id: id("whd", "d2"),
    webhook_id: id("wh", "live-main"),
    env: "live",
    event_type: "payment_intent.failed",
    status: "DELIVERED",
    attempt: 2,
    response_code: 200,
    signature_header: signatureHeader("d2", tsAt(-90)),
    payload_sha256: sha("whd:d2"),
    delivered_at: tsAt(-90),
  },
  {
    delivery_id: id("whd", "d3"),
    webhook_id: id("wh", "live-main"),
    env: "live",
    event_type: "refund.succeeded",
    status: "FAILED",
    attempt: 3,
    response_code: 503,
    signature_header: signatureHeader("d3", tsAt(-130)),
    payload_sha256: sha("whd:d3"),
    delivered_at: tsAt(-130),
  },
  {
    delivery_id: id("whd", "d4"),
    webhook_id: id("wh", "sandbox-dev"),
    env: "sandbox",
    event_type: "payment_intent.succeeded",
    status: "DELIVERED",
    attempt: 1,
    response_code: 200,
    signature_header: signatureHeader("d4", tsAt(-14)),
    payload_sha256: sha("whd:d4"),
    delivered_at: tsAt(-14),
  },
  {
    delivery_id: id("whd", "d5"),
    webhook_id: id("wh", "sandbox-dev"),
    env: "sandbox",
    event_type: "payment_intent.failed",
    status: "EXHAUSTED",
    attempt: 8,
    response_code: 500,
    signature_header: signatureHeader("d5", tsAt(-60 * 20)),
    payload_sha256: sha("whd:d5"),
    delivered_at: tsAt(-60 * 20),
  },
];

let whSeq = 0;

export function listWebhookEndpointsMock(env: Env): WebhookEndpoint[] {
  return webhookEndpoints.filter((w) => w.env === env && w.status !== "DELETED");
}

export function listWebhookDeliveriesMock(env: Env, webhookId: string): WebhookDelivery[] {
  return webhookDeliveries.filter((d) => d.env === env && (webhookId === "" || d.webhook_id === webhookId));
}

export function createWebhookEndpointMock(
  env: Env,
  url: string,
  enabledEvents: string[],
  description: string,
): MockResult<WebhookEndpointCreated> {
  if (!url.startsWith("https://")) {
    return mockError("invalid_request", "url_must_be_https", "Webhook endpoint URLs must be HTTPS.");
  }
  if (enabledEvents.length === 0) {
    return mockError("invalid_request", "events_required", "Select at least one event type.");
  }
  if (listWebhookEndpointsMock(env).length >= MAX_ENDPOINTS) {
    return mockError("limit_exceeded", "endpoint_limit", `At most ${MAX_ENDPOINTS} endpoints per environment.`);
  }
  whSeq += 1;
  const at = mutationTs();
  const rec: WebhookEndpoint = {
    webhook_id: id("wh", `created:${whSeq}`),
    env,
    url,
    enabled_events: enabledEvents,
    description,
    status: "ACTIVE",
    created_at: at,
  };
  webhookEndpoints.unshift(rec);
  return ok({ ...rec, signing_secret: `whsec_${detHex(`whsec:${rec.webhook_id}`, 48)}` });
}

export function deleteWebhookEndpointMock(webhookId: string): MockResult<WebhookEndpoint> {
  const rec = webhookEndpoints.find((w) => w.webhook_id === webhookId);
  if (!rec || rec.status === "DELETED") return mockError("not_found", "endpoint_not_found", "No such webhook endpoint.");
  rec.status = "DELETED";
  mutationTs();
  return ok(rec);
}

export function testWebhookEndpointMock(webhookId: string): MockResult<WebhookDelivery> {
  const rec = webhookEndpoints.find((w) => w.webhook_id === webhookId);
  if (!rec || rec.status === "DELETED") return mockError("not_found", "endpoint_not_found", "No such webhook endpoint.");
  whSeq += 1;
  const at = mutationTs();
  const delivery: WebhookDelivery = {
    delivery_id: id("whd", `test:${whSeq}`),
    webhook_id: rec.webhook_id,
    env: rec.env,
    event_type: "portal.test_event",
    status: "DELIVERED",
    attempt: 1,
    response_code: 200,
    signature_header: signatureHeader(`test:${whSeq}`, at),
    payload_sha256: sha(`whd:test:${whSeq}`),
    delivered_at: at,
  };
  webhookDeliveries.unshift(delivery);
  return ok(delivery);
}

export function replayWebhookDeliveryMock(deliveryId: string): MockResult<WebhookDelivery> {
  const orig = webhookDeliveries.find((d) => d.delivery_id === deliveryId);
  if (!orig) return mockError("not_found", "delivery_not_found", "No such webhook delivery.");
  whSeq += 1;
  const at = mutationTs();
  const replay: WebhookDelivery = {
    ...orig,
    delivery_id: id("whd", `replay:${whSeq}`),
    status: "DELIVERED",
    attempt: orig.attempt + 1,
    response_code: 200,
    signature_header: signatureHeader(`replay:${whSeq}`, at),
    delivered_at: at,
  };
  webhookDeliveries.unshift(replay);
  return ok(replay);
}

// ---------------------------------------------------------------------------
// Bangla QR (spec/13 — mirrors the REAL engine routes). The mock returns
// ENGINE-shaped responses: deterministic opaque payload strings issued
// "server-side" — the portal never builds EMVCo TLV payloads client-side.
// ---------------------------------------------------------------------------

export const QR_STATIC_LIMIT_MINOR = 2_020_000; // BDT 20,200 cap, integer paisa

function qrRenderUrls(merchantQrId: string): QrRenderUrls {
  const base = `/v1/qr-codes/${merchantQrId}`;
  return { png: `${base}/render.png`, svg: `${base}/render.svg`, kit_pdf: `${base}/kit.pdf` };
}

// Deterministic stand-in for the engine-issued EMVCo TLV string. Shape-only:
// it starts with the payload-format indicator the engine emits ("000201") but
// is otherwise opaque — exactly how the portal must treat real payloads.
function enginePayload(key: string): string {
  return `000201${detHex(`qr-payload:${key}`, 96).toUpperCase()}`;
}

function enginePayloadHash(key: string): string {
  return `sha256:${sha(`qr-payload:${key}`)}`;
}

interface MerchantQrRecord extends MerchantQr {
  payload_key: string; // seed for the deterministic payload / render assets
}

function seedQr(key: string, label: string, storeId: string | null, terminalId: string | null, offset: number): MerchantQrRecord {
  return {
    merchant_qr_id: id("qr", key),
    merchant_id: DEMO_MERCHANT.merchant_id,
    state: "ACTIVE",
    label,
    store_id: storeId,
    terminal_id: terminalId,
    suspend_reason: null,
    created_at: tsAt(offset),
    updated_at: tsAt(offset),
    render_urls: qrRenderUrls(id("qr", key)),
    payload_key: key,
  };
}

const merchantQrs: MerchantQrRecord[] = [
  seedQr("live-gulshan", "Gulshan-1 outlet", "store-gulshan-1", "counter-1", -60 * 24 * 50),
  seedQr("live-dhanmondi", "Dhanmondi outlet", "store-dhanmondi", null, -60 * 24 * 45),
];

let qrSeq = 0;

function toMerchantQr(rec: MerchantQrRecord): MerchantQr {
  const { payload_key: _key, ...view } = rec;
  return view;
}

export function listMerchantQrsMock(merchantId: string): MockResult<{ data: MerchantQr[] }> {
  if (merchantId !== DEMO_MERCHANT.merchant_id) {
    return mockError("authorization", "permission_denied", "Merchant keys may only list their own QR codes.");
  }
  return ok({ data: merchantQrs.map(toMerchantQr) });
}

export function getMerchantQrMock(merchantQrId: string): MockResult<MerchantQr> {
  const rec = merchantQrs.find((q) => q.merchant_qr_id === merchantQrId);
  if (!rec) return mockError("not_found", "qr_not_found", "Merchant QR not found.");
  return ok(toMerchantQr(rec));
}

export function issueStaticQrMock(
  merchantId: string,
  label: string,
  storeId: string | null,
  terminalId: string | null,
): MockResult<StaticQrIssued> {
  if (merchantId !== DEMO_MERCHANT.merchant_id) {
    return mockError("authorization", "permission_denied", "Merchant keys may only issue QR codes for their own merchant.");
  }
  if (label.trim() === "") {
    return mockError("invalid_request", "validation_failed", "label (non-empty string) is required.");
  }
  qrSeq += 1;
  const at = mutationTs();
  const key = `created:${qrSeq}`;
  const rec: MerchantQrRecord = {
    merchant_qr_id: id("qr", key),
    merchant_id: DEMO_MERCHANT.merchant_id,
    state: "ACTIVE",
    label,
    store_id: storeId,
    terminal_id: terminalId,
    suspend_reason: null,
    created_at: at,
    updated_at: at,
    render_urls: qrRenderUrls(id("qr", key)),
    payload_key: key,
  };
  merchantQrs.unshift(rec);
  return {
    status: 201,
    body: {
      merchant_qr_id: rec.merchant_qr_id,
      state: rec.state,
      payload: enginePayload(key),
      payload_hash: enginePayloadHash(key),
      static_limit_minor: QR_STATIC_LIMIT_MINOR,
      render_urls: rec.render_urls,
    },
  };
}

export function qrLifecycleMock(
  merchantQrId: string,
  trigger: "suspend" | "reactivate" | "revoke",
  reason: string | null,
): MockResult<MerchantQr> {
  const rec = merchantQrs.find((q) => q.merchant_qr_id === merchantQrId);
  if (!rec) return mockError("not_found", "qr_not_found", "Merchant QR not found.");
  // Mirrors the engine FSM: ACTIVE -suspend-> SUSPENDED -reactivate-> ACTIVE;
  // revoke from ACTIVE or SUSPENDED; REVOKED is terminal.
  if (rec.state === "REVOKED") {
    return mockError("conflict", "qr_revoked", "This QR is revoked (terminal).");
  }
  if (trigger === "suspend") {
    if (rec.state !== "ACTIVE") return mockError("conflict", "invalid_transition", `Cannot suspend a ${rec.state} QR.`);
    rec.state = "SUSPENDED";
    rec.suspend_reason = reason || "merchant_request";
  } else if (trigger === "reactivate") {
    if (rec.state !== "SUSPENDED") return mockError("conflict", "invalid_transition", `Cannot reactivate a ${rec.state} QR.`);
    rec.state = "ACTIVE";
    rec.suspend_reason = null;
  } else {
    rec.state = "REVOKED";
    rec.suspend_reason = reason || "merchant_request";
  }
  rec.updated_at = mutationTs();
  return ok(toMerchantQr(rec));
}

// Rendered assets — deterministic bytes per (qr, kind), mirroring the engine
// contract: byte-identical across calls, served with Cache-Control and
// X-BDPay-Content-Sha256 headers by the mock route.
const PNG_1PX_B64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==";

export function qrRenderAssetMock(
  merchantQrId: string,
  kind: "png" | "svg" | "kit_pdf",
): { content: Uint8Array; mediaType: string } | null {
  const rec = merchantQrs.find((q) => q.merchant_qr_id === merchantQrId);
  if (!rec) return null;
  if (kind === "png") {
    const raw = atob(PNG_1PX_B64);
    const bytes = new Uint8Array(raw.length);
    for (let i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);
    return { content: bytes, mediaType: "image/png" };
  }
  if (kind === "kit_pdf") {
    const pdf = `%PDF-1.4\n% BD-PAY QR kit (mock) ${enginePayloadHash(rec.payload_key)}\n%%EOF\n`;
    return { content: new TextEncoder().encode(pdf), mediaType: "application/pdf" };
  }
  const svg = [
    `<svg xmlns="http://www.w3.org/2000/svg" width="240" height="240" role="img" aria-label="Bangla QR ${rec.label}">`,
    `<rect width="240" height="240" fill="#ffffff" stroke="#37474f"/>`,
    `<text x="120" y="110" text-anchor="middle" font-family="monospace" font-size="12">Bangla QR (mock render)</text>`,
    `<text x="120" y="135" text-anchor="middle" font-family="monospace" font-size="9">${enginePayloadHash(rec.payload_key).slice(0, 30)}…</text>`,
    `</svg>`,
  ].join("");
  return { content: new TextEncoder().encode(svg), mediaType: "image/svg+xml" };
}

// ---------------------------------------------------------------------------
// Disputes (merchant side)
// ---------------------------------------------------------------------------

const disputes: Dispute[] = [
  {
    dispute_id: id("dspt", "evidence-pending"),
    payment_intent_id: intents[0]?.payment_intent_id ?? id("pi", "intent:0"),
    env: "live",
    state: "EVIDENCE_PENDING",
    amount_minor: "245000",
    currency: "BDT",
    method: "BKASH",
    reason_code: "goods_not_received",
    evidence_deadline_at: tsAt(60 * 24 * 5),
    evidence: [],
    state_history: [
      { state: "OPEN", at: tsAt(-60 * 24 * 2) },
      { state: "EVIDENCE_PENDING", at: tsAt(-60 * 24 * 2 + 30) },
    ],
    opened_at: tsAt(-60 * 24 * 2),
    resolved_at: null,
    updated_at: tsAt(-60 * 24 * 2 + 30),
    schema_version: 1,
  },
  {
    dispute_id: id("dspt", "under-review"),
    payment_intent_id: intents[1]?.payment_intent_id ?? id("pi", "intent:1"),
    env: "live",
    state: "UNDER_REVIEW",
    amount_minor: "129900",
    currency: "BDT",
    method: "NAGAD",
    reason_code: "amount_wrong",
    evidence_deadline_at: null,
    evidence: [
      {
        pointer: `s3://bdpay-evidence/${detHex("ev:1", 12)}/delivery-receipt.pdf`,
        sha256: sha("ev:1"),
        label: "Signed delivery receipt",
        added_by: MEMBERS.owner.member_id,
        added_at: tsAt(-60 * 24 * 4),
      },
    ],
    state_history: [
      { state: "OPEN", at: tsAt(-60 * 24 * 6) },
      { state: "EVIDENCE_PENDING", at: tsAt(-60 * 24 * 6 + 45) },
      { state: "UNDER_REVIEW", at: tsAt(-60 * 24 * 4) },
    ],
    opened_at: tsAt(-60 * 24 * 6),
    resolved_at: null,
    updated_at: tsAt(-60 * 24 * 4),
    schema_version: 1,
  },
  {
    dispute_id: id("dspt", "resolved"),
    payment_intent_id: intents[7]?.payment_intent_id ?? id("pi", "intent:7"),
    env: "live",
    state: "RESOLVED_MERCHANT",
    amount_minor: "97000",
    currency: "BDT",
    method: "NAGAD",
    reason_code: "duplicate",
    evidence_deadline_at: null,
    evidence: [
      {
        pointer: `s3://bdpay-evidence/${detHex("ev:2", 12)}/statement.pdf`,
        sha256: sha("ev:2"),
        label: "Single-charge bank statement",
        added_by: MEMBERS.owner.member_id,
        added_at: tsAt(-60 * 24 * 12),
      },
    ],
    state_history: [
      { state: "OPEN", at: tsAt(-60 * 24 * 15) },
      { state: "EVIDENCE_PENDING", at: tsAt(-60 * 24 * 15 + 60) },
      { state: "UNDER_REVIEW", at: tsAt(-60 * 24 * 12) },
      { state: "RESOLVED_MERCHANT", at: tsAt(-60 * 24 * 9) },
    ],
    opened_at: tsAt(-60 * 24 * 15),
    resolved_at: tsAt(-60 * 24 * 9),
    updated_at: tsAt(-60 * 24 * 9),
    schema_version: 1,
  },
];

export function listDisputesMock(filter: { state: string; env: string }): Dispute[] {
  return disputes.filter(
    (d) => (filter.state === "" || d.state === filter.state) && (filter.env === "" || d.env === filter.env),
  );
}

export function findDispute(disputeId: string): Dispute | undefined {
  return disputes.find((d) => d.dispute_id === disputeId);
}

export function submitDisputeEvidenceMock(
  disputeId: string,
  actorId: string,
  pointer: string,
  sha256: string,
  label: string,
): MockResult<Dispute> {
  const d = findDispute(disputeId);
  if (!d) return mockError("not_found", "dispute_not_found", "No such dispute.");
  if (d.state !== "EVIDENCE_PENDING" && d.state !== "OPEN") {
    return mockError("conflict", "evidence_window_closed", `Evidence cannot be submitted in state ${d.state}.`);
  }
  if (pointer.trim() === "" || !/^[0-9a-f]{64}$/.test(sha256)) {
    return mockError("invalid_request", "bad_evidence", "pointer and a 64-hex sha256 are required.");
  }
  const at = mutationTs();
  d.evidence.push({ pointer, sha256, label: label || "Evidence", added_by: actorId, added_at: at });
  d.state = "UNDER_REVIEW";
  d.state_history.push({ state: "UNDER_REVIEW", at });
  d.updated_at = at;
  return ok(d);
}

// ---------------------------------------------------------------------------
// Bulk disbursement (maker-checker; finance co-sign > BDT 10 lakh)
// ---------------------------------------------------------------------------

export const FINANCE_COSIGN_THRESHOLD_MINOR = "100000000"; // BDT 10,00,000

function seedRows(key: string, amounts: string[]): DisbursementRow[] {
  return amounts.map((a, i) => ({
    row_no: i + 1,
    beneficiary_name: ["Karim Traders", "Mita Stores", "Hossain Brothers", "Lima Enterprise"][i % 4] ?? "Beneficiary",
    account_number: detHex(`acct:${key}:${i}`, 12).replace(/[a-f]/g, (c) => String(c.charCodeAt(0) % 10)),
    routing_number: String(100000000 + (parseInt(detHex(`rt:${key}:${i}`, 6), 16) % 899999999)).slice(0, 9),
    amount_minor: a,
    reference: `ref-${key}-${i + 1}`,
  }));
}

function batchTotal(rows: DisbursementRow[]): string {
  return rows.reduce((acc, r) => acc + BigInt(r.amount_minor), 0n).toString();
}

function seedBatch(
  key: string,
  state: DisbursementBatch["state"],
  rows: DisbursementRow[],
  offset: number,
  checker: MerchantMember | null,
): DisbursementBatch {
  const total = batchTotal(rows);
  const cosign = BigInt(total) > BigInt(FINANCE_COSIGN_THRESHOLD_MINOR);
  return {
    batch_id: id("dbat", key),
    env: "live",
    state,
    maker_member_id: MEMBERS.finance_maker.member_id,
    maker_name: MEMBERS.finance_maker.display_name,
    checker_member_id: checker ? checker.member_id : null,
    checker_name: checker ? checker.display_name : null,
    total_minor: total,
    row_count: rows.length,
    rows,
    finance_cosign_required: cosign,
    approval_request_id: cosign ? id("appr", `cosign:${key}`) : null,
    reject_reason: null,
    created_at: tsAt(offset),
    decided_at: checker ? tsAt(offset + 90) : null,
    released_at: state === "RELEASED" ? tsAt(offset + 180) : null,
    updated_at: tsAt(offset + (checker ? 90 : 0)),
    schema_version: 1,
  };
}

const batches: DisbursementBatch[] = [
  seedBatch("released-1", "RELEASED", seedRows("released-1", ["1250000", "830000", "990000"]), -60 * 24 * 7, MEMBERS.finance_checker),
  seedBatch("pending-1", "PENDING_CHECKER", seedRows("pending-1", ["450000", "725000", "310000", "618000"]), -60 * 5, null),
  seedBatch(
    "cosign-1",
    "PENDING_FINANCE_COSIGN",
    seedRows("cosign-1", ["45000000", "38500000", "29900000"]),
    -60 * 28,
    MEMBERS.finance_checker,
  ),
];

let batchSeq = 0;

export function listBatchesMock(env: Env): DisbursementBatch[] {
  return batches.filter((b) => b.env === env);
}

export function createBatchMock(env: Env, rows: DisbursementRow[], maker: MerchantMember): MockResult<DisbursementBatch> {
  if (rows.length === 0) return mockError("invalid_request", "rows_required", "The batch has no valid rows.");
  for (const r of rows) {
    try {
      if (BigInt(r.amount_minor) <= 0n) {
        return mockError("invalid_request", "amount_not_positive", `Row ${r.row_no}: amount must be positive.`);
      }
    } catch {
      return mockError("invalid_request", "bad_amount", `Row ${r.row_no}: amount_minor must be integer paisa.`);
    }
  }
  batchSeq += 1;
  const at = mutationTs();
  const total = batchTotal(rows);
  const cosign = BigInt(total) > BigInt(FINANCE_COSIGN_THRESHOLD_MINOR);
  const rec: DisbursementBatch = {
    batch_id: id("dbat", `created:${batchSeq}`),
    env,
    state: "PENDING_CHECKER",
    maker_member_id: maker.member_id,
    maker_name: maker.display_name,
    checker_member_id: null,
    checker_name: null,
    total_minor: total,
    row_count: rows.length,
    rows,
    finance_cosign_required: cosign,
    approval_request_id: null,
    reject_reason: null,
    created_at: at,
    decided_at: null,
    released_at: null,
    updated_at: at,
    schema_version: 1,
  };
  batches.unshift(rec);
  return ok(rec);
}

export function approveBatchMock(batchId: string, checker: MerchantMember): MockResult<DisbursementBatch> {
  const b = batches.find((x) => x.batch_id === batchId);
  if (!b) return mockError("not_found", "batch_not_found", "No such disbursement batch.");
  if (b.state !== "PENDING_CHECKER") {
    return mockError("conflict", "batch_not_pending", `Batch is ${b.state}; only PENDING_CHECKER can be approved.`);
  }
  if (checker.member_id === b.maker_member_id) {
    return mockError("authorization", "checker_equals_maker", "Maker-checker violation: the checker must differ from the maker.");
  }
  const at = mutationTs();
  b.checker_member_id = checker.member_id;
  b.checker_name = checker.display_name;
  b.decided_at = at;
  if (BigInt(b.total_minor) > BigInt(FINANCE_COSIGN_THRESHOLD_MINOR)) {
    b.state = "PENDING_FINANCE_COSIGN";
    b.approval_request_id = id("appr", `cosign:${b.batch_id}`);
  } else {
    b.state = "RELEASE_SCHEDULED";
  }
  b.updated_at = at;
  return ok(b);
}

export function rejectBatchMock(batchId: string, checker: MerchantMember, reason: string): MockResult<DisbursementBatch> {
  const b = batches.find((x) => x.batch_id === batchId);
  if (!b) return mockError("not_found", "batch_not_found", "No such disbursement batch.");
  if (b.state !== "PENDING_CHECKER") {
    return mockError("conflict", "batch_not_pending", `Batch is ${b.state}; only PENDING_CHECKER can be rejected.`);
  }
  if (checker.member_id === b.maker_member_id) {
    return mockError("authorization", "checker_equals_maker", "Maker-checker violation: the checker must differ from the maker.");
  }
  if (reason.trim() === "") {
    return mockError("invalid_request", "reason_required", "A rejection reason is required.");
  }
  const at = mutationTs();
  b.state = "REJECTED";
  b.checker_member_id = checker.member_id;
  b.checker_name = checker.display_name;
  b.reject_reason = reason;
  b.decided_at = at;
  b.updated_at = at;
  return ok(b);
}

// ---------------------------------------------------------------------------
// Reports, notifications, settings
// ---------------------------------------------------------------------------

const reports: ReportItem[] = [
  {
    report_id: id("rpt", "settle-0611"),
    kind: "SETTLEMENT_FILE",
    label: "Settlement file 2026-06-11 (T+1)",
    generated_at: tsAt(-60 * 18),
    size_bytes: 48213,
    sha256: sha("rpt:settle-0611"),
  },
  {
    report_id: id("rpt", "recon-0611"),
    kind: "RECON_REPORT",
    label: "Reconciliation report 2026-06-11",
    generated_at: tsAt(-60 * 17),
    size_bytes: 102457,
    sha256: sha("rpt:recon-0611"),
  },
  {
    report_id: id("rpt", "va-0610"),
    kind: "VA_RECON_REPORT",
    label: "Virtual-account recon 2026-06-10",
    generated_at: tsAt(-60 * 41),
    size_bytes: 35720,
    sha256: sha("rpt:va-0610"),
  },
  {
    report_id: id("rpt", "payout-0609"),
    kind: "PAYOUT_RETURN_FILE",
    label: "Payout return file 2026-06-09",
    generated_at: tsAt(-60 * 65),
    size_bytes: 8112,
    sha256: sha("rpt:payout-0609"),
  },
];

export function listReportsMock(env: Env): ReportItem[] {
  // Reports exist for the live environment only; sandbox gets an empty list.
  return env === "live" ? reports : [];
}

export function reportFileBody(report: ReportItem): string {
  // Deterministic CSV body; bytes are identical on every download so the
  // sha256 contract (X-BDPay-Content-Sha256) is demonstrable.
  const lines = [
    `report_id,kind,label,generated_at`,
    `${report.report_id},${report.kind},"${report.label}",${report.generated_at}`,
    `row_ref,amount_minor,currency,state`,
  ];
  for (let i = 1; i <= 5; i++) {
    lines.push(`${report.report_id}-row-${i},${(100000 + i * 12345).toString()},BDT,SETTLED`);
  }
  return lines.join("\n") + "\n";
}

const notifications: PortalNotification[] = [
  {
    notification_id: id("ntf", "n1"),
    kind: "settlement",
    title_en: "Settlement released",
    title_bn: "নিষ্পত্তি ছাড় হয়েছে",
    body_en: "BDT 52,300.00 settled to your account ending 4417 for 2026-06-11.",
    body_bn: "২০২৬-০৬-১১ তারিখের জন্য ৪৪১৭ শেষাঙ্কের অ্যাকাউন্টে ৫২,৩০০.০০ টাকা নিষ্পত্তি হয়েছে।",
    read_at: null,
    created_at: tsAt(-60 * 18),
  },
  {
    notification_id: id("ntf", "n2"),
    kind: "dispute",
    title_en: "Evidence requested on a dispute",
    title_bn: "একটি বিরোধে প্রমাণ চাওয়া হয়েছে",
    body_en: "A dispute on BDT 2,450.00 (bKash) needs evidence within 5 days.",
    body_bn: "২,৪৫০.০০ টাকার (বিকাশ) একটি বিরোধে ৫ দিনের মধ্যে প্রমাণ প্রয়োজন।",
    read_at: null,
    created_at: tsAt(-60 * 24 * 2 + 30),
  },
  {
    notification_id: id("ntf", "n3"),
    kind: "api_key",
    title_en: "API key rotation grace window ending",
    title_bn: "এপিআই কী রোটেশনের গ্রেস সময় শেষ হচ্ছে",
    body_en: "The old secret for key 'CI pipeline' expires in 6 hours.",
    body_bn: "'CI pipeline' কী-এর পুরনো সিক্রেট ৬ ঘণ্টার মধ্যে মেয়াদোত্তীর্ণ হবে।",
    read_at: tsAt(-60 * 3),
    created_at: tsAt(-60 * 9),
  },
  {
    notification_id: id("ntf", "n4"),
    kind: "disbursement",
    title_en: "Disbursement batch awaiting checker",
    title_bn: "বিতরণ ব্যাচ চেকারের অপেক্ষায়",
    body_en: "A batch of 4 rows totalling BDT 21,030.00 awaits checker approval.",
    body_bn: "মোট ২১,০৩০.০০ টাকার ৪ সারির একটি ব্যাচ চেকারের অনুমোদনের অপেক্ষায়।",
    read_at: null,
    created_at: tsAt(-60 * 5),
  },
];

export function listNotificationsMock(): PortalNotification[] {
  return notifications;
}

export function getSettingsMock(): MerchantSettings {
  return {
    merchant: DEMO_MERCHANT,
    virtual_accounts: [
      {
        va_id: id("va", "gulshan"),
        store_label: "Gulshan-1 outlet",
        va_number: `2050${detHex("va:gulshan", 9).replace(/[a-f]/g, (c) => String(c.charCodeAt(0) % 10))}`,
        state: "ACTIVE",
        bank_name: "Sonali Bank (sponsor)",
        created_at: tsAt(-60 * 24 * 60),
      },
      {
        va_id: id("va", "dhanmondi"),
        store_label: "Dhanmondi outlet",
        va_number: null,
        state: "PENDING_SPONSOR_RANGE",
        bank_name: "Sonali Bank (sponsor)",
        created_at: tsAt(-60 * 24 * 3),
      },
    ],
    totp: {
      enrolled: true,
      otpauth_uri: "otpauth://totp/BD-PAY:rahim.uddin%40dhakamart.example?secret=MASKED&issuer=BD-PAY",
      secret_masked: "ABCD-••••-••••-WXYZ",
      enrolled_at: tsAt(-60 * 24 * 219),
    },
    members: [MEMBERS.owner, MEMBERS.developer, MEMBERS.finance_maker, MEMBERS.finance_checker],
  };
}

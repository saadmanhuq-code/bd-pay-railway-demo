// Deterministic mock state for the spec/16 launch-readiness surfaces
// (workstream 4): public status + certification matrix + badges (§E),
// payment links + hosted checkout (§C), sandbox signup (§F).
// Same rules as store.ts: no Math.random / Date.now in the data path.

import { createHmac } from "node:crypto";
import type { Env } from "@/lib/api/types";
import type {
  CertificationEntry,
  CertificationMatrix,
  CheckoutMethod,
  ConnectorStatusEntry,
  PaymentLink,
  PublicLinkIntentResult,
  PublicPaymentLinkView,
  PublicStatusFeed,
  SandboxSignupCreated,
  SandboxSignupState,
  SandboxSignupVerified,
} from "@/lib/api/launchTypes";
import { CERT_CHECK_IDS } from "@/lib/api/launchTypes";
import { DEMO_MERCHANT, SNAPSHOT_AT, detHex, mockError, type MockResult } from "./store";

// ---------------------------------------------------------------------------
// Deterministic helpers (mirrors store.ts; those helpers are module-private)
// ---------------------------------------------------------------------------

const EPOCH_MS = Date.parse(SNAPSHOT_AT);

function tsAt(offsetMinutes: number): string {
  return new Date(EPOCH_MS + offsetMinutes * 60_000).toISOString().replace(".000Z", "Z");
}

let launchSeq = 0;
function mutationTs(): string {
  launchSeq += 1;
  return new Date(EPOCH_MS + 45 * 60_000 + launchSeq * 1000).toISOString().replace(".000Z", "Z");
}

function id(prefix: string, key: string): string {
  return `${prefix}_${detHex(`launch:${key}`)}`;
}

function ok<T>(body: T, status = 200): MockResult<T> {
  return { status, body };
}

// ---------------------------------------------------------------------------
// Public status feed — GET /v1/public/status (spec/16 §E; cache 60s)
// ---------------------------------------------------------------------------

const STATUS_FEED: ConnectorStatusEntry[] = [
  {
    connector_id: "npsb_iso8583_v28",
    display_name: "NPSB (ISO 8583)",
    active_mode: "SIMULATOR",
    health_status: "HEALTHY",
    circuit_state: "CLOSED",
    uptime_24h_bps: 10000,
    uptime_7d_bps: 9985,
    last_health_at: tsAt(-1),
  },
  {
    connector_id: "banglaqr_npsb_v1",
    display_name: "Bangla QR (NPSB)",
    active_mode: "SIMULATOR",
    health_status: "HEALTHY",
    circuit_state: "CLOSED",
    uptime_24h_bps: 10000,
    uptime_7d_bps: 9998,
    last_health_at: tsAt(-1),
  },
  {
    connector_id: "bkash_pgw_v2",
    display_name: "bKash PGW",
    active_mode: "SIMULATOR",
    health_status: "HEALTHY",
    circuit_state: "CLOSED",
    uptime_24h_bps: 9995,
    uptime_7d_bps: 9991,
    last_health_at: tsAt(-2),
  },
  {
    connector_id: "nagad_pgw_v1",
    display_name: "Nagad PGW",
    active_mode: "SIMULATOR",
    health_status: "DEGRADED",
    circuit_state: "HALF_OPEN",
    uptime_24h_bps: 9760,
    uptime_7d_bps: 9942,
    last_health_at: tsAt(-1),
  },
  {
    connector_id: "email_smtp_v1",
    display_name: "Email (SMTP)",
    active_mode: "SANDBOX",
    health_status: "HEALTHY",
    circuit_state: "CLOSED",
    uptime_24h_bps: 10000,
    uptime_7d_bps: 10000,
    last_health_at: tsAt(-3),
  },
];

export function publicStatusMock(): PublicStatusFeed {
  return { as_of: SNAPSHOT_AT, data: STATUS_FEED };
}

// ---------------------------------------------------------------------------
// Certification matrix — GET /v1/public/certification-matrix (spec/16 §E)
// ---------------------------------------------------------------------------

function passedChecks(): CertificationEntry["checks"] {
  const checks: CertificationEntry["checks"] = {};
  for (const c of CERT_CHECK_IDS) checks[c] = "PASSED";
  return checks;
}

const CERT_MATRIX: CertificationEntry[] = [
  {
    connector_id: "npsb_iso8583_v28",
    display_name: "NPSB (ISO 8583)",
    certification_status: "CERTIFIED",
    last_certified_at: tsAt(-60 * 24 * 9),
    adapter_version: "2.8.4",
    checks: passedChecks(),
    report_hash: detHex("cert:npsb_iso8583_v28", 64),
  },
  {
    connector_id: "banglaqr_npsb_v1",
    display_name: "Bangla QR (NPSB)",
    certification_status: "CERTIFIED",
    last_certified_at: tsAt(-60 * 24 * 6),
    adapter_version: "1.3.0",
    checks: passedChecks(),
    report_hash: detHex("cert:banglaqr_npsb_v1", 64),
  },
  {
    connector_id: "bkash_pgw_v2",
    display_name: "bKash PGW",
    certification_status: "CERTIFIED",
    last_certified_at: tsAt(-60 * 24 * 11),
    adapter_version: "2.1.7",
    checks: passedChecks(),
    report_hash: detHex("cert:bkash_pgw_v2", 64),
  },
  {
    connector_id: "nagad_pgw_v1",
    display_name: "Nagad PGW",
    certification_status: "PENDING",
    last_certified_at: null,
    adapter_version: "1.0.2",
    checks: {},
    report_hash: null,
  },
];

export function certificationMatrixMock(): CertificationMatrix {
  return { as_of: SNAPSHOT_AT, data: CERT_MATRIX };
}

// Badge — GET /v1/public/badges/{connector_id}.svg. Deterministic SVG;
// 404 (null) for unknown connector ids.
export function badgeSvgMock(connectorId: string): string | null {
  const entry = CERT_MATRIX.find((c) => c.connector_id === connectorId);
  if (!entry) return null;
  const status = entry.certification_status;
  const color = status === "CERTIFIED" ? "#2e7d32" : status === "PENDING" ? "#757575" : "#c62828";
  const dateLabel = entry.last_certified_at ? entry.last_certified_at.slice(0, 10) : "—";
  const right = `${status} · ${dateLabel}`;
  return [
    `<svg xmlns="http://www.w3.org/2000/svg" width="280" height="20" role="img" aria-label="BD-PAY cert: ${status}">`,
    `<rect width="92" height="20" fill="#37474f"/>`,
    `<rect x="92" width="188" height="20" fill="${color}"/>`,
    `<g fill="#ffffff" font-family="Verdana,Geneva,sans-serif" font-size="11">`,
    `<text x="8" y="14">BD-PAY cert</text>`,
    `<text x="100" y="14">${right}</text>`,
    `</g>`,
    `</svg>`,
  ].join("");
}

// ---------------------------------------------------------------------------
// Payment links (spec/16 §C)
// ---------------------------------------------------------------------------

function publicCode(key: string): string {
  // Real backend: base32(sha256(link_id + merchant_id + created_at))[:16],
  // lowercase. The mock derives a deterministic 16-char lowercase code.
  return detHex(`plinkcode:${key}`, 16);
}

function seedLink(args: {
  key: string;
  env: Env;
  description: string;
  amountMinor: string | null;
  amountMinMinor?: string | null;
  amountMaxMinor?: string | null;
  singleUse: boolean;
  state: PaymentLink["state"];
  paid?: boolean;
  expiresOffsetMin: number | null;
  createdOffsetMin: number;
}): PaymentLink {
  const code = publicCode(args.key);
  return {
    payment_link_id: id("plink", args.key),
    env: args.env,
    public_code: code,
    url: `/pay/l/${code}`,
    description: args.description,
    currency: "BDT",
    amount_minor: args.amountMinor,
    amount_min_minor: args.amountMinMinor ?? null,
    amount_max_minor: args.amountMaxMinor ?? null,
    single_use: args.singleUse,
    state: args.state,
    payment_intent_id: args.paid ? id("pi", `paid:${args.key}`) : null,
    expires_at: args.expiresOffsetMin === null ? null : tsAt(args.expiresOffsetMin),
    created_at: tsAt(args.createdOffsetMin),
  };
}

const paymentLinks: PaymentLink[] = [
  seedLink({
    key: "sbx-order-1042",
    env: "sandbox",
    description: "Order 1042",
    amountMinor: "250000",
    singleUse: true,
    state: "ACTIVE",
    expiresOffsetMin: 60 * 70,
    createdOffsetMin: -60 * 2,
  }),
  seedLink({
    key: "sbx-topup",
    env: "sandbox",
    description: "Account top-up",
    amountMinor: null,
    amountMinMinor: "10000",
    amountMaxMinor: "500000",
    singleUse: false,
    state: "ACTIVE",
    expiresOffsetMin: 60 * 48,
    createdOffsetMin: -60 * 5,
  }),
  seedLink({
    key: "sbx-order-1031",
    env: "sandbox",
    description: "Order 1031",
    amountMinor: "129900",
    singleUse: true,
    state: "PAID",
    paid: true,
    expiresOffsetMin: 60 * 12,
    createdOffsetMin: -60 * 30,
  }),
  seedLink({
    key: "sbx-order-0993",
    env: "sandbox",
    description: "Order 0993",
    amountMinor: "84500",
    singleUse: true,
    state: "EXPIRED",
    expiresOffsetMin: -60 * 6,
    createdOffsetMin: -60 * 90,
  }),
  seedLink({
    key: "sbx-order-0871",
    env: "sandbox",
    description: "Order 0871",
    amountMinor: "45000",
    singleUse: true,
    state: "CANCELLED",
    expiresOffsetMin: 60 * 2,
    createdOffsetMin: -60 * 50,
  }),
  seedLink({
    key: "live-invoice-220",
    env: "live",
    description: "Invoice 220",
    amountMinor: "1250000",
    singleUse: true,
    state: "ACTIVE",
    expiresOffsetMin: 60 * 70,
    createdOffsetMin: -60 * 4,
  }),
];

let linkSeq = 0;
// single_use enforcement: at most one non-terminal intent per link.
const openIntentByLink = new Map<string, string>();

export function listPaymentLinksMock(env: Env): PaymentLink[] {
  return paymentLinks.filter((l) => l.env === env);
}

export function findPaymentLinkMock(plinkId: string): PaymentLink | undefined {
  return paymentLinks.find((l) => l.payment_link_id === plinkId);
}

// spec/00 PII redaction screen at intake — REJECT, never redact.
function descriptionHasPii(description: string): boolean {
  const bnNormalized = description.replace(/[০-৯]/g, (d) => String("০১২৩৪৫৬৭৮৯".indexOf(d)));
  if (/@/.test(bnNormalized)) return true; // email-shaped
  if (/(?:\+?88)?01\d{9}/.test(bnNormalized.replace(/[\s-]/g, ""))) return true; // BD MSISDN
  if (/\d{10,17}/.test(bnNormalized.replace(/[\s-]/g, ""))) return true; // NID / account-number-shaped
  return false;
}

export function createPaymentLinkMock(args: {
  env: Env;
  description: string;
  amountMinor: string | null;
  amountMinMinor: string | null;
  amountMaxMinor: string | null;
  singleUse: boolean;
  expiryHours: number | null;
}): MockResult<PaymentLink> {
  if (args.description.trim() === "") {
    return mockError("invalid_request", "description_required", "A description is required.");
  }
  if (descriptionHasPii(args.description)) {
    return mockError(
      "invalid_request",
      "description_contains_pii",
      "The description appears to contain personal data and was rejected (not redacted).",
    );
  }
  const parse = (v: string | null): bigint | null => {
    if (v === null) return null;
    try {
      return BigInt(v);
    } catch {
      return null;
    }
  };
  const fixed = parse(args.amountMinor);
  const min = parse(args.amountMinMinor);
  const max = parse(args.amountMaxMinor);
  if (args.amountMinor !== null) {
    if (fixed === null || fixed <= 0n) {
      return mockError("invalid_request", "bad_amount", "amount_minor must be positive integer paisa.");
    }
  } else {
    if (min === null || max === null || min <= 0n || max <= min) {
      return mockError(
        "invalid_request",
        "bad_amount_bounds",
        "amount_min_minor and amount_max_minor must be positive integer paisa with min < max.",
      );
    }
  }
  const hours = args.expiryHours === null ? 72 : args.expiryHours; // PAYMENT_LINK_DEFAULT_TTL_HOURS default 72
  if (!Number.isInteger(hours) || hours <= 0 || hours > 24 * 90) {
    return mockError("invalid_request", "bad_expiry", "expires_at must be between 1 hour and 90 days from now.");
  }
  linkSeq += 1;
  const at = mutationTs();
  const code = publicCode(`created:${linkSeq}`);
  const rec: PaymentLink = {
    payment_link_id: id("plink", `created:${linkSeq}`),
    env: args.env,
    public_code: code,
    url: `/pay/l/${code}`,
    description: args.description,
    currency: "BDT",
    amount_minor: args.amountMinor,
    amount_min_minor: args.amountMinor === null ? args.amountMinMinor : null,
    amount_max_minor: args.amountMinor === null ? args.amountMaxMinor : null,
    single_use: args.singleUse,
    state: "ACTIVE",
    payment_intent_id: null,
    expires_at: new Date(Date.parse(at) + hours * 3_600_000).toISOString().replace(".000Z", "Z"),
    created_at: at,
  };
  paymentLinks.unshift(rec);
  return { status: 201, body: rec };
}

export function cancelPaymentLinkMock(plinkId: string): MockResult<PaymentLink> {
  const rec = findPaymentLinkMock(plinkId);
  if (!rec) return mockError("not_found", "payment_link_not_found", "No such payment link.");
  if (rec.state !== "ACTIVE") {
    return mockError("conflict", "link_not_cancellable", `Only ACTIVE links can be cancelled (state: ${rec.state}).`);
  }
  rec.state = "CANCELLED";
  mutationTs();
  return ok(rec);
}

// ---------------------------------------------------------------------------
// Public link view + intent creation (hosted checkout, spec/16 §C)
// ---------------------------------------------------------------------------

export function publicLinkViewMock(code: string): PublicPaymentLinkView | null {
  const rec = paymentLinks.find((l) => l.public_code === code);
  if (!rec) return null;
  return {
    public_code: rec.public_code,
    state: rec.state,
    merchant_display_name: DEMO_MERCHANT.trade_name,
    merchant_display_name_bn: DEMO_MERCHANT.trade_name_bn,
    description: rec.description,
    currency: "BDT",
    amount_minor: rec.amount_minor,
    amount_min_minor: rec.amount_min_minor,
    amount_max_minor: rec.amount_max_minor,
    expires_at: rec.expires_at,
  };
}

export function createPublicIntentMock(
  code: string,
  amountMinorRaw: string | null,
  method: CheckoutMethod,
): MockResult<PublicLinkIntentResult> {
  const rec = paymentLinks.find((l) => l.public_code === code);
  if (!rec) return mockError("not_found", "payment_link_not_found", "No such payment link.");
  if (rec.state !== "ACTIVE") {
    return mockError("conflict", "link_not_payable", `This link is ${rec.state} and cannot accept a payment.`);
  }
  let amount: bigint;
  if (rec.amount_minor !== null) {
    amount = BigInt(rec.amount_minor);
  } else {
    if (amountMinorRaw === null) {
      return mockError("invalid_request", "amount_required", "An amount within the link bounds is required.");
    }
    try {
      amount = BigInt(amountMinorRaw);
    } catch {
      return mockError("invalid_request", "bad_amount", "amount_minor must be integer paisa.");
    }
    const min = BigInt(rec.amount_min_minor ?? "0");
    const max = BigInt(rec.amount_max_minor ?? "0");
    if (amount < min || amount > max) {
      return mockError("invalid_request", "amount_out_of_bounds", "Amount is outside the link's allowed range.");
    }
  }
  if (rec.single_use && openIntentByLink.has(rec.payment_link_id)) {
    return mockError("conflict", "link_not_payable", "A payment is already in progress for this single-use link.");
  }
  linkSeq += 1;
  const at = mutationTs();
  const intentId = id("pi", `pub:${rec.payment_link_id}:${linkSeq}`);
  if (rec.single_use) openIntentByLink.set(rec.payment_link_id, intentId);

  // Mirrors the real engine: the intent envelope carries an ENGINE-issued
  // dynamic Bangla QR (spec/13) bound to the intent — ttl 300s — regardless
  // of the chosen method; CARD/MFS additionally get a redirect next_action.
  // The mock payload is shape-only and opaque, like the real TLV string.
  const result: PublicLinkIntentResult = {
    payment_intent_id: intentId,
    status: "REQUIRES_ACTION",
    amount_minor: Number(amount),
    currency: "BDT",
    qr_payload: `000201${detHex(`qr-payload:plink:${rec.payment_link_id}:${linkSeq}`, 96).toUpperCase()}`,
    qr_payload_hash: `sha256:${detHex(`qr-hash:plink:${rec.payment_link_id}:${linkSeq}`, 64)}`,
    qr_expires_at: new Date(Date.parse(at) + 300_000).toISOString().replace(".000Z", "Z"),
  };
  if (method !== "BANGLA_QR") {
    result.next_action = {
      type: "redirect_to_url",
      redirect_to_url: `https://checkout.simulator.bdpay.example/redirect/${intentId}`,
    };
  }
  return ok(result, 201);
}

// ---------------------------------------------------------------------------
// Sandbox signup (spec/16 §F) — signup-specific mock email OTP; "999999"
// demos expiry; any other bad code decrements attempts, 5 failures ⇒ REVOKED.
// ---------------------------------------------------------------------------

export const SANDBOX_BASE_URL = "https://sandbox.api.bdpay.example";
export const SANDBOX_DOCS_URL = "/docs";
const DEV_SANDBOX_OTP_SECRET = "bdpay-developer-portal-dev-sandbox-otp-secret-v1";
const SANDBOX_OTP_MAX_ATTEMPTS = 5;

interface SandboxSignup {
  sandbox_signup_id: string;
  email: string;
  display_name: string;
  state: SandboxSignupState;
  otp_attempts_remaining: number;
  otp_digest: string;
}

const sandboxSignups = new Map<string, SandboxSignup>();
let signupSeq = 0;

function sandboxOtpSecret(): string | null {
  const configured = process.env.BDPAY_MOCK_SANDBOX_OTP_SECRET?.trim();
  if (configured) return configured;
  if (process.env.NODE_ENV === "production") return null;
  return DEV_SANDBOX_OTP_SECRET;
}

function sandboxOtpCode(secret: string, sbxsId: string, email: string): string {
  const digest = createHmac("sha256", secret).update(`sandbox-otp-code|${sbxsId}|${email}`).digest();
  const code = String((digest.readUInt32BE(0) & 0x7fffffff) % 1_000_000).padStart(6, "0");
  return code === "999999" ? "999998" : code;
}

function sandboxOtpDigest(secret: string, sbxsId: string, email: string, otp: string): string {
  return createHmac("sha256", secret).update(`sandbox-otp-digest|${sbxsId}|${email}|${otp}`).digest("hex");
}

function constantTimeEqual(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i += 1) {
    diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  }
  return diff === 0;
}

export function createSandboxSignupMock(email: string, displayName: string): MockResult<SandboxSignupCreated> {
  if (!email.includes("@")) {
    return mockError("invalid_request", "email_invalid", "A valid email is required.");
  }
  if (displayName.trim() === "") {
    return mockError("invalid_request", "display_name_required", "A display name is required.");
  }
  const secret = sandboxOtpSecret();
  if (secret === null) {
    return mockError("internal", "mock_sandbox_otp_secret_missing", "Mock sandbox OTP secret is not configured.");
  }
  signupSeq += 1;
  mutationTs();
  const sandboxSignupId = id("sbxs", `signup:${signupSeq}`);
  const otp = sandboxOtpCode(secret, sandboxSignupId, email);
  const rec: SandboxSignup = {
    sandbox_signup_id: sandboxSignupId,
    email,
    display_name: displayName,
    state: "CREATED",
    otp_attempts_remaining: SANDBOX_OTP_MAX_ATTEMPTS,
    otp_digest: sandboxOtpDigest(secret, sandboxSignupId, email, otp),
  };
  sandboxSignups.set(rec.sandbox_signup_id, rec);
  const body: SandboxSignupCreated = {
    sandbox_signup_id: rec.sandbox_signup_id,
    state: "CREATED",
    otp_attempts_remaining: SANDBOX_OTP_MAX_ATTEMPTS,
  };
  if (process.env.NODE_ENV !== "production") body.debug_otp_code = otp;
  return {
    status: 201,
    body,
  };
}

export function verifySandboxSignupMock(sbxsId: string, otp: string): MockResult<SandboxSignupVerified> {
  const rec = sandboxSignups.get(sbxsId);
  if (!rec) return mockError("not_found", "signup_not_found", "No such sandbox signup.");
  if (rec.state === "REVOKED") {
    return mockError("conflict", "signup_revoked", "This signup was revoked after too many failed attempts.");
  }
  if (rec.state === "EXPIRED") {
    return mockError("conflict", "signup_expired", "This verification code has expired. Start a new signup.");
  }
  if (rec.state === "VERIFIED") {
    return mockError("conflict", "signup_already_verified", "This signup is already verified; the key was shown once.");
  }
  if (otp === "999999") {
    rec.state = "EXPIRED";
    return mockError("conflict", "signup_expired", "This verification code has expired. Start a new signup.");
  }
  const secret = sandboxOtpSecret();
  if (secret === null) {
    return mockError("internal", "mock_sandbox_otp_secret_missing", "Mock sandbox OTP secret is not configured.");
  }
  const submitted = otp.trim();
  const submittedDigest = sandboxOtpDigest(secret, rec.sandbox_signup_id, rec.email, submitted);
  if (!/^\d{6}$/.test(submitted) || !constantTimeEqual(submittedDigest, rec.otp_digest)) {
    rec.otp_attempts_remaining -= 1;
    if (rec.otp_attempts_remaining <= 0) {
      rec.state = "REVOKED";
      return mockError("conflict", "signup_revoked", "Too many failed attempts — this signup is now revoked.");
    }
    return mockError(
      "invalid_request",
      "otp_invalid",
      `Incorrect code. ${rec.otp_attempts_remaining} attempt(s) remaining.`,
    );
  }
  rec.state = "VERIFIED";
  mutationTs();
  return {
    status: 200,
    body: {
      sandbox_signup_id: rec.sandbox_signup_id,
      state: "VERIFIED",
      merchant_id: id("mrch", `sbx:${rec.sandbox_signup_id}`),
      api_key: `bdpk_test_${detHex(`sbxkey:${rec.sandbox_signup_id}`, 32)}`,
      key_prefix: "bdpk_test_",
      sandbox_base_url: SANDBOX_BASE_URL,
      docs_url: SANDBOX_DOCS_URL,
    },
  };
}

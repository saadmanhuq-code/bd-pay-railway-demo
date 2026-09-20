// Typed models for the spec/16 launch-readiness surfaces (workstream 4):
// public connector status + certification matrix (spec/16 §E), payment links
// + hosted checkout (spec/16 §C), and public sandbox signup (spec/16 §F).
// Additive to src/lib/api/types.ts — nothing here redefines an existing model.

import type { Env } from "./types";

// ---------------------------------------------------------------------------
// Public status feed — GET /v1/public/status (spec/16 §E)
// ---------------------------------------------------------------------------

export type ActiveMode = "SIMULATOR" | "SANDBOX" | "PRODUCTION";
export type HealthStatus = "HEALTHY" | "DEGRADED" | "UNHEALTHY";
export type CircuitState = "CLOSED" | "HALF_OPEN" | "OPEN";

export interface ConnectorStatusEntry {
  connector_id: string;
  display_name: string;
  active_mode: ActiveMode; // shown VERBATIM — SIMULATOR/SANDBOX honesty is the feature
  health_status: HealthStatus;
  circuit_state: CircuitState;
  uptime_24h_bps: number; // 10000 = 100.00%
  uptime_7d_bps: number;
  last_health_at: string;
}

export interface PublicStatusFeed {
  as_of: string;
  data: ConnectorStatusEntry[];
}

// ---------------------------------------------------------------------------
// Certification matrix — GET /v1/public/certification-matrix (spec/16 §E)
// ---------------------------------------------------------------------------

export type CertVerdict = "PASSED" | "FAILED";
export type CertificationStatus = "CERTIFIED" | "PENDING" | "EXPIRED";

export const CERT_CHECK_IDS = [
  "CERT-01",
  "CERT-02",
  "CERT-03",
  "CERT-04",
  "CERT-05",
  "CERT-06",
  "CERT-07",
  "CERT-08",
  "CERT-09",
  "CERT-10",
] as const;

export type CertCheckId = (typeof CERT_CHECK_IDS)[number];

export interface CertificationEntry {
  connector_id: string;
  display_name: string;
  certification_status: CertificationStatus;
  last_certified_at: string | null;
  adapter_version: string;
  checks: Partial<Record<CertCheckId, CertVerdict>>; // from the latest PASSED CertificationRun
  report_hash: string | null; // hash is public; the report FILE is not
}

export interface CertificationMatrix {
  as_of: string;
  data: CertificationEntry[];
}

// ---------------------------------------------------------------------------
// Payment links — /v1/payment-links (merchant) + public checkout (spec/16 §C)
// ---------------------------------------------------------------------------

export type PaymentLinkState = "ACTIVE" | "PAID" | "EXPIRED" | "CANCELLED";

export interface PaymentLink {
  payment_link_id: string; // plink_…
  env: Env;
  public_code: string; // base32(sha256(...))[:16] lowercase, content-addressed
  url: string; // hosted checkout URL (/pay/l/{public_code})
  description: string;
  currency: "BDT";
  amount_minor: string | null; // fixed amount; null ⇒ payer-entered within bounds
  amount_min_minor: string | null;
  amount_max_minor: string | null;
  single_use: boolean;
  state: PaymentLinkState;
  payment_intent_id: string | null; // populated once paid
  expires_at: string | null;
  created_at: string;
}

// Public, PII-free view rendered by the hosted checkout page.
export interface PublicPaymentLinkView {
  public_code: string;
  state: PaymentLinkState;
  merchant_display_name: string;
  merchant_display_name_bn: string;
  description: string;
  currency: "BDT";
  amount_minor: string | null;
  amount_min_minor: string | null;
  amount_max_minor: string | null;
  expires_at: string | null;
}

export type CheckoutMethod = "BANGLA_QR" | "CARD" | "MFS";

export type IntentNextAction = { type: "redirect_to_url"; redirect_to_url: string } | { type: "otp" };

// POST /v1/public/payment-links/{public_code}/intents — the standard spec/02
// intent envelope. The engine auto-issues a dynamic Bangla QR bound to the
// intent at creation (spec/16 §C); the qr_* fields are ABSENT when scan-only
// mode refuses issuance, so render their absence gracefully.
export interface PublicLinkIntentResult {
  payment_intent_id: string;
  status: string; // spec/02 intent FSM state (e.g. REQUIRES_ACTION)
  amount_minor: number;
  currency: "BDT";
  qr_payload?: string; // spec/13 dynamic TLV payload from the engine
  qr_payload_hash?: string; // "sha256:<hex>"
  qr_expires_at?: string;
  next_action?: IntentNextAction;
}

// ---------------------------------------------------------------------------
// Sandbox signup — /v1/sandbox/signups (spec/16 §F, SANDBOX_PUBLIC only)
// ---------------------------------------------------------------------------

export type SandboxSignupState = "CREATED" | "VERIFIED" | "REVOKED" | "EXPIRED";

export interface SandboxSignupCreated {
  sandbox_signup_id: string; // sbxs_…
  state: "CREATED";
  otp_attempts_remaining: number;
  debug_otp_code?: string;
}

export interface SandboxSignupVerified {
  sandbox_signup_id: string;
  state: "VERIFIED";
  merchant_id: string;
  api_key: string; // bdpk_test_… — shown ONCE
  key_prefix: "bdpk_test_";
  sandbox_base_url: string;
  docs_url: string;
}

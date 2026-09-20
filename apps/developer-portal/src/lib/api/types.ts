// Typed models for every resource the developer portal renders.
// Sources: spec/15-ops-console-and-portal.md §Developer portal,
// spec/00-conventions.md §4/§6, spec/01-gateway.md (API keys, webhook
// endpoints), spec/08 (KYB FSM), spec/02 (PaymentIntent states),
// spec/13 (Bangla QR), spec/10 (simulator scenario DSL).

// ---------------------------------------------------------------------------
// Conventions §4 — error envelope + cursor pagination
// ---------------------------------------------------------------------------

export type ErrorType =
  | "invalid_request"
  | "authentication"
  | "authorization"
  | "rate_limit"
  | "idempotency_conflict"
  | "limit_exceeded"
  | "sanctions_block"
  | "aml_block"
  | "connector_error"
  | "conflict"
  | "not_found"
  | "internal";

export interface ErrorEnvelope {
  error: {
    type: ErrorType;
    code: string;
    message: string;
    request_id: string;
    doc_url: string;
  };
}

export interface Paginated<T> {
  data: T[];
  next_cursor: string | null;
}

// ---------------------------------------------------------------------------
// Environments & merchant personas (spec/01 API-key-scope mappings,
// surfaced in the portal per spec/15 §Role matrix)
// ---------------------------------------------------------------------------

export type Env = "live" | "sandbox";

export type Persona = "owner" | "developer" | "finance_maker" | "finance_checker";

export const PERSONAS: Persona[] = ["owner", "developer", "finance_maker", "finance_checker"];

export interface MerchantMember {
  member_id: string;
  email: string;
  display_name: string;
  persona: Persona;
}

export type MerchantStatus = "ACTIVE" | "SUSPENDED" | "TERMINATED";

export interface MerchantSummary {
  merchant_id: string;
  legal_name: string;
  trade_name: string;
  trade_name_bn: string;
  status: MerchantStatus;
  mcc: string;
  city: string;
  created_at: string;
}

export interface Session {
  member: MerchantMember;
  merchant: MerchantSummary;
  issued_at: string;
  absolute_expires_at: string;
}

export interface LoginResult {
  member: MerchantMember;
}

// ---------------------------------------------------------------------------
// Onboarding / KYB (spec/08 FSM — server is authoritative on every value)
// ---------------------------------------------------------------------------

export type KybState =
  | "APPLICATION_SUBMITTED"
  | "DOCUMENTS_PENDING"
  | "KYB_IN_PROGRESS"
  | "ENHANCED_DUE_DILIGENCE"
  | "SANCTIONS_REVIEW"
  | "RISK_ASSESSED"
  | "AGREEMENT_PENDING"
  | "ACTIVE"
  | "REJECTED";

export const KYB_TRACKER_STATES: KybState[] = [
  "APPLICATION_SUBMITTED",
  "DOCUMENTS_PENDING",
  "KYB_IN_PROGRESS",
  "SANCTIONS_REVIEW",
  "RISK_ASSESSED",
  "AGREEMENT_PENDING",
  "ACTIVE",
];

export type KybDocumentStatus = "REQUIRED" | "UPLOADED" | "ACCEPTED" | "REJECTED";

export interface KybDocument {
  doc_type: string;
  label_en: string;
  label_bn: string;
  status: KybDocumentStatus;
  pointer: string | null;
  sha256: string | null;
  uploaded_at: string | null;
}

export interface KybStateHistoryItem {
  state: KybState;
  at: string;
}

export interface OnboardingApplication {
  application_id: string;
  merchant_id: string;
  legal_name: string;
  trade_name: string;
  trade_name_bn: string;
  bin_number: string;
  contact_email: string;
  contact_phone: string;
  mcc: string;
  city: string;
  kyb_status: KybState;
  state_history: KybStateHistoryItem[];
  documents: KybDocument[];
  created_at: string;
  updated_at: string;
  schema_version: number;
}

export interface SignupResult {
  application_id: string;
  kyb_status: KybState;
}

// ---------------------------------------------------------------------------
// Payments dashboard (spec/02 intent FSM states)
// ---------------------------------------------------------------------------

export type PaymentIntentState =
  | "CREATED"
  | "REQUIRES_PAYMENT_METHOD"
  | "REQUIRES_CONFIRMATION"
  | "REQUIRES_ACTION"
  | "PROCESSING"
  | "REQUIRES_CAPTURE"
  | "SUCCEEDED"
  | "FAILED"
  | "CANCELLED"
  | "PARTIALLY_REFUNDED";

export type PaymentMethod = "BKASH" | "NAGAD" | "CARD" | "NPSB_IBFT" | "BANGLA_QR";

export interface PaymentIntentSummary {
  payment_intent_id: string;
  env: Env;
  amount_minor: string; // BIGINT-safe: serialized as string
  currency: "BDT";
  method: PaymentMethod;
  status: PaymentIntentState;
  created_at: string;
}

export interface MerchantDashboard {
  as_of: string;
  env: Env;
  volume_today_minor: string;
  volume_7d_minor: string;
  volume_30d_minor: string;
  count_today: number;
  success_rate_bps: number; // 10000 = 100%
  settlement: {
    pending_minor: string;
    next_release_at: string;
    last_settled_minor: string;
    last_settled_at: string;
  };
  recent_intents: PaymentIntentSummary[];
}

// ---------------------------------------------------------------------------
// API keys (spec/01 §B — portal wraps, never re-defines)
// ---------------------------------------------------------------------------

export type ApiKeyStatus = "ACTIVE" | "ROTATION_PENDING" | "REVOKED" | "EXPIRED";

export interface ApiKey {
  key_id: string;
  env: Env;
  key_prefix: "bdpk_live_" | "bdpk_test_";
  key_name: string;
  scopes: string[];
  last_used_at: string | null;
  created_at: string;
  expires_at: string | null;
  status: ApiKeyStatus;
}

export interface ApiKeyCreated extends ApiKey {
  secret: string; // shown ONCE on create; no recovery path — rotate instead
}

export interface ApiKeyRotated {
  key_id: string;
  new_secret: string; // shown ONCE
  // null for in-place immediate rotation (old secret stops working at once);
  // a grace-window timestamp only if the backend reports overlap.
  old_secret_expires_at: string | null;
}

export const API_KEY_SCOPES: string[] = [
  "payment:write",
  "payment:read",
  "refund:write",
  "refund:read",
  "webhook:write",
  "webhook:read",
  "apikey:read",
  "qr:write",
  "qr:read",
  "settlement:read",
];

// ---------------------------------------------------------------------------
// Webhook endpoints + deliveries (spec/01 §I)
// ---------------------------------------------------------------------------

export type WebhookEndpointStatus = "ACTIVE" | "DISABLED" | "DELETED";

export interface WebhookEndpoint {
  webhook_id: string;
  env: Env;
  url: string;
  enabled_events: string[];
  description: string;
  status: WebhookEndpointStatus;
  created_at: string;
}

export interface WebhookEndpointCreated extends WebhookEndpoint {
  signing_secret: string; // whsec_… shown ONCE
}

export type WebhookDeliveryStatus = "PENDING" | "DELIVERED" | "FAILED" | "EXHAUSTED";

export interface WebhookDelivery {
  delivery_id: string;
  webhook_id: string;
  env: Env;
  event_type: string;
  status: WebhookDeliveryStatus;
  attempt: number;
  response_code: number | null;
  signature_header: string; // X-BDPay-Signature: t=<unix>,v1=<hmac-sha256 hex>
  payload_sha256: string;
  delivered_at: string;
}

export const WEBHOOK_EVENT_TYPES: string[] = [
  "payment_intent.succeeded",
  "payment_intent.failed",
  "payment_attempt.charged",
  "refund.succeeded",
  "refund.failed",
  "merchant.activated",
  "merchant.suspended",
  "payout.batch_released",
];

// ---------------------------------------------------------------------------
// Bangla QR (spec/13 — engine routes under /v1/merchants/{id}/qr-codes and
// /v1/qr-codes/{merchant_qr_id}). TLV payloads are ENGINE-issued: the portal
// never synthesizes EMVCo payloads client-side. Rendered assets come from the
// render_urls (binary; Cache-Control + X-BDPay-Content-Sha256 headers).
// ---------------------------------------------------------------------------

export type MerchantQrState = "ACTIVE" | "SUSPENDED" | "REVOKED";

export interface QrRenderUrls {
  png: string; // /v1/qr-codes/{merchant_qr_id}/render.png
  svg: string; // /v1/qr-codes/{merchant_qr_id}/render.svg
  kit_pdf: string; // /v1/qr-codes/{merchant_qr_id}/kit.pdf
}

export interface MerchantQr {
  merchant_qr_id: string;
  merchant_id: string;
  state: MerchantQrState;
  label: string;
  store_id: string | null;
  terminal_id: string | null;
  suspend_reason: string | null;
  created_at: string;
  updated_at: string;
  render_urls: QrRenderUrls;
}

// GET /v1/merchants/{merchant_id}/qr-codes — plain data envelope (no cursor).
export interface MerchantQrList {
  data: MerchantQr[];
}

// POST /v1/merchants/{merchant_id}/qr-codes (201) — the only response that
// carries the raw EMVCo TLV payload string.
export interface StaticQrIssued {
  merchant_qr_id: string;
  state: MerchantQrState;
  payload: string; // full EMVCo TLV string from the engine (incl. CRC, ID 63)
  payload_hash: string; // "sha256:<hex>"
  static_limit_minor: number; // BDT 20,200 cap = 2,020,000 paisa
  render_urls: QrRenderUrls;
}

// ---------------------------------------------------------------------------
// Disputes — merchant side (spec/15 §State machines 3)
// ---------------------------------------------------------------------------

export type DisputeState =
  | "OPEN"
  | "EVIDENCE_PENDING"
  | "UNDER_REVIEW"
  | "RESOLVED_MERCHANT"
  | "RESOLVED_CUSTOMER"
  | "CHARGEBACK_FILED"
  | "CHARGEBACK_CLOSED"
  | "WITHDRAWN";

export type DisputeReasonCode =
  | "goods_not_received"
  | "unauthorized"
  | "duplicate"
  | "amount_wrong"
  | "other";

export interface EvidenceItem {
  pointer: string; // object-store pointer
  sha256: string;
  label: string;
  added_by: string;
  added_at: string;
}

export interface DisputeStateHistoryItem {
  state: DisputeState;
  at: string;
}

export interface Dispute {
  dispute_id: string;
  payment_intent_id: string;
  env: Env;
  state: DisputeState;
  amount_minor: string;
  currency: "BDT";
  method: PaymentMethod;
  reason_code: DisputeReasonCode;
  evidence_deadline_at: string | null;
  evidence: EvidenceItem[];
  state_history: DisputeStateHistoryItem[];
  opened_at: string;
  resolved_at: string | null;
  updated_at: string;
  schema_version: number;
}

// ---------------------------------------------------------------------------
// Bulk disbursement (spec/15 §Developer portal — maker-checker;
// bulk_disbursement_release in the four-eyes catalogue)
// ---------------------------------------------------------------------------

export interface DisbursementRow {
  row_no: number;
  beneficiary_name: string;
  account_number: string;
  routing_number: string;
  amount_minor: string;
  reference: string;
}

export type DisbursementBatchState =
  | "PENDING_CHECKER"
  | "PENDING_FINANCE_COSIGN"
  | "RELEASE_SCHEDULED"
  | "RELEASED"
  | "REJECTED";

export interface DisbursementBatch {
  batch_id: string;
  env: Env;
  state: DisbursementBatchState;
  maker_member_id: string;
  maker_name: string;
  checker_member_id: string | null;
  checker_name: string | null;
  total_minor: string;
  row_count: number;
  rows: DisbursementRow[];
  finance_cosign_required: boolean; // total > BDT 10 lakh (100,000,000 paisa)
  approval_request_id: string | null; // appr_… (spec/15 bulk_disbursement_release)
  reject_reason: string | null;
  created_at: string;
  decided_at: string | null;
  released_at: string | null;
  updated_at: string;
  schema_version: number;
}

// CSV validation report (client-side convenience pass; the server re-runs
// BangladeshNumericNormalization and is authoritative).
export type CsvRowErrorCode =
  | "missing_field"
  | "bad_amount"
  | "amount_not_positive"
  | "bad_account"
  | "bad_routing"
  | "duplicate_reference";

export interface CsvRowReport {
  row_no: number;
  row: DisbursementRow | null;
  errors: CsvRowErrorCode[];
}

// ---------------------------------------------------------------------------
// Reports (deterministic file+hash contract, spec/15 §Dashboards & exports)
// ---------------------------------------------------------------------------

export interface ReportItem {
  report_id: string;
  kind: "SETTLEMENT_FILE" | "RECON_REPORT" | "VA_RECON_REPORT" | "PAYOUT_RETURN_FILE";
  label: string;
  generated_at: string;
  size_bytes: number;
  sha256: string; // matches the X-BDPay-Content-Sha256 header on download
}

// ---------------------------------------------------------------------------
// Notifications (portal_notifications — title/body in both languages)
// ---------------------------------------------------------------------------

export interface PortalNotification {
  notification_id: string;
  kind: string;
  title_en: string;
  title_bn: string;
  body_en: string;
  body_bn: string;
  read_at: string | null;
  created_at: string;
}

// ---------------------------------------------------------------------------
// Settings — virtual accounts + TOTP enrollment
// ---------------------------------------------------------------------------

export type VirtualAccountState = "ACTIVE" | "PENDING_SPONSOR_RANGE";

export interface VirtualAccount {
  va_id: string;
  store_label: string;
  va_number: string | null; // null while PENDING_SPONSOR_RANGE (spec/15 OQ4)
  state: VirtualAccountState;
  bank_name: string;
  created_at: string;
}

export interface TotpEnrollment {
  enrolled: boolean;
  otpauth_uri: string;
  secret_masked: string;
  enrolled_at: string | null;
}

export interface MerchantSettings {
  merchant: MerchantSummary;
  virtual_accounts: VirtualAccount[];
  totp: TotpEnrollment;
  members: MerchantMember[];
}

// ---------------------------------------------------------------------------
// Sandbox live demo flow (gateway GET /v1/sandbox/demo/fullflow)
// ---------------------------------------------------------------------------

export interface SandboxDemoFullflow {
  seed_id: string | null;
  mode: string;
  auth: {
    kind: string;
    principal_id: string;
    merchant_id: string | null;
    operator_id: string | null;
    customer_id: string | null;
  };
  summary: {
    status: string;
    amount_minor: number;
    refunded_minor: number;
    currency: string;
    as_of: string;
  };
  merchant: Record<string, unknown>;
  customer: Record<string, unknown> | null;
  intent: Record<string, unknown> & {
    payment_intent_id: string;
    merchant_id: string;
    customer_id?: string | null;
    amount_minor: number;
    currency: string;
    method?: PaymentMethod;
    status: string;
    created_at?: string;
  };
  attempts: Paginated<Record<string, unknown>>;
  refunds: Paginated<Record<string, unknown>>;
  ledger: {
    available: boolean;
    entries: Record<string, unknown>[];
  };
}

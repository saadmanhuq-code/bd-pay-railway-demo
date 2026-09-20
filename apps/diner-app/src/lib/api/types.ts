// API types for the diner-app surface. Amounts are integer paisa
// (`*_minor: number`, conventions §6); timestamps are RFC3339 UTC strings
// (conventions §7). Error envelope is the binding conventions §4 shape.

import type { Day, OfferWindow } from "@/lib/offers/engine";

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
// Diner session (D1 — authenticated redemption only)
// ---------------------------------------------------------------------------

export interface DinerCustomer {
  customer_id: string;
  display_name: string;
  phone_masked: string;
}

export interface DinerSession {
  customer: DinerCustomer;
  issued_at: string;
  absolute_expires_at: string;
}

export interface OtpChallenge {
  challenge_id: string;
  expires_at: string;
  debug_otp_code?: string;
}

// ---------------------------------------------------------------------------
// Discovery
// ---------------------------------------------------------------------------

export interface DinerMerchant {
  merchant_id: string;
  display_name: string;
  display_name_bn: string;
  area: string;
  area_bn: string;
  cuisine: string;
  cuisine_bn: string;
  live_offer_count: number;
  best_percent_bps: number | null;
  /** EatClub-style time-window chips derived from live offer windows. */
  window_tags: WindowTag[];
}

export type WindowTag = "lunch" | "early_bird" | "happy_hour" | "late";


export type CheckoutMethod = "BANGLA_QR" | "BKASH" | "NAGAD";

// GET /v1/offers/eligible response rows (spec/18 §API surface; PII-free,
// customer-specific checks excluded — per-customer cap enforced only at
// reservation so the endpoint stays cacheable).
export interface EligibleOffer {
  offer_id: string;
  kind: "PERCENT_OFF" | "BOGO";
  title: string;
  title_bn: string;
  percent_bps: number | null;
  windows: OfferWindow[];
  min_spend_minor: number;
  max_discount_minor: number | null;
  allowed_methods: CheckoutMethod[];
  valid_until: string;
  example_amount_minor?: number;
  example_discount_minor?: number;
}

export type { Day, OfferWindow };

// ---------------------------------------------------------------------------
// Reservation -> payment (spec/18 extension to POST /v1/payment-intents)
// ---------------------------------------------------------------------------

export type RedemptionState = "RESERVED" | "APPLIED" | "RELEASED" | "REVERSED" | "SETTLED";

export interface IntentOfferBlock {
  offer_id: string;
  gross_amount_minor: number;
  discount_minor: number;
}

export interface PaymentIntentView {
  payment_intent_id: string;
  merchant_id: string;
  merchant_display_name: string;
  merchant_display_name_bn: string;
  customer_id: string;
  amount_minor: number; // the NET — the only amount the rail ever sees
  currency: "BDT";
  method: CheckoutMethod;
  status: "CREATED" | "SUCCEEDED" | "FAILED" | "EXPIRED" | "PARTIALLY_REFUNDED" | "REFUNDED";
  offer: IntentOfferBlock | null; // read-only block (spec/18 §API surface 3)
  redemption_state: RedemptionState | null;
  created_at: string;
  expires_at: string;
}

export interface DynamicQr {
  payload_id: string;
  payment_intent_id: string;
  payload: string; // spec/13 dynamic TLV payload — never built client-side
  payload_hash: string;
  amount_minor: number;
  expires_at: string;
}

// ---------------------------------------------------------------------------
// Sandbox live demo flow (gateway GET /v1/sandbox/demo/fullflow)
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// Sandbox demo one-tap diner pay
// ---------------------------------------------------------------------------

export interface DinerPayResult {
  payment_intent_id: string;
  status: PaymentIntentView["status"];
  amount_minor: number;
  customer_id: string;
  merchant_id: string;
}

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
    status: PaymentIntentView["status"];
    amount_minor: number;
    refunded_minor: number;
    currency: "BDT";
    as_of: string;
  };
  merchant: Record<string, unknown>;
  customer: Record<string, unknown> | null;
  intent: Record<string, unknown> & {
    payment_intent_id: string;
    merchant_id: string;
    customer_id?: string | null;
    amount_minor: number;
    currency: "BDT";
    method?: string;
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

// Typed models for the spec/18 dining-wedge offer surface (merchant side).
// Source of truth: bdpay/kernel/offers/service.py::offer_view (the REAL
// /v1/offers wire shape) + bdpay/gateway/routes_offers.py (route surface).
// Additive to src/lib/api/types.ts — nothing here redefines an existing model.
//
// Wire-shape notes (pinned by tests/apps/test_developer_portal_offer_wiring.py):
//   - min_spend_minor / max_discount_minor serialize as JSON integers on the
//     real surface (offer_view returns Python ints), so they are `number`
//     here — shape-correctness against production wins over the portal's
//     string-BIGINT convention (errata S18-E18). Display goes through
//     formatBdt, which enforces the integer guard.
//   - commission_bps / subsidy_bps are NOT in the merchant view: they are
//     operator-configured (ApprovalRequest-gated subsidy) and never travel
//     through the merchant surface.
//   - /v1/offers carries NO env query parameter: an offer is keyed to the
//     environment of the merchant API key that created it (errata S18-E19).

// ---------------------------------------------------------------------------
// FSM states (spec/18 §State machines, FSM 1)
// ---------------------------------------------------------------------------

export type OfferState = "DRAFT" | "ACTIVE" | "PAUSED" | "EXHAUSTED" | "EXPIRED" | "ARCHIVED";

export const OFFER_STATES: OfferState[] = ["DRAFT", "ACTIVE", "PAUSED", "EXHAUSTED", "EXPIRED", "ARCHIVED"];

// Terminal states refuse every trigger (refusal-first; spec/18).
export const OFFER_TERMINAL_STATES: OfferState[] = ["EXPIRED", "ARCHIVED"];

export type OfferKind = "PERCENT_OFF" | "BOGO";

// ---------------------------------------------------------------------------
// Binding validation constants (spec/18 §API surface; mirrored from
// bdpay/kernel/offers/models.py — the server re-validates and is authoritative)
// ---------------------------------------------------------------------------

export const PERCENT_BPS_FLOOR = 500; // 5%
export const PERCENT_BPS_CEILING = 5000; // 50% (config offer.max_percent_bps)
export const MAX_WINDOW_ROWS = 7;
export const VALIDITY_MAX_DAYS = 366;

export const WINDOW_DAY_CODES = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"] as const;
export type WindowDay = (typeof WINDOW_DAY_CODES)[number];

// spec/02 payment_method_type enum (bdpay/kernel/payment_states.py) — the
// create form offers this catalogue; the server additionally enforces the
// subset of the merchant's ACTIVE connector methods
// (code: method_not_supported_by_merchant).
export const OFFER_METHODS = ["NPSB_IBFT", "BEFTN_CREDIT", "BKASH", "NAGAD", "ROCKET", "CARD", "BANGLA_QR"] as const;
export type OfferMethod = (typeof OFFER_METHODS)[number];

// Cap-monitoring warning threshold: notification-service raises the merchant
// alert at 80% utilization (spec/18 §Events) — the portal mirrors that line.
export const CAP_WARNING_BPS = 8000; // 80.00%

// ---------------------------------------------------------------------------
// Wire models
// ---------------------------------------------------------------------------

// One time-window row: Asia/Dhaka local HH:MM; start inclusive, end EXCLUSIVE
// (spec/18 §Eligibility boundary rule).
export interface OfferWindow {
  days: WindowDay[];
  start_local: string; // "HH:MM"
  end_local: string; // "HH:MM"
}

export interface Offer {
  offer_id: string; // off_<sha24>
  version: number; // economics version head (edit-by-versioning, S18-E5)
  merchant_id: string;
  kind: OfferKind;
  percent_bps: number | null; // PERCENT_OFF only; [500, 5000]
  bogo_config: Record<string, unknown> | null; // schema-reserved; build-deferred (D2)
  title: string;
  title_bn: string;
  windows: OfferWindow[];
  valid_from: string; // RFC3339 Z
  valid_until: string;
  min_spend_minor: number; // integer paisa (real wire shape — see header note)
  max_discount_minor: number | null;
  cap_per_day: number | null;
  cap_total: number | null;
  cap_per_customer: number | null;
  allowed_methods: OfferMethod[];
  cap_recredit_on_refund: boolean;
  state: OfferState;
  currency: "BDT";
  created_at: string;
  updated_at: string;
}

// Live counter readout, GET /v1/offers/{offer_id} only (spec/18 §API surface:
// "includes live counter readout"). Counts only — never customer identities
// (PDPO posture: merchants see counts, never who redeemed).
export interface OfferCounters {
  redeemed_today: number;
  redeemed_total: number;
  reserved_now: number;
}

export interface OfferDetail extends Offer {
  counters: OfferCounters;
}

// ---------------------------------------------------------------------------
// Request bodies
// ---------------------------------------------------------------------------

// POST /v1/offers — gateway model is extra="forbid": send EXACTLY these
// fields, nothing else (no env, no state).
export interface CreateOfferInput {
  kind: OfferKind;
  percent_bps: number | null;
  title: string;
  title_bn: string;
  windows: OfferWindow[];
  valid_from: string;
  valid_until: string;
  min_spend_minor: number | null;
  max_discount_minor: number | null;
  cap_per_day: number | null;
  cap_total: number | null;
  cap_per_customer: number | null;
  allowed_methods: OfferMethod[];
  cap_recredit_on_refund: boolean;
}

// POST /v1/offers/{offer_id}/edit (errata S18-E17): economic fields are
// edit-by-versioning (a new immutable version row; in-flight reservations pin
// the version they reserved against); title fields mutate in place. Only the
// fields being changed are sent.
export interface EditOfferInput {
  percent_bps?: number;
  windows?: OfferWindow[];
  valid_from?: string;
  valid_until?: string;
  min_spend_minor?: number;
  max_discount_minor?: number;
  cap_per_day?: number;
  cap_total?: number;
  cap_per_customer?: number;
  allowed_methods?: OfferMethod[];
  title?: string;
  title_bn?: string;
}

// ---------------------------------------------------------------------------
// Display helpers (presentation arithmetic on server-provided integers only —
// the portal renders, it never recomputes business values)
// ---------------------------------------------------------------------------

// Utilization in basis points (10000 = 100%) from server counts + caps.
// Integer math only; counts and caps are small integers by DDL CHECK.
export function utilizationBps(used: number, cap: number | null): number | null {
  if (cap === null || cap <= 0 || !Number.isInteger(used) || !Number.isInteger(cap)) return null;
  return Math.floor((used * 10_000) / cap);
}

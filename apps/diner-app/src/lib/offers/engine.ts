// Deterministic offer-eligibility engine — a faithful TypeScript mirror of
// the kernel evaluator (bdpay/kernel/offers/eligibility.py, spec/18
// §Eligibility evaluation algorithm). Pure: no I/O, no clock reads — callers
// inject `nowUtc`. Integer paisa everywhere; all arithmetic is BigInt
// (floats are banned on the money path, conventions §6).
//
// Role split (errata S18-E20 posture): in dev-mock mode this module IS the
// mock server's evaluator; in real-API mode the SERVER is authoritative and
// this module only powers the display preview. The two can never disagree on
// the math because both floor with integer division and cap with
// max_discount_minor exactly as the spec specifies.

export type Day = "MON" | "TUE" | "WED" | "THU" | "FRI" | "SAT" | "SUN";
export type OfferKind = "PERCENT_OFF" | "BOGO";
export type OfferState = "DRAFT" | "ACTIVE" | "PAUSED" | "EXHAUSTED" | "EXPIRED" | "ARCHIVED";

export interface OfferWindow {
  days: Day[];
  start_local: string; // "HH:MM" Asia/Dhaka (fixed +06:00, no DST)
  end_local: string; // "HH:MM"; start inclusive, end EXCLUSIVE (spec/18 step 3)
}

export interface OfferSnapshot {
  offer_id: string;
  merchant_id: string;
  version: number;
  kind: OfferKind;
  percent_bps: number | null;
  title: string;
  title_bn: string;
  windows: OfferWindow[];
  valid_from: string; // RFC3339 UTC
  valid_until: string; // RFC3339 UTC
  min_spend_minor: number;
  max_discount_minor: number | null;
  cap_per_day: number | null;
  cap_total: number | null;
  cap_per_customer: number | null;
  allowed_methods: string[];
  state: OfferState;
}

export type ReasonCode =
  | "OFFER_NOT_ACTIVE"
  | "OUTSIDE_WINDOW"
  | "BEFORE_VALIDITY"
  | "AFTER_VALIDITY"
  | "MIN_SPEND_NOT_MET"
  | "METHOD_NOT_ALLOWED"
  | "CAP_DAY_EXHAUSTED"
  | "CAP_TOTAL_EXHAUSTED"
  | "CAP_CUSTOMER_EXHAUSTED"
  | "MERCHANT_NOT_ACTIVE"
  | "BOGO_LINE_ITEMS_MISSING";

export interface EligibilityResult {
  eligible: boolean;
  discount_minor: number; // 0 when ineligible
  reasons: ReasonCode[]; // empty when eligible; ALL failing reasons, not just first
}

/** Typed refusal for D2 (errata S18-E7): BOGO is schema-reserved,
 * build-REFUSED at every depth. No BOGO discount math exists. */
export class BogoNotEnabledError extends Error {
  readonly code = "bogo_not_yet_enabled";
  constructor() {
    super("BOGO offers are not yet enabled (pilot is PERCENT_OFF-only)");
  }
}

/** Engine invariant breach (spec/18: 0 <= discount < gross — the rail must
 * always move at least 1 paisa). */
export class DiscountEqualsGrossError extends Error {
  readonly code = "discount_equals_gross";
  constructor() {
    super("discount must stay strictly below the gross amount");
  }
}

// ---------------------------------------------------------------------------
// Asia/Dhaka time (fixed UTC+06:00, no DST table needed — spec/18 step 3)
// ---------------------------------------------------------------------------

const DHAKA_OFFSET_MS = 6 * 3600 * 1000;
const DAY_NAMES: Day[] = ["SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"];

export interface DhakaParts {
  day: Day;
  hhmm: string;
  dateStr: string; // "YYYY-MM-DD" Asia/Dhaka calendar date (counter day-scope key)
}

export function dhakaParts(isoUtc: string): DhakaParts {
  const ms = Date.parse(isoUtc);
  if (!Number.isFinite(ms)) {
    throw new Error(`invalid RFC3339 timestamp: ${isoUtc}`);
  }
  const t = new Date(ms + DHAKA_OFFSET_MS);
  const day = DAY_NAMES[t.getUTCDay()] as Day;
  const hh = String(t.getUTCHours()).padStart(2, "0");
  const mm = String(t.getUTCMinutes()).padStart(2, "0");
  const dateStr = `${t.getUTCFullYear()}-${String(t.getUTCMonth() + 1).padStart(2, "0")}-${String(
    t.getUTCDate(),
  ).padStart(2, "0")}`;
  return { day, hhmm: `${hh}:${mm}`, dateStr };
}

/** Boundary rule (spec/18): start_local INCLUSIVE, end_local EXCLUSIVE.
 * "HH:MM" strings compare correctly lexicographically. */
export function windowMatches(windows: OfferWindow[], nowUtc: string): boolean {
  const { day, hhmm } = dhakaParts(nowUtc);
  return windows.some(
    (w) => w.days.includes(day) && w.start_local <= hhmm && hhmm < w.end_local,
  );
}

// ---------------------------------------------------------------------------
// Discount arithmetic — integer-only, floor (customer's disfavor by <=1 paisa)
// ---------------------------------------------------------------------------

export function computeDiscountMinor(
  grossAmountMinor: number,
  percentBps: number,
  maxDiscountMinor: number | null,
): number {
  if (!Number.isSafeInteger(grossAmountMinor) || grossAmountMinor <= 0) {
    throw new Error("gross_amount_minor must be a positive integer (paisa)");
  }
  if (!Number.isSafeInteger(percentBps) || percentBps < 0) {
    throw new Error("percent_bps must be a non-negative integer");
  }
  let discount = (BigInt(grossAmountMinor) * BigInt(percentBps)) / 10000n; // floor
  if (maxDiscountMinor !== null) {
    if (!Number.isSafeInteger(maxDiscountMinor) || maxDiscountMinor <= 0) {
      throw new Error("max_discount_minor must be a positive integer (paisa)");
    }
    const cap = BigInt(maxDiscountMinor);
    if (discount > cap) discount = cap;
  }
  // Invariant: 0 <= discount < gross (the rail must always move >= 1 paisa).
  if (discount >= BigInt(grossAmountMinor)) {
    throw new DiscountEqualsGrossError();
  }
  const out = Number(discount);
  if (!Number.isSafeInteger(out)) {
    throw new Error("discount_minor exceeds safe integer range");
  }
  return out;
}

// ---------------------------------------------------------------------------
// Eligibility evaluation (spec/18 order; ALL failing reasons returned)
// ---------------------------------------------------------------------------

export interface AdvisoryCounters {
  // Advisory/fast-fail ONLY — never trusted for enforcement (spec/18 step 6).
  dayUsed?: number;
  totalUsed?: number;
  customerUsed?: number;
}

export function evaluateEligibility(
  offer: OfferSnapshot,
  opts: {
    merchantActive: boolean;
    nowUtc: string;
    grossAmountMinor?: number | null;
    method?: string | null;
    counters?: AdvisoryCounters;
  },
): EligibilityResult {
  if (offer.kind === "BOGO") {
    // D2 defense-in-depth (errata S18-E7): typed refusal, never evaluated.
    throw new BogoNotEnabledError();
  }
  const reasons: ReasonCode[] = [];
  const nowMs = Date.parse(opts.nowUtc);
  if (!Number.isFinite(nowMs)) throw new Error("invalid nowUtc");

  // 1. Offer state; merchant state.
  if (offer.state !== "ACTIVE") reasons.push("OFFER_NOT_ACTIVE");
  if (!opts.merchantActive) reasons.push("MERCHANT_NOT_ACTIVE");

  // 2. Validity range.
  if (nowMs < Date.parse(offer.valid_from)) reasons.push("BEFORE_VALIDITY");
  if (nowMs > Date.parse(offer.valid_until)) reasons.push("AFTER_VALIDITY");

  // 3. Window match (Asia/Dhaka; start inclusive, end exclusive).
  if (!windowMatches(offer.windows, opts.nowUtc)) reasons.push("OUTSIDE_WINDOW");

  // 4. Min spend (only checkable when a gross amount is supplied).
  const gross = opts.grossAmountMinor ?? null;
  if (gross !== null && gross < offer.min_spend_minor) {
    reasons.push("MIN_SPEND_NOT_MET");
  }

  // 5. Method.
  if (opts.method != null && !offer.allowed_methods.includes(opts.method)) {
    reasons.push("METHOD_NOT_ALLOWED");
  }

  // 6. Advisory counter reads (fast-fail; the atomic reservation is the
  //    authoritative check).
  const c = opts.counters ?? {};
  if (offer.cap_per_day !== null && (c.dayUsed ?? 0) >= offer.cap_per_day) {
    reasons.push("CAP_DAY_EXHAUSTED");
  }
  if (offer.cap_total !== null && (c.totalUsed ?? 0) >= offer.cap_total) {
    reasons.push("CAP_TOTAL_EXHAUSTED");
  }
  if (
    offer.cap_per_customer !== null &&
    (c.customerUsed ?? 0) >= offer.cap_per_customer
  ) {
    reasons.push("CAP_CUSTOMER_EXHAUSTED");
  }

  if (reasons.length > 0) {
    return { eligible: false, discount_minor: 0, reasons };
  }

  // 7. Discount computation (PERCENT_OFF only in the pilot, decision D2).
  let discount = 0;
  if (gross !== null) {
    if (offer.percent_bps === null) {
      throw new Error("PERCENT_OFF offer without percent_bps (schema breach)");
    }
    discount = computeDiscountMinor(gross, offer.percent_bps, offer.max_discount_minor);
  }
  return { eligible: true, discount_minor: discount, reasons: [] };
}

// ---------------------------------------------------------------------------
// Fail-closed counter reservation (spec/18 §Cap enforcement; errata S18-E14:
// narrowest-first — customer -> day -> total — so a refusal always names the
// NARROWEST exhausted scope). All-or-nothing: nothing is consumed unless
// every applicable scope has room.
// ---------------------------------------------------------------------------

export type CounterScope = "customer" | "day" | "total";

export interface CounterState {
  used: number;
  cap_limit: number;
}

const SCOPE_ORDER: CounterScope[] = ["customer", "day", "total"];

export type ReserveOutcome =
  | { ok: true }
  | { ok: false; exhausted_scope: CounterScope };

export function reserveCounters(
  counters: { scope: CounterScope; counter: CounterState }[],
): ReserveOutcome {
  const sorted = [...counters].sort(
    (a, b) => SCOPE_ORDER.indexOf(a.scope) - SCOPE_ORDER.indexOf(b.scope),
  );
  for (const { scope, counter } of sorted) {
    if (counter.used < 0 || counter.cap_limit < 1) {
      // Fail CLOSED on malformed counter state — never over-redeem.
      return { ok: false, exhausted_scope: scope };
    }
    if (counter.used >= counter.cap_limit) {
      return { ok: false, exhausted_scope: scope };
    }
  }
  for (const { counter } of sorted) {
    counter.used += 1;
  }
  return { ok: true };
}

/** Re-credit on RELEASED/REVERSED — `SET used = used - 1 WHERE used > 0`. */
export function recreditCounter(counter: CounterState): void {
  if (counter.used > 0) counter.used -= 1;
}

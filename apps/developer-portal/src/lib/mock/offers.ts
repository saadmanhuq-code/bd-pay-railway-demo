// Deterministic mock state for the spec/18 merchant offer surface.
// Mirrors bdpay/kernel/offers/service.py (OfferService) contract-for-contract:
// same validation order, same error codes, refusal-first FSM (any
// (state, trigger) pair not in the table is DENIED), edit-by-versioning
// (errata S18-E5), idempotent lifecycle replays, live counter readout shape.
// Same rules as store.ts: no Math.random / Date.now in the data path.

import type { ErrorType } from "@/lib/api/types";
import type {
  CreateOfferInput,
  EditOfferInput,
  Offer,
  OfferCounters,
  OfferDetail,
  OfferKind,
  OfferMethod,
  OfferState,
  OfferWindow,
} from "@/lib/api/offerTypes";
import {
  MAX_WINDOW_ROWS,
  OFFER_METHODS,
  OFFER_TERMINAL_STATES,
  PERCENT_BPS_CEILING,
  PERCENT_BPS_FLOOR,
  VALIDITY_MAX_DAYS,
  WINDOW_DAY_CODES,
} from "@/lib/api/offerTypes";
import { DEMO_MERCHANT, SNAPSHOT_AT, detHex, mockError, type MockResult } from "./store";

// ---------------------------------------------------------------------------
// Deterministic helpers (mirrors launch.ts; store.ts helpers are module-private)
// ---------------------------------------------------------------------------

const EPOCH_MS = Date.parse(SNAPSHOT_AT);

function tsAt(offsetMinutes: number): string {
  return new Date(EPOCH_MS + offsetMinutes * 60_000).toISOString().replace(".000Z", "Z");
}

let offerSeq = 0;
// Mutation clock = WALL clock (second resolution, strictly monotonic so two
// mutations in the same second still order). The seed epoch is anchored at
// process boot, so a long-lived Railway replica previously stamped "just now"
// actions (approve, create key, add webhook, link expiry…) with the boot hour
// — e.g. an approval decided today showed yesterday 12:30.
let lastMutationMs = 0;
function mutationTs(): string {
  offerSeq += 1;
  const nowSec = Math.floor(Date.now() / 1000) * 1000;
  lastMutationMs = Math.max(nowSec, lastMutationMs + 1000);
  return new Date(lastMutationMs).toISOString().replace(".000Z", "Z");
}

function id(prefix: string, key: string): string {
  return `${prefix}_${detHex(`offers:${key}`)}`;
}

function ok<T>(body: T, status = 200): MockResult<T> {
  return { status, body };
}

function err(type: ErrorType, code: string, message: string): MockResult<never> {
  return mockError(type, code, message);
}

// ---------------------------------------------------------------------------
// Refusal-first Offer FSM (transcribed from bdpay/kernel/offers/states.py —
// OFFER_TABLE; merchant-reachable triggers only). Terminal states are absent
// from the map entirely: every trigger on them is DENIED.
// ---------------------------------------------------------------------------

const OFFER_TRANSITIONS: Record<string, Partial<Record<OfferState, OfferState>>> = {
  activate: { DRAFT: "ACTIVE" },
  pause: { ACTIVE: "PAUSED" },
  resume: { PAUSED: "ACTIVE" },
  archive: { DRAFT: "ARCHIVED", ACTIVE: "ARCHIVED", PAUSED: "ARCHIVED", EXHAUSTED: "ARCHIVED" },
  cap_raised: { EXHAUSTED: "ACTIVE" },
};

export type OfferLifecycleAction = "activate" | "pause" | "resume" | "archive";

// ---------------------------------------------------------------------------
// Seed data — one merchant (DEMO_MERCHANT), offers across the FSM states.
// The ACTIVE seed mirrors the spec/18 §API example (weekday-afternoon 25%
// off; counters 14 / 312 / 2).
// ---------------------------------------------------------------------------

interface OfferRecord extends Offer {
  counters: OfferCounters;
  created_idempotency_key: string;
}

function seedOffer(args: {
  key: string;
  kind?: OfferKind;
  percentBps: number;
  title: string;
  titleBn: string;
  windows: OfferWindow[];
  validFromOffsetMin: number;
  validUntilOffsetMin: number;
  minSpendMinor?: number;
  maxDiscountMinor?: number | null;
  capPerDay?: number | null;
  capTotal?: number | null;
  capPerCustomer?: number | null;
  methods: OfferMethod[];
  state: OfferState;
  version?: number;
  counters: OfferCounters;
  createdOffsetMin: number;
}): OfferRecord {
  return {
    offer_id: id("off", args.key),
    version: args.version ?? 1,
    merchant_id: DEMO_MERCHANT.merchant_id,
    kind: args.kind ?? "PERCENT_OFF",
    percent_bps: args.percentBps,
    bogo_config: null,
    title: args.title,
    title_bn: args.titleBn,
    windows: args.windows,
    valid_from: tsAt(args.validFromOffsetMin),
    valid_until: tsAt(args.validUntilOffsetMin),
    min_spend_minor: args.minSpendMinor ?? 0,
    max_discount_minor: args.maxDiscountMinor ?? null,
    cap_per_day: args.capPerDay ?? null,
    cap_total: args.capTotal ?? null,
    cap_per_customer: args.capPerCustomer ?? null,
    allowed_methods: args.methods,
    cap_recredit_on_refund: true,
    state: args.state,
    currency: "BDT",
    created_at: tsAt(args.createdOffsetMin),
    updated_at: tsAt(args.createdOffsetMin),
    counters: args.counters,
    created_idempotency_key: `seed:${args.key}`,
  };
}

const offers: OfferRecord[] = [
  // The spec/18 §API surface example offer, live.
  seedOffer({
    key: "weekday-afternoon",
    percentBps: 2500,
    title: "Weekday afternoon 25% off",
    titleBn: "সাপ্তাহিক দুপুরে ২৫% ছাড়",
    windows: [{ days: ["MON", "TUE", "WED", "THU"], start_local: "14:30", end_local: "18:00" }],
    validFromOffsetMin: -60 * 24 * 10,
    validUntilOffsetMin: 60 * 24 * 80,
    minSpendMinor: 50000,
    maxDiscountMinor: 40000,
    capPerDay: 20,
    capTotal: 1000,
    capPerCustomer: 2,
    methods: ["BANGLA_QR", "BKASH", "NAGAD"],
    state: "ACTIVE",
    counters: { redeemed_today: 14, redeemed_total: 312, reserved_now: 2 },
    createdOffsetMin: -60 * 24 * 12,
  }),
  seedOffer({
    key: "late-lunch-draft",
    percentBps: 1500,
    title: "Late lunch 15% off",
    titleBn: "দেরি দুপুরের খাবারে ১৫% ছাড়",
    windows: [{ days: ["SAT", "SUN"], start_local: "15:00", end_local: "17:00" }],
    validFromOffsetMin: 60 * 24 * 2,
    validUntilOffsetMin: 60 * 24 * 90,
    minSpendMinor: 30000,
    capPerDay: 10,
    capTotal: 400,
    methods: ["BANGLA_QR"],
    state: "DRAFT",
    counters: { redeemed_today: 0, redeemed_total: 0, reserved_now: 0 },
    createdOffsetMin: -60 * 5,
  }),
  seedOffer({
    key: "dinner-paused",
    percentBps: 3000,
    title: "Early dinner 30% off",
    titleBn: "আগেভাগে রাতের খাবারে ৩০% ছাড়",
    windows: [{ days: ["MON", "TUE", "WED"], start_local: "18:00", end_local: "19:30" }],
    validFromOffsetMin: -60 * 24 * 30,
    validUntilOffsetMin: 60 * 24 * 40,
    minSpendMinor: 80000,
    maxDiscountMinor: 60000,
    capPerDay: 8,
    capTotal: 500,
    capPerCustomer: 1,
    methods: ["BANGLA_QR", "BKASH"],
    state: "PAUSED",
    version: 2,
    counters: { redeemed_today: 0, redeemed_total: 57, reserved_now: 0 },
    createdOffsetMin: -60 * 24 * 32,
  }),
  // EXHAUSTED: counters at cap_total — only a cap raise reactivates it.
  seedOffer({
    key: "launch-exhausted",
    percentBps: 5000,
    title: "Launch week 50% off",
    titleBn: "উদ্বোধনী সপ্তাহে ৫০% ছাড়",
    windows: [{ days: ["FRI", "SAT"], start_local: "12:00", end_local: "16:00" }],
    validFromOffsetMin: -60 * 24 * 20,
    validUntilOffsetMin: 60 * 24 * 20,
    minSpendMinor: 40000,
    maxDiscountMinor: 30000,
    capPerDay: 25,
    capTotal: 150,
    capPerCustomer: 1,
    methods: ["BANGLA_QR"],
    state: "EXHAUSTED",
    counters: { redeemed_today: 9, redeemed_total: 150, reserved_now: 0 },
    createdOffsetMin: -60 * 24 * 21,
  }),
  seedOffer({
    key: "iftar-expired",
    percentBps: 2000,
    title: "Iftar special 20% off",
    titleBn: "ইফতার বিশেষ ২০% ছাড়",
    windows: [{ days: ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"], start_local: "17:30", end_local: "19:00" }],
    validFromOffsetMin: -60 * 24 * 120,
    validUntilOffsetMin: -60 * 24 * 90,
    minSpendMinor: 25000,
    capPerDay: 30,
    capTotal: 900,
    methods: ["BANGLA_QR", "BKASH", "NAGAD"],
    state: "EXPIRED",
    counters: { redeemed_today: 0, redeemed_total: 763, reserved_now: 0 },
    createdOffsetMin: -60 * 24 * 125,
  }),
];

// Create-replay detection (errata S18-E10a): one offer per
// (merchant, Idempotency-Key); replay returns the stored offer.
const offersByCreateKey = new Map<string, OfferRecord>(offers.map((o) => [o.created_idempotency_key, o]));

// The merchant's active connector method set (spec/18: allowed_methods must
// be a subset; code method_not_supported_by_merchant). ROCKET intentionally
// absent so the refusal path is demo-able.
const MERCHANT_SUPPORTED_METHODS: ReadonlySet<string> = new Set(["BANGLA_QR", "BKASH", "NAGAD", "CARD", "NPSB_IBFT"]);

// ---------------------------------------------------------------------------
// Views (offer_view parity: counters appear ONLY on the single-offer read)
// ---------------------------------------------------------------------------

function toOffer(rec: OfferRecord): Offer {
  const { counters: _c, created_idempotency_key: _k, ...view } = rec;
  return view;
}

function toDetail(rec: OfferRecord): OfferDetail {
  const { created_idempotency_key: _k, ...view } = rec;
  return view;
}

function findOffer(offerId: string): OfferRecord | undefined {
  // Merchant API keys see only their own offers (RLS posture): unknown and
  // foreign ids are the same 404 — no existence leak.
  return offers.find((o) => o.offer_id === offerId && o.merchant_id === DEMO_MERCHANT.merchant_id);
}

export function listOffersMock(state: string | null): MockResult<{ data: Offer[]; next_cursor: null }> {
  if (state !== null && state !== "" && !(OFFER_TERMINAL_STATES as string[]).concat("DRAFT", "ACTIVE", "PAUSED", "EXHAUSTED").includes(state)) {
    return err("invalid_request", "invalid_request", `unknown offer state '${state}'.`);
  }
  const rows = offers.filter((o) => state === null || state === "" || o.state === state);
  return ok({ data: rows.map(toOffer), next_cursor: null });
}

export function getOfferMock(offerId: string): MockResult<OfferDetail> {
  const rec = findOffer(offerId);
  if (!rec) return err("not_found", "offer_not_found", "offer not found");
  return ok(toDetail(rec));
}

// ---------------------------------------------------------------------------
// Create (mirrors OfferService.create_offer validation order + codes)
// ---------------------------------------------------------------------------

const HHMM_RE = /^(?:[01][0-9]|2[0-3]):[0-5][0-9]$/;

function windowsInvalid(windows: OfferWindow[]): MockResult<never> | null {
  if (!Array.isArray(windows) || windows.length === 0) {
    return err("invalid_request", "windows_required", "at least one time window is required");
  }
  if (windows.length > MAX_WINDOW_ROWS) {
    return err("invalid_request", "invalid_window", `at most ${MAX_WINDOW_ROWS} window rows are allowed`);
  }
  for (const w of windows) {
    if (!Array.isArray(w.days) || w.days.length === 0 || new Set(w.days).size !== w.days.length) {
      return err("invalid_request", "invalid_window", "window days must be non-empty and unique");
    }
    for (const d of w.days) {
      if (!(WINDOW_DAY_CODES as readonly string[]).includes(d)) {
        return err("invalid_request", "invalid_window", `unknown window day '${d}'; use MON..SUN`);
      }
    }
    if (!HHMM_RE.test(w.start_local) || !HHMM_RE.test(w.end_local) || !(w.start_local < w.end_local)) {
      return err("invalid_request", "invalid_window", "window start_local must be HH:MM and before end_local (Asia/Dhaka)");
    }
  }
  for (let i = 0; i < windows.length; i++) {
    for (let j = i + 1; j < windows.length; j++) {
      const a = windows[i]!;
      const b = windows[j]!;
      const shared = a.days.some((d) => b.days.includes(d));
      if (shared && a.start_local < b.end_local && b.start_local < a.end_local) {
        return err("invalid_request", "overlapping_windows", "windows overlap on the same day");
      }
    }
  }
  return null;
}

function intOrNull(v: unknown): number | null | "bad" {
  if (v === null || v === undefined) return null;
  if (typeof v !== "number" || !Number.isInteger(v)) return "bad";
  return v;
}

export function createOfferMock(input: CreateOfferInput, idempotencyKey: string): MockResult<Offer> {
  if (DEMO_MERCHANT.status !== "ACTIVE") {
    return err("authorization", "merchant_not_active", "merchant must be ACTIVE to manage offers");
  }
  const replay = offersByCreateKey.get(idempotencyKey);
  if (replay !== undefined) {
    if (replay.kind !== input.kind || replay.title !== input.title) {
      return err("idempotency_conflict", "idempotency_conflict", "Idempotency-Key replayed with mismatched parameters");
    }
    return ok(toOffer(replay)); // create replay returns the stored offer
  }
  if (input.kind !== "PERCENT_OFF" && input.kind !== "BOGO") {
    return err("invalid_request", "invalid_request", "kind must be PERCENT_OFF or BOGO");
  }
  if (input.kind === "BOGO") {
    // Decision D2 (binding): schema accepts BOGO, build refuses it.
    return err("invalid_request", "bogo_not_yet_enabled", "BOGO offers are not yet enabled (pilot is PERCENT_OFF-only)");
  }
  const percent = intOrNull(input.percent_bps);
  if (percent === null || percent === "bad") {
    return err("invalid_request", "invalid_request", "percent_bps is required for PERCENT_OFF");
  }
  if (percent < PERCENT_BPS_FLOOR || percent > PERCENT_BPS_CEILING) {
    return err(
      "invalid_request",
      "percent_bps_out_of_range",
      `percent_bps must be within [${PERCENT_BPS_FLOOR}, ${PERCENT_BPS_CEILING}]`,
    );
  }
  const windowsRefusal = windowsInvalid(input.windows);
  if (windowsRefusal) return windowsRefusal;
  const from = Date.parse(input.valid_from);
  const until = Date.parse(input.valid_until);
  if (!Number.isFinite(from) || !Number.isFinite(until)) {
    return err("invalid_request", "invalid_validity_range", "valid_from and valid_until must be RFC3339 timestamps");
  }
  if (!(from < until)) {
    return err("invalid_request", "invalid_validity_range", "valid_from must be before valid_until");
  }
  if (until - from > VALIDITY_MAX_DAYS * 24 * 3_600_000) {
    return err("invalid_request", "invalid_validity_range", `validity range is capped at ${VALIDITY_MAX_DAYS} days`);
  }
  if (!Array.isArray(input.allowed_methods) || input.allowed_methods.length === 0) {
    return err("invalid_request", "invalid_request", "allowed_methods must be a non-empty list");
  }
  for (const m of input.allowed_methods) {
    if (!(OFFER_METHODS as readonly string[]).includes(m)) {
      return err("invalid_request", "method_unsupported", `unknown payment method '${m}'`);
    }
    if (!MERCHANT_SUPPORTED_METHODS.has(m)) {
      return err(
        "invalid_request",
        "method_not_supported_by_merchant",
        "allowed_methods must be a subset of the merchant's active connector methods",
      );
    }
  }
  const minSpend = intOrNull(input.min_spend_minor);
  if (minSpend === "bad" || (minSpend !== null && minSpend < 0)) {
    return err("invalid_request", "amount_must_be_integer", "min_spend_minor must be non-negative integer paisa");
  }
  const maxDiscount = intOrNull(input.max_discount_minor);
  if (maxDiscount === "bad" || (maxDiscount !== null && maxDiscount < 1)) {
    return err("invalid_request", "amount_must_be_integer", "max_discount_minor must be positive integer paisa");
  }
  const caps: (number | null)[] = [];
  for (const [name, raw] of [
    ["cap_per_day", input.cap_per_day],
    ["cap_total", input.cap_total],
    ["cap_per_customer", input.cap_per_customer],
  ] as const) {
    const v = intOrNull(raw);
    if (v === "bad" || (v !== null && v < 1)) {
      return err("invalid_request", "invalid_request", `${name} must be an integer >= 1 when present`);
    }
    caps.push(v);
  }
  const [capPerDay, capTotal, capPerCustomer] = caps as [number | null, number | null, number | null];
  if (capPerDay === null && capTotal === null) {
    // The circuit breaker is not optional: uncapped offers do not exist.
    return err("invalid_request", "cap_required", "at least one of cap_per_day / cap_total is required");
  }
  if (capPerDay !== null && capTotal !== null && capPerDay > capTotal) {
    return err("invalid_request", "invalid_request", "cap_per_day must be <= cap_total");
  }
  if (input.title.trim() === "" || input.title_bn.trim() === "") {
    return err("invalid_request", "invalid_request", "title and title_bn are both required");
  }
  const at = mutationTs();
  const rec: OfferRecord = {
    offer_id: id("off", `created:${idempotencyKey}`),
    version: 1,
    merchant_id: DEMO_MERCHANT.merchant_id,
    kind: "PERCENT_OFF",
    percent_bps: percent,
    bogo_config: null,
    title: input.title,
    title_bn: input.title_bn,
    windows: input.windows,
    valid_from: input.valid_from,
    valid_until: input.valid_until,
    min_spend_minor: minSpend ?? 0,
    max_discount_minor: maxDiscount,
    cap_per_day: capPerDay,
    cap_total: capTotal,
    cap_per_customer: capPerCustomer,
    allowed_methods: input.allowed_methods,
    cap_recredit_on_refund: input.cap_recredit_on_refund !== false,
    state: "DRAFT",
    currency: "BDT",
    created_at: at,
    updated_at: at,
    counters: { redeemed_today: 0, redeemed_total: 0, reserved_now: 0 },
    created_idempotency_key: idempotencyKey,
  };
  offers.unshift(rec);
  offersByCreateKey.set(idempotencyKey, rec);
  return { status: 201, body: toOffer(rec) };
}

// ---------------------------------------------------------------------------
// Lifecycle (sub-resource POSTs; idempotent replays per the kernel service)
// ---------------------------------------------------------------------------

export function offerLifecycleMock(offerId: string, action: OfferLifecycleAction): MockResult<Offer> {
  const rec = findOffer(offerId);
  if (!rec) return err("not_found", "offer_not_found", "offer not found");

  // Idempotent replays (kernel parity): the target state replays as 200.
  if (action === "activate" && rec.state === "ACTIVE") return ok(toOffer(rec));
  if (action === "pause" && rec.state === "PAUSED") return ok(toOffer(rec));
  if (action === "resume" && rec.state === "ACTIVE") return ok(toOffer(rec));
  if (action === "archive" && rec.state === "ARCHIVED") return ok(toOffer(rec));

  // Guards that run BEFORE the transition table (kernel parity).
  if (action === "activate") {
    if (DEMO_MERCHANT.status !== "ACTIVE") {
      return err("authorization", "merchant_not_active", "merchant must be ACTIVE to activate offers");
    }
    if (Date.parse(SNAPSHOT_AT) >= Date.parse(rec.valid_until)) {
      return err("conflict", "invalid_state_transition", "offer validity has already ended");
    }
  }
  if (action === "resume" && Date.parse(SNAPSHOT_AT) >= Date.parse(rec.valid_until)) {
    return err("conflict", "invalid_state_transition", "offer validity has already ended");
  }
  if (action === "archive" && rec.state !== "DRAFT" && rec.counters.reserved_now > 0) {
    return err("conflict", "reserved_redemptions_outstanding", "offer has RESERVED redemptions outstanding; archive refused");
  }

  // Refusal-first: any (state, trigger) pair not in the table is DENIED.
  const to = OFFER_TRANSITIONS[action]?.[rec.state];
  if (to === undefined) {
    return err("conflict", "invalid_state_transition", `trigger '${action}' is not allowed from state ${rec.state}`);
  }
  rec.state = to;
  rec.updated_at = mutationTs();
  return ok(toOffer(rec));
}

// ---------------------------------------------------------------------------
// Edit (errata S18-E5/E17): economic fields version; title fields mutate in
// place; EXHAUSTED reactivates only via a cap_total raise (cap_raised row).
// ---------------------------------------------------------------------------

const ECONOMIC_FIELDS = new Set([
  "percent_bps",
  "windows",
  "valid_from",
  "valid_until",
  "min_spend_minor",
  "max_discount_minor",
  "cap_per_day",
  "cap_total",
  "cap_per_customer",
  "allowed_methods",
]);
const MUTABLE_FIELDS = new Set(["title", "title_bn"]);

export function editOfferMock(offerId: string, changes: EditOfferInput): MockResult<Offer> {
  const rec = findOffer(offerId);
  if (!rec) return err("not_found", "offer_not_found", "offer not found");
  if ((OFFER_TERMINAL_STATES as string[]).includes(rec.state)) {
    return err("conflict", "invalid_state_transition", "terminal offers are immutable");
  }
  const keys = Object.keys(changes);
  if (keys.length === 0) {
    return err("invalid_request", "invalid_request", "no changes supplied");
  }
  const unknown = keys.filter((k) => !ECONOMIC_FIELDS.has(k) && !MUTABLE_FIELDS.has(k));
  if (unknown.length > 0) {
    return err("invalid_request", "invalid_request", `unknown or immutable fields: ${unknown.sort().join(", ")}`);
  }
  const economic = keys.filter((k) => ECONOMIC_FIELDS.has(k));

  // Re-run the create-grade validations on the would-be record.
  const next = { ...rec, ...changes } as OfferRecord;
  if (economic.length > 0) {
    if (next.percent_bps === null || !Number.isInteger(next.percent_bps)) {
      return err("invalid_request", "invalid_request", "percent_bps must be an integer");
    }
    if (next.percent_bps < PERCENT_BPS_FLOOR || next.percent_bps > PERCENT_BPS_CEILING) {
      return err(
        "invalid_request",
        "percent_bps_out_of_range",
        `percent_bps must be within [${PERCENT_BPS_FLOOR}, ${PERCENT_BPS_CEILING}]`,
      );
    }
    const windowsRefusal = windowsInvalid(next.windows);
    if (windowsRefusal) return windowsRefusal;
    const from = Date.parse(next.valid_from);
    const until = Date.parse(next.valid_until);
    if (!Number.isFinite(from) || !Number.isFinite(until) || !(from < until) || until - from > VALIDITY_MAX_DAYS * 24 * 3_600_000) {
      return err("invalid_request", "invalid_validity_range", "valid_from/valid_until must be a valid range of at most 366 days");
    }
    const capPerDay = next.cap_per_day;
    const capTotal = next.cap_total;
    for (const v of [capPerDay, capTotal, next.cap_per_customer]) {
      if (v !== null && (!Number.isInteger(v) || v < 1)) {
        return err("invalid_request", "invalid_request", "caps must be integers >= 1 when present");
      }
    }
    if (capPerDay === null && capTotal === null) {
      return err("invalid_request", "cap_required", "at least one of cap_per_day / cap_total is required");
    }
    if (capPerDay !== null && capTotal !== null && capPerDay > capTotal) {
      return err("invalid_request", "invalid_request", "cap_per_day must be <= cap_total");
    }
    // EXHAUSTED --cap_raised--> ACTIVE happens exactly when the edit raises
    // cap_total; any other economic edit on EXHAUSTED is refused.
    const capRaised =
      rec.state === "EXHAUSTED" && typeof capTotal === "number" && rec.cap_total !== null && capTotal > rec.cap_total;
    if (rec.state === "EXHAUSTED" && !capRaised) {
      return err("conflict", "invalid_state_transition", "an EXHAUSTED offer reactivates only by raising cap_total");
    }
    rec.version += 1; // new immutable economics version (S18-E5)
    if (capRaised) rec.state = "ACTIVE";
  }
  for (const k of keys) {
    // Apply the validated changes onto the live record.
    (rec as unknown as Record<string, unknown>)[k] = (changes as Record<string, unknown>)[k];
  }
  rec.updated_at = mutationTs();
  return ok(toOffer(rec));
}

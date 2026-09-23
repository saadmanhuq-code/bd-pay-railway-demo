import { tagsForWindows } from "@/lib/demo/windowTags";
// In-app deterministic mock state for the diner-app. All seed data derives
// from fixed constants — no Math.random / Date.now anywhere in the data path
// (the developer-portal/ops-console mock pattern). The clock is the fixed
// SNAPSHOT_AT constant (injected-clock posture: the data path reads ONE
// deterministic instant). Mutations are FSM-consistent (spec/18 FSM 2,
// refusal-first) and counters are fail-closed via the shared engine — the
// same module that powers the UI preview, so mock-server math and display
// math cannot diverge.

import { createHmac } from "node:crypto";
import type {
  CheckoutMethod,
  DinerCustomer,
  DinerMerchant,
  DynamicQr,
  EligibleOffer,
  ErrorEnvelope,
  ErrorType,
  OtpChallenge,
  PaymentIntentView,
  RedemptionState,
} from "@/lib/api/types";
import {
  BogoNotEnabledError,
  DiscountEqualsGrossError,
  dhakaParts,
  evaluateEligibility,
  reserveCounters,
  type CounterScope,
  type CounterState,
  type OfferSnapshot,
  type ReasonCode,
} from "@/lib/offers/engine";

// ---------------------------------------------------------------------------
// Deterministic helpers
// ---------------------------------------------------------------------------

const SEED = "bdpay-diner-app-mock-seed-v1";

/** FNV-1a derived deterministic hex id suffix (developer-portal pattern,
 * corrected: the donor variant emitted hex before consuming the input, so
 * distinct inputs collided on the shared SEED prefix — here the FULL string
 * is absorbed first, then the state is expanded with a counter). */
export function detHex(input: string, length = 24): string {
  const src = `${SEED}:${input}`;
  let h = 0x811c9dc5;
  for (let i = 0; i < src.length; i++) {
    const code = src.charCodeAt(i);
    h ^= code & 0xff;
    h = Math.imul(h, 0x01000193) >>> 0;
    h ^= (code >>> 8) & 0xff; // high byte too (Bengali chars are > 0xff)
    h = Math.imul(h, 0x01000193) >>> 0;
  }
  let out = "";
  let counter = 0;
  while (out.length < length) {
    h ^= counter & 0xff;
    h = Math.imul(h, 0x01000193) >>> 0;
    out += h.toString(16).padStart(8, "0");
    counter++;
  }
  return out.slice(0, length);
}

function id(prefix: string, key: string): string {
  return `${prefix}_${detHex(key)}`;
}

/** The mock clock — one fixed instant. 2026-06-13T10:00:00Z is Saturday
 * 16:00 Asia/Dhaka: inside the weekend windows, exactly ON the 16:00
 * inclusive start boundary of the Pizza Roma window. */
export const SNAPSHOT_AT = "2026-06-13T10:00:00Z";
const SNAPSHOT_DHAKA_DATE = dhakaParts(SNAPSHOT_AT).dateStr; // "2026-06-13"
const RESERVATION_TTL_MS = 30 * 60 * 1000; // = intent TTL, 30 min (spec/18)
const RESERVED_EXPIRES_AT = new Date(
  Date.parse(SNAPSHOT_AT) + RESERVATION_TTL_MS,
).toISOString().replace(".000Z", "Z");

export interface MockResult<T> {
  status: number;
  body: T | ErrorEnvelope;
}

const STATUS_BY_TYPE: Record<ErrorType, number> = {
  invalid_request: 400,
  authentication: 401,
  authorization: 403,
  rate_limit: 429,
  idempotency_conflict: 422,
  limit_exceeded: 422,
  sanctions_block: 403,
  aml_block: 403,
  connector_error: 502,
  conflict: 409,
  not_found: 404,
  internal: 500,
};

let requestSerial = 0;

export function mockError(type: ErrorType, code: string, message: string): MockResult<never> {
  requestSerial += 1;
  const envelope: ErrorEnvelope = {
    error: {
      type,
      code,
      message, // PII-free, static strings only
      request_id: `req_${detHex(`req|${code}|${requestSerial}`, 16)}`,
      doc_url: `https://docs.bdpay.example/errors/${code}`,
    },
  };
  return { status: STATUS_BY_TYPE[type], body: envelope };
}

// ---------------------------------------------------------------------------
// Demo diner (D1 — login required; the login relationship is the CAC asset)
// ---------------------------------------------------------------------------

export const DEMO_PHONE = "01712345678";
const DEMO_CUSTOMER_ID = id("cust", "demo-diner");
const DEV_OTP_SECRET = "bdpay-diner-app-dev-otp-secret-v1";
const OTP_TTL_MS = 5 * 60 * 1000;
const OTP_MAX_ATTEMPTS = 5;

interface OtpChallengeRecord {
  phone: string;
  challengeId: string;
  digest: string;
  attemptsRemaining: number;
  expires_at: string;
}

const otpChallengesByPhone = new Map<string, OtpChallengeRecord>();
let otpChallengeSeq = 0;

function mockOtpSecret(): string | null {
  const configured = process.env.BDPAY_MOCK_OTP_SECRET?.trim();
  if (configured) return configured;
  const mockFlag = process.env.BDPAY_ENABLE_MOCK?.trim().toLowerCase() ?? "";
  if (["1", "true", "yes", "on"].includes(mockFlag)) return DEV_OTP_SECRET;
  if (process.env.NODE_ENV === "production") return null;
  return DEV_OTP_SECRET;
}

function hmacHex(secret: string, input: string): string {
  return createHmac("sha256", secret).update(input).digest("hex");
}

function otpCode(secret: string, phone: string, challengeId: string): string {
  const digest = createHmac("sha256", secret).update(`otp-code|${phone}|${challengeId}`).digest();
  return String((digest.readUInt32BE(0) & 0x7fffffff) % 1_000_000).padStart(6, "0");
}

function otpDigest(secret: string, phone: string, challengeId: string, code: string): string {
  return hmacHex(secret, `otp-digest|${phone}|${challengeId}|${code}`);
}

function constantTimeEqual(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i += 1) {
    diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  }
  return diff === 0;
}

export function getDemoCustomer(): DinerCustomer {
  return {
    customer_id: DEMO_CUSTOMER_ID,
    display_name: "Demo Diner",
    phone_masked: "01•••••••678",
  };
}

export function otpChallenge(phone: string): MockResult<OtpChallenge> {
  const secret = mockOtpSecret();
  if (secret === null) {
    return mockError("internal", "mock_otp_secret_missing", "Mock OTP secret is not configured.");
  }
  otpChallengeSeq += 1;
  const challengeId = id("otpc", `challenge|${phone}|${otpChallengeSeq}`);
  // Wall-clock TTL (same posture as session cookie / QR) — SNAPSHOT_AT is for
  // deterministic offer windows, not auth challenge lifetime on a long-lived replica.
  const expiresAt = new Date(Date.now() + OTP_TTL_MS).toISOString().replace(/\.\d{3}Z$/, "Z");
  const code = otpCode(secret, phone, challengeId);
  otpChallengesByPhone.set(phone, {
    phone,
    challengeId,
    digest: otpDigest(secret, phone, challengeId, code),
    attemptsRemaining: OTP_MAX_ATTEMPTS,
    expires_at: expiresAt,
  });
  const body: OtpChallenge = {
    challenge_id: challengeId,
    expires_at: expiresAt,
  };
  const mockFlag = process.env.BDPAY_ENABLE_MOCK?.trim().toLowerCase() ?? "";
  const mockOn = ["1", "true", "yes", "on"].includes(mockFlag);
  // Simulator demos (incl. Railway production mock) surface the code on-screen —
  // there is no SMS rail. Live SMS path never sets BDPAY_ENABLE_MOCK.
  if (process.env.NODE_ENV !== "production" || mockOn) body.debug_otp_code = code;
  return { status: 200, body };
}

export function verifyOtpChallenge(phone: string, code: string): MockResult<{ ok: true }> {
  const secret = mockOtpSecret();
  if (secret === null) {
    return mockError("internal", "mock_otp_secret_missing", "Mock OTP secret is not configured.");
  }
  const rec = otpChallengesByPhone.get(phone);
  if (!rec || rec.phone !== phone || rec.attemptsRemaining <= 0) {
    return mockError("authentication", "otp_invalid", "The one-time code did not match.");
  }
  if (Date.parse(rec.expires_at) < Date.now()) {
    otpChallengesByPhone.delete(phone);
    return mockError("authentication", "otp_invalid", "The one-time code did not match.");
  }
  const submitted = code.trim();
  const digest = otpDigest(secret, phone, rec.challengeId, submitted);
  if (!/^\d{6}$/.test(submitted) || !constantTimeEqual(digest, rec.digest)) {
    rec.attemptsRemaining -= 1;
    return mockError("authentication", "otp_invalid", "The one-time code did not match.");
  }
  otpChallengesByPhone.delete(phone);
  return { status: 200, body: { ok: true } };
}

// ---------------------------------------------------------------------------
// Merchants (restaurant directory — errata S18-E17: deterministic dev-mock
// directory; geo/maps/ranking are activation-scope per spec/18 out-of-scope)
// ---------------------------------------------------------------------------

interface MerchantSeed {
  key: string;
  display_name: string;
  display_name_bn: string;
  area: string;
  area_bn: string;
  cuisine: string;
  cuisine_bn: string;
  active: boolean;
}

const MERCHANT_SEEDS: MerchantSeed[] = [
  {
    key: "kacchi-bhai",
    display_name: "Kacchi Bhai",
    display_name_bn: "কাচ্চি ভাই",
    area: "Dhanmondi",
    area_bn: "ধানমন্ডি",
    cuisine: "Kacchi & Biryani",
    cuisine_bn: "কাচ্চি ও বিরিয়ানি",
    active: true,
  },
  {
    key: "sultans-dine",
    display_name: "Sultan's Dine",
    display_name_bn: "সুলতান'স ডাইন",
    area: "Gulshan",
    area_bn: "গুলশান",
    cuisine: "Kacchi & Polao",
    cuisine_bn: "কাচ্চি ও পোলাও",
    active: true,
  },
  {
    key: "star-kabab",
    display_name: "Star Kabab",
    display_name_bn: "স্টার কাবাব",
    area: "Banani",
    area_bn: "বনানী",
    cuisine: "Kabab & Curry",
    cuisine_bn: "কাবাব ও কারি",
    active: true,
  },
  {
    key: "pizza-roma",
    display_name: "Pizza Roma",
    display_name_bn: "পিৎজা রোমা",
    area: "Uttara",
    area_bn: "উত্তরা",
    cuisine: "Pizza & Pasta",
    cuisine_bn: "পিৎজা ও পাস্তা",
    active: true,
  },
  {
    key: "chillox",
    display_name: "Chillox",
    display_name_bn: "চিলক্স",
    area: "Mirpur",
    area_bn: "মিরপুর",
    cuisine: "Burgers",
    cuisine_bn: "বার্গার",
    active: true,
  },
  {
    key: "haji-biriyani",
    display_name: "Haji Biriyani",
    display_name_bn: "হাজী বিরিয়ানি",
    area: "Old Dhaka",
    area_bn: "পুরান ঢাকা",
    cuisine: "Biriyani",
    cuisine_bn: "বিরিয়ানি",
    active: true,
  },
];

interface MerchantRecord extends MerchantSeed {
  merchant_id: string;
}

const MERCHANTS: MerchantRecord[] = MERCHANT_SEEDS.map((seed) => ({
  ...seed,
  merchant_id: id("mrch", seed.key),
}));

function findMerchant(merchantId: string): MerchantRecord | undefined {
  return MERCHANTS.find((m) => m.merchant_id === merchantId);
}

// ---------------------------------------------------------------------------
// Offers (spec/18 entities; PERCENT_OFF-only pilot per D2)
// ---------------------------------------------------------------------------

interface OfferRecord extends OfferSnapshot {
  cap_recredit_on_refund: boolean;
}

function offer(
  merchantKey: string,
  offerKey: string,
  fields: Omit<OfferRecord, "offer_id" | "merchant_id" | "version">,
): OfferRecord {
  return {
    offer_id: id("off", `${merchantKey}|${offerKey}`),
    merchant_id: id("mrch", merchantKey),
    version: 1,
    ...fields,
  };
}

const VALID_FROM = "2026-06-01T00:00:00Z";
const VALID_UNTIL = "2026-09-30T23:59:59Z";

const OFFERS: OfferRecord[] = [
  // Kacchi Bhai — weekday afternoon 25% (OUTSIDE_WINDOW at the SAT snapshot).
  offer("kacchi-bhai", "weekday-afternoon", {
    kind: "PERCENT_OFF",
    percent_bps: 2500,
    title: "Weekday afternoon 25% off",
    title_bn: "সাপ্তাহিক দুপুরে ২৫% ছাড়",
    windows: [{ days: ["MON", "TUE", "WED", "THU"], start_local: "14:30", end_local: "18:00" }],
    valid_from: VALID_FROM,
    valid_until: VALID_UNTIL,
    min_spend_minor: 50000,
    max_discount_minor: 40000,
    cap_per_day: 20,
    cap_total: 1000,
    cap_per_customer: 2,
    allowed_methods: ["BANGLA_QR", "BKASH", "NAGAD"],
    state: "ACTIVE",
    cap_recredit_on_refund: true,
  }),
  // Kacchi Bhai — weekend off-peak 20% (ELIGIBLE at snapshot).
  offer("kacchi-bhai", "weekend-offpeak", {
    kind: "PERCENT_OFF",
    percent_bps: 2000,
    title: "Weekend off-peak 20% off",
    title_bn: "সাপ্তাহিক ছুটির কম-ভিড়ে ২০% ছাড়",
    windows: [{ days: ["SAT", "SUN"], start_local: "15:00", end_local: "18:30" }],
    valid_from: VALID_FROM,
    valid_until: VALID_UNTIL,
    min_spend_minor: 50000,
    max_discount_minor: 30000,
    cap_per_day: 15,
    cap_total: 500,
    cap_per_customer: 2,
    allowed_methods: ["BANGLA_QR", "BKASH", "NAGAD"],
    state: "ACTIVE",
    cap_recredit_on_refund: true,
  }),
  // Sultan's Dine — weekend all-day 30%, ONE day-slot left at snapshot.
  offer("sultans-dine", "weekend-allday", {
    kind: "PERCENT_OFF",
    percent_bps: 3000,
    title: "Weekend 30% off (all day)",
    title_bn: "ছুটির দিনে সারাদিন ৩০% ছাড়",
    // All-day = explicit window row (errata S18-E8: empty windows refused).
    windows: [{ days: ["SAT", "SUN"], start_local: "00:00", end_local: "23:59" }],
    valid_from: VALID_FROM,
    valid_until: VALID_UNTIL,
    min_spend_minor: 100000,
    max_discount_minor: 40000,
    cap_per_day: 20,
    cap_total: 1500,
    cap_per_customer: 1,
    allowed_methods: ["BANGLA_QR", "BKASH"],
    state: "ACTIVE",
    cap_recredit_on_refund: true,
  }),
  // Star Kabab — lunch & dinner windows, both OUTSIDE_WINDOW at 16:00.
  offer("star-kabab", "lunch", {
    kind: "PERCENT_OFF",
    percent_bps: 1000,
    title: "Lunch 10% off",
    title_bn: "দুপুরের খাবারে ১০% ছাড়",
    windows: [
      { days: ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"], start_local: "11:30", end_local: "15:00" },
    ],
    valid_from: VALID_FROM,
    valid_until: VALID_UNTIL,
    min_spend_minor: 30000,
    max_discount_minor: 20000,
    cap_per_day: 25,
    cap_total: 2000,
    cap_per_customer: 3,
    allowed_methods: ["BANGLA_QR", "NAGAD"],
    state: "ACTIVE",
    cap_recredit_on_refund: true,
  }),
  offer("star-kabab", "dinner", {
    kind: "PERCENT_OFF",
    percent_bps: 1500,
    title: "Dinner 15% off",
    title_bn: "রাতের খাবারে ১৫% ছাড়",
    windows: [
      { days: ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"], start_local: "19:00", end_local: "22:30" },
    ],
    valid_from: VALID_FROM,
    valid_until: VALID_UNTIL,
    min_spend_minor: 30000,
    max_discount_minor: 20000,
    cap_per_day: 25,
    cap_total: 2000,
    cap_per_customer: 3,
    allowed_methods: ["BANGLA_QR", "NAGAD"],
    state: "ACTIVE",
    cap_recredit_on_refund: true,
  }),
  // Pizza Roma — Saturday evening 50% (the researched ceiling), Bangla QR
  // only, cap_per_customer 1: the second attempt demonstrates the NARROWEST
  // exhausted scope refusal (errata S18-E14).
  offer("pizza-roma", "sat-evening", {
    kind: "PERCENT_OFF",
    percent_bps: 5000,
    title: "Saturday evening 50% off",
    title_bn: "শনিবার সন্ধ্যায় ৫০% ছাড়",
    windows: [{ days: ["SAT"], start_local: "16:00", end_local: "19:00" }],
    valid_from: VALID_FROM,
    valid_until: VALID_UNTIL,
    min_spend_minor: 80000,
    max_discount_minor: 60000,
    cap_per_day: 10,
    cap_total: 200,
    cap_per_customer: 1,
    allowed_methods: ["BANGLA_QR"],
    state: "ACTIVE",
    cap_recredit_on_refund: false,
  }),
  // Chillox — merchant-PAUSED offer (not discoverable; FSM 1 PAUSED state).
  offer("chillox", "burger-tuesday", {
    kind: "PERCENT_OFF",
    percent_bps: 2000,
    title: "Burger Tuesday 20% off",
    title_bn: "বার্গার মঙ্গলবারে ২০% ছাড়",
    windows: [{ days: ["TUE"], start_local: "12:00", end_local: "21:00" }],
    valid_from: VALID_FROM,
    valid_until: VALID_UNTIL,
    min_spend_minor: 40000,
    max_discount_minor: 25000,
    cap_per_day: 30,
    cap_total: 800,
    cap_per_customer: 2,
    allowed_methods: ["BANGLA_QR", "BKASH", "NAGAD"],
    state: "PAUSED",
    cap_recredit_on_refund: true,
  }),
  // Haji Biriyani — EXHAUSTED (cap_total reached; FSM 1 EXHAUSTED state).
  offer("haji-biriyani", "launch-special", {
    kind: "PERCENT_OFF",
    percent_bps: 3500,
    title: "Launch special 35% off",
    title_bn: "উদ্বোধনী বিশেষ ৩৫% ছাড়",
    windows: [
      { days: ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"], start_local: "12:00", end_local: "22:00" },
    ],
    valid_from: VALID_FROM,
    valid_until: VALID_UNTIL,
    min_spend_minor: 60000,
    max_discount_minor: 35000,
    cap_per_day: 50,
    cap_total: 300,
    cap_per_customer: 1,
    allowed_methods: ["BANGLA_QR", "BKASH"],
    state: "EXHAUSTED",
    cap_recredit_on_refund: true,
  }),
];

function findOffer(offerId: string): OfferRecord | undefined {
  return OFFERS.find((o) => o.offer_id === offerId);
}

// ---------------------------------------------------------------------------
// Counters (fail-closed; one row per offer/scope/scope_key — spec/18 octr)
// ---------------------------------------------------------------------------

const COUNTERS = new Map<string, CounterState>();

function counterKey(offerId: string, scope: CounterScope, scopeKey: string): string {
  return id("octr", `${offerId}|${scope}|${scopeKey}`);
}

function getCounter(
  offerId: string,
  scope: CounterScope,
  scopeKey: string,
  capLimit: number,
  seedUsed = 0,
): CounterState {
  const key = counterKey(offerId, scope, scopeKey);
  let st = COUNTERS.get(key);
  if (!st) {
    st = { used: seedUsed, cap_limit: capLimit };
    COUNTERS.set(key, st);
  }
  return st;
}

function seedCounter(offerKeyPair: [string, string], scope: CounterScope, scopeKey: string, used: number): void {
  const offerId = id("off", `${offerKeyPair[0]}|${offerKeyPair[1]}`);
  const rec = findOffer(offerId);
  if (!rec) return;
  const cap =
    scope === "day" ? rec.cap_per_day : scope === "total" ? rec.cap_total : rec.cap_per_customer;
  if (cap === null) return;
  getCounter(offerId, scope, scopeKey, cap, used);
}

// Seeded utilization at SNAPSHOT (deterministic).
seedCounter(["kacchi-bhai", "weekend-offpeak"], "day", SNAPSHOT_DHAKA_DATE, 3);
seedCounter(["kacchi-bhai", "weekend-offpeak"], "total", "ALL", 47);
seedCounter(["kacchi-bhai", "weekend-offpeak"], "customer", DEMO_CUSTOMER_ID, 1); // June-7 APPLIED row below
seedCounter(["sultans-dine", "weekend-allday"], "day", SNAPSHOT_DHAKA_DATE, 19); // one slot left today
seedCounter(["sultans-dine", "weekend-allday"], "total", "ALL", 322);
seedCounter(["pizza-roma", "sat-evening"], "day", SNAPSHOT_DHAKA_DATE, 2);
seedCounter(["pizza-roma", "sat-evening"], "total", "ALL", 31);
seedCounter(["star-kabab", "lunch"], "day", SNAPSHOT_DHAKA_DATE, 6);
seedCounter(["star-kabab", "lunch"], "total", "ALL", 240);

function advisoryCounters(offerRec: OfferRecord, customerId: string | null) {
  const out: { dayUsed?: number; totalUsed?: number; customerUsed?: number } = {};
  if (offerRec.cap_per_day !== null) {
    out.dayUsed = getCounter(offerRec.offer_id, "day", SNAPSHOT_DHAKA_DATE, offerRec.cap_per_day).used;
  }
  if (offerRec.cap_total !== null) {
    out.totalUsed = getCounter(offerRec.offer_id, "total", "ALL", offerRec.cap_total).used;
  }
  if (customerId !== null && offerRec.cap_per_customer !== null) {
    out.customerUsed = getCounter(
      offerRec.offer_id,
      "customer",
      customerId,
      offerRec.cap_per_customer,
    ).used;
  }
  return out;
}

// ---------------------------------------------------------------------------
// Payment intents + redemptions (FSM 2 — refusal-first)
// ---------------------------------------------------------------------------

interface IntentRecord extends PaymentIntentView {
  offer_version: number | null; // internal pin (spec/18: reservations pin the version) — never serialized
}

const INTENTS = new Map<string, IntentRecord>();

/** API projection — strips internal-only fields from the stored record. */
function intentView(rec: IntentRecord): PaymentIntentView {
  const { offer_version: _internal, ...view } = rec;
  return { ...view, offer: rec.offer === null ? null : { ...rec.offer } };
}

function seedIntent(
  key: string,
  merchantKey: string,
  offerKeyPair: [string, string] | null,
  fields: {
    gross: number;
    discount: number;
    status: IntentRecord["status"];
    redemption: RedemptionState | null;
    method: CheckoutMethod;
    created_at: string;
  },
): void {
  const merchant = MERCHANTS.find((m) => m.key === merchantKey);
  if (!merchant) return;
  const offerId = offerKeyPair ? id("off", `${offerKeyPair[0]}|${offerKeyPair[1]}`) : null;
  const offerRec = offerId ? findOffer(offerId) : null;
  const pi = id("pi", key);
  INTENTS.set(pi, {
    payment_intent_id: pi,
    merchant_id: merchant.merchant_id,
    merchant_display_name: merchant.display_name,
    merchant_display_name_bn: merchant.display_name_bn,
    customer_id: DEMO_CUSTOMER_ID,
    amount_minor: fields.gross - fields.discount,
    currency: "BDT",
    method: fields.method,
    status: fields.status,
    offer:
      offerId !== null
        ? { offer_id: offerId, gross_amount_minor: fields.gross, discount_minor: fields.discount }
        : null,
    redemption_state: fields.redemption,
    created_at: fields.created_at,
    expires_at: fields.created_at,
    offer_version: offerRec ? offerRec.version : null,
  });
}

// Redemption history seed (terminal + applied states across the FSM).
// 2026-06-07 was a Sunday, 2026-05-30 and 2026-06-06 Saturdays, 2026-06-04 a
// Thursday — each inside its offer's window at the recorded instant.
seedIntent("hist-applied", "kacchi-bhai", ["kacchi-bhai", "weekend-offpeak"], {
  gross: 120000,
  discount: 24000, // 2000 bps, floor; below the 30000 cap
  status: "SUCCEEDED",
  redemption: "APPLIED",
  method: "BANGLA_QR",
  created_at: "2026-06-07T10:10:00Z",
});
seedIntent("hist-settled", "sultans-dine", ["sultans-dine", "weekend-allday"], {
  gross: 250000,
  discount: 40000, // 3000 bps = 75000, CAPPED at max_discount 40000
  status: "SUCCEEDED",
  redemption: "SETTLED",
  method: "BKASH",
  created_at: "2026-05-30T07:30:00Z",
});
seedIntent("hist-released", "pizza-roma", ["pizza-roma", "sat-evening"], {
  gross: 90000,
  discount: 45000, // 5000 bps; below the 60000 cap
  status: "EXPIRED",
  redemption: "RELEASED", // not paid in 30 min; slot re-credited
  method: "BANGLA_QR",
  created_at: "2026-06-06T10:20:00Z",
});
seedIntent("hist-reversed", "kacchi-bhai", ["kacchi-bhai", "weekday-afternoon"], {
  gross: 100000,
  discount: 25000, // 2500 bps; below the 40000 cap
  status: "SUCCEEDED",
  redemption: "REVERSED", // full refund; cap_recredit_on_refund honored
  method: "NAGAD",
  created_at: "2026-06-04T09:00:00Z",
});

// ---------------------------------------------------------------------------
// Directory + discovery
// ---------------------------------------------------------------------------

function eligibleAtSnapshot(offerRec: OfferRecord, merchant: MerchantRecord): boolean {
  try {
    const res = evaluateEligibility(offerRec, {
      merchantActive: merchant.active,
      nowUtc: SNAPSHOT_AT,
      counters: advisoryCounters(offerRec, null),
    });
    return res.eligible;
  } catch {
    return false; // fail closed: an unevaluable offer is not discoverable
  }
}

export function listMerchantsMock(params: { area: string; q: string }): DinerMerchant[] {
  return MERCHANTS.filter((m) => {
    if (params.area && m.area !== params.area) return false;
    if (params.q) {
      const q = params.q.toLowerCase();
      if (
        !m.display_name.toLowerCase().includes(q) &&
        !m.display_name_bn.includes(params.q) &&
        !m.area.toLowerCase().includes(q) &&
        !m.area_bn.includes(params.q)
      ) {
        return false;
      }
    }
    return true;
  }).map((m) => {
    const live = OFFERS.filter((o) => o.merchant_id === m.merchant_id && eligibleAtSnapshot(o, m));
    const best = live.reduce<number | null>(
      (acc, o) => (o.percent_bps !== null && (acc === null || o.percent_bps > acc) ? o.percent_bps : acc),
      null,
    );
    const tags = tagsForWindows(live.flatMap((o) => o.windows));
    return {
      merchant_id: m.merchant_id,
      display_name: m.display_name,
      display_name_bn: m.display_name_bn,
      area: m.area,
      area_bn: m.area_bn,
      cuisine: m.cuisine,
      cuisine_bn: m.cuisine_bn,
      live_offer_count: live.length,
      best_percent_bps: best,
      window_tags: tags,
    };
  });
}

export function getMerchantMock(merchantId: string): DinerMerchant | null {
  const rows = listMerchantsMock({ area: "", q: "" });
  return rows.find((m) => m.merchant_id === merchantId) ?? null;
}

/** GET /v1/offers/eligible — mirrors the kernel service shape: PII-free,
 * customer checks EXCLUDED (per-customer cap enforced only at reservation,
 * spec/18 §API surface). */
export function eligibleOffersMock(params: {
  merchant_id: string;
  method: string;
  example_amount_minor: number | null;
  at: string;
}): MockResult<{ data: EligibleOffer[] }> {
  const merchant = findMerchant(params.merchant_id);
  if (!merchant) {
    return mockError("not_found", "merchant_not_found", "No such merchant.");
  }
  const when = params.at || SNAPSHOT_AT;
  if (!Number.isFinite(Date.parse(when))) {
    return mockError("invalid_request", "validation_failed", "at must be an RFC3339 timestamp.");
  }
  const out: EligibleOffer[] = [];
  for (const o of OFFERS) {
    if (o.merchant_id !== merchant.merchant_id || o.state !== "ACTIVE") continue;
    let res;
    try {
      res = evaluateEligibility(o, {
        merchantActive: merchant.active,
        nowUtc: when,
        method: params.method || null,
        counters: advisoryCounters(o, null),
      });
    } catch {
      continue; // fail closed
    }
    if (!res.eligible) continue;
    const entry: EligibleOffer = {
      offer_id: o.offer_id,
      kind: o.kind,
      title: o.title,
      title_bn: o.title_bn,
      percent_bps: o.percent_bps,
      windows: o.windows,
      min_spend_minor: o.min_spend_minor,
      max_discount_minor: o.max_discount_minor,
      allowed_methods: o.allowed_methods as CheckoutMethod[],
      valid_until: o.valid_until,
    };
    if (params.example_amount_minor !== null) {
      try {
        const ex = evaluateEligibility(o, {
          merchantActive: merchant.active,
          nowUtc: when,
          grossAmountMinor: params.example_amount_minor,
          method: params.method || null,
          counters: advisoryCounters(o, null),
        });
        if (ex.eligible) {
          entry.example_amount_minor = params.example_amount_minor;
          entry.example_discount_minor = ex.discount_minor;
        }
      } catch {
        // example preview unavailable; the row itself stays discoverable
      }
    }
    out.push(entry);
  }
  return { status: 200, body: { data: out } };
}

// ---------------------------------------------------------------------------
// Reservation (spec/18 extension to POST /v1/payment-intents)
// ---------------------------------------------------------------------------

const CAP_REASONS: ReasonCode[] = [
  "CAP_CUSTOMER_EXHAUSTED",
  "CAP_DAY_EXHAUSTED",
  "CAP_TOTAL_EXHAUSTED",
];

// Narrowest-first naming (errata S18-E14): customer > day > total.
function narrowestCapCode(reasons: ReasonCode[]): string {
  if (reasons.includes("CAP_CUSTOMER_EXHAUSTED")) return "offer_cap_exhausted_customer";
  if (reasons.includes("CAP_DAY_EXHAUSTED")) return "offer_cap_exhausted_day";
  return "offer_cap_exhausted_total";
}

const REPLAYS = new Map<string, MockResult<PaymentIntentView>>();

export function createOfferIntentMock(params: {
  authenticated: boolean;
  idempotencyKey: string;
  merchant_id: string;
  offer_id: string | null;
  gross_amount_minor: unknown;
  method: string;
}): MockResult<PaymentIntentView> {
  // Replay (client Idempotency-Key): stored response returns verbatim;
  // counters are NOT touched again.
  const replayKey = `${params.merchant_id}|${params.idempotencyKey}`;
  const stored = REPLAYS.get(replayKey);
  if (stored) return stored;

  const merchant = findMerchant(params.merchant_id);
  if (!merchant) {
    return mockError("not_found", "merchant_not_found", "No such merchant.");
  }
  if (!["BANGLA_QR", "BKASH", "NAGAD"].includes(params.method)) {
    return mockError("invalid_request", "validation_failed", "Unknown payment method.");
  }
  const method = params.method as CheckoutMethod;

  if (params.offer_id === null) {
    // Zero-offer invariant: this surface never exercises it, but the seam
    // behavior is pinned — absent offer_id means plain spec/02 creation.
    return mockError(
      "invalid_request",
      "validation_failed",
      "The diner surface creates offer redemptions; offer_id is required here.",
    );
  }

  // D1: redemption REQUIRES an authenticated customer (guests get 401).
  if (!params.authenticated) {
    return mockError(
      "authentication",
      "offer_requires_login",
      "Offer redemption requires diner-app login; guests cannot redeem.",
    );
  }

  const offerRec = findOffer(params.offer_id);
  if (!offerRec || offerRec.merchant_id !== merchant.merchant_id) {
    return mockError("not_found", "offer_not_found", "No such offer at this merchant.");
  }

  const gross = params.gross_amount_minor;
  if (typeof gross !== "number" || !Number.isSafeInteger(gross)) {
    return mockError(
      "invalid_request",
      "gross_amount_required",
      "gross_amount_minor (integer paisa) is required when offer_id is present.",
    );
  }
  if (gross <= 0) {
    return mockError("invalid_request", "amount_not_positive", "gross_amount_minor must be > 0 paisa.");
  }

  const customerId = DEMO_CUSTOMER_ID;
  let evaluation;
  try {
    evaluation = evaluateEligibility(offerRec, {
      merchantActive: merchant.active,
      nowUtc: SNAPSHOT_AT,
      grossAmountMinor: gross,
      method,
      counters: advisoryCounters(offerRec, customerId),
    });
  } catch (err) {
    if (err instanceof BogoNotEnabledError) {
      return mockError(
        "invalid_request",
        "bogo_not_yet_enabled",
        "BOGO offers are not yet enabled (pilot is PERCENT_OFF-only).",
      );
    }
    if (err instanceof DiscountEqualsGrossError) {
      return mockError(
        "invalid_request",
        "discount_equals_gross",
        "The discount may not equal the gross amount; the rail must move at least 1 paisa.",
      );
    }
    // Evaluator exception -> ineligible (fail-closed, spec/18 failure table).
    return mockError("invalid_request", "offer_not_eligible", "OFFER_EVAL_ERROR");
  }

  if (!evaluation.eligible) {
    const capOnly = evaluation.reasons.every((r) => CAP_REASONS.includes(r));
    if (capOnly) {
      return mockError(
        "limit_exceeded",
        narrowestCapCode(evaluation.reasons),
        "The offer's redemption cap is exhausted for this scope.",
      );
    }
    return mockError(
      "invalid_request",
      "offer_not_eligible",
      `offer_not_eligible: ${evaluation.reasons.join(", ")}`,
    );
  }

  // Atomic, fail-closed reservation — narrowest scope first (S18-E14), in
  // the same "transaction" as the intent + redemption rows.
  const scopes: { scope: CounterScope; counter: CounterState }[] = [];
  if (offerRec.cap_per_customer !== null) {
    scopes.push({
      scope: "customer",
      counter: getCounter(offerRec.offer_id, "customer", customerId, offerRec.cap_per_customer),
    });
  }
  if (offerRec.cap_per_day !== null) {
    scopes.push({
      scope: "day",
      counter: getCounter(offerRec.offer_id, "day", SNAPSHOT_DHAKA_DATE, offerRec.cap_per_day),
    });
  }
  if (offerRec.cap_total !== null) {
    scopes.push({
      scope: "total",
      counter: getCounter(offerRec.offer_id, "total", "ALL", offerRec.cap_total),
    });
  }
  const outcome = reserveCounters(scopes);
  if (!outcome.ok) {
    return mockError(
      "limit_exceeded",
      `offer_cap_exhausted_${outcome.exhausted_scope}`,
      "The offer's redemption cap is exhausted for this scope.",
    );
  }

  const discount = evaluation.discount_minor;
  const net = gross - discount;
  const pi = id("pi", `reserve|${offerRec.offer_id}|${params.idempotencyKey}`);
  const record: IntentRecord = {
    payment_intent_id: pi,
    merchant_id: merchant.merchant_id,
    merchant_display_name: merchant.display_name,
    merchant_display_name_bn: merchant.display_name_bn,
    customer_id: customerId,
    amount_minor: net, // immutable hereafter; no mid-flow repricing, ever
    currency: "BDT",
    method,
    status: "CREATED",
    offer: { offer_id: offerRec.offer_id, gross_amount_minor: gross, discount_minor: discount },
    redemption_state: "RESERVED",
    // Wall-clock create/expiry for demo honesty; offer-window eligibility still
    // evaluates against SNAPSHOT_AT fixtures above.
    created_at: new Date().toISOString().replace(/\.\d{3}Z$/, "Z"),
    expires_at: new Date(Date.now() + RESERVATION_TTL_MS).toISOString().replace(/\.\d{3}Z$/, "Z"),
    offer_version: offerRec.version,
  };
  INTENTS.set(pi, record);
  const result: MockResult<PaymentIntentView> = { status: 201, body: intentView(record) };
  REPLAYS.set(replayKey, result);
  return result;
}

export function getIntentMock(paymentIntentId: string): MockResult<PaymentIntentView> {
  const rec = INTENTS.get(paymentIntentId);
  if (!rec || rec.customer_id !== DEMO_CUSTOMER_ID) {
    return mockError("not_found", "payment_intent_not_found", "No such payment intent.");
  }
  return { status: 200, body: intentView(rec) };
}

/** Seed history ages (ms before wall now) — keeps "My usage" recent on long-lived demos. */
const HIST_AGE_MS: Record<string, number> = {
  [id("pi", "hist-applied")]: 2 * 86_400_000,
  [id("pi", "hist-released")]: 3 * 86_400_000,
  [id("pi", "hist-reversed")]: 5 * 86_400_000,
  [id("pi", "hist-settled")]: 10 * 86_400_000,
};

export function listMyIntentsMock(): PaymentIntentView[] {
  const now = Date.now();
  return [...INTENTS.values()]
    .filter((r) => r.customer_id === DEMO_CUSTOMER_ID)
    .map((r) => {
      const view = intentView(r);
      const age = HIST_AGE_MS[r.payment_intent_id];
      if (age == null) return view;
      const at = new Date(now - age).toISOString().replace(/\.\d{3}Z$/, "Z");
      return { ...view, created_at: at, expires_at: at };
    })
    .sort((a, b) => (a.created_at < b.created_at ? 1 : -1));
}

/** POST /v1/payment-intents/{id}/confirm — the simulated rail. Refusal-first:
 * only CREATED confirms; success drives RESERVED -> APPLIED (FSM 2,
 * consuming payment_intent.succeeded). */
export function confirmIntentMock(paymentIntentId: string): MockResult<PaymentIntentView> {
  const rec = INTENTS.get(paymentIntentId);
  if (!rec || rec.customer_id !== DEMO_CUSTOMER_ID) {
    return mockError("not_found", "payment_intent_not_found", "No such payment intent.");
  }
  if (rec.status !== "CREATED") {
    return mockError(
      "conflict",
      "invalid_state_transition",
      `confirm is not a legal trigger from status ${rec.status} (refusal-first).`,
    );
  }
  rec.status = "SUCCEEDED";
  if (rec.offer !== null && rec.redemption_state === "RESERVED") {
    rec.redemption_state = "APPLIED";
  }
  return { status: 200, body: intentView(rec) };
}

// ---------------------------------------------------------------------------
// Dynamic QR (spec/13 handoff — net amount only; offers REQUIRE dynamic QR)
// ---------------------------------------------------------------------------

function tlv(tag: string, value: string): string {
  return `${tag}${String(value.length).padStart(2, "0")}${value}`;
}

export function issueDynamicQrMock(paymentIntentId: string): MockResult<DynamicQr> {
  const rec = INTENTS.get(paymentIntentId);
  if (!rec || rec.customer_id !== DEMO_CUSTOMER_ID) {
    return mockError("not_found", "payment_intent_not_found", "No such payment intent.");
  }
  if (rec.status !== "CREATED") {
    return mockError(
      "conflict",
      "invalid_state_transition",
      "A dynamic QR can only be issued for an intent awaiting payment.",
    );
  }
  const taka = Math.floor(rec.amount_minor / 100);
  const paisa = String(rec.amount_minor % 100).padStart(2, "0");
  const amount = `${taka}.${paisa}`;
  const merchantName = rec.merchant_display_name.slice(0, 25);
  // Deterministic spec/13-shaped dynamic (point-of-initiation 12) payload.
  const payload =
    tlv("00", "01") +
    tlv("01", "12") +
    tlv("26", tlv("00", "bd.bdpay") + tlv("01", detHex(`mqr|${rec.merchant_id}`, 16))) +
    tlv("52", "5812") +
    tlv("53", "050") +
    tlv("54", amount) +
    tlv("58", "BD") +
    tlv("59", merchantName) +
    tlv("60", "DHAKA") +
    tlv("62", tlv("01", rec.payment_intent_id.slice(3, 19))) +
    tlv("63", detHex(`crc|${rec.payment_intent_id}`, 4).toUpperCase());
  // Demo-facing QR expiry: near-future wall clock (not the frozen SNAPSHOT_AT)
  // so the diner walk never shows a stale 2026-06-13 date. Intent TTL still
  // uses RESERVED_EXPIRES_AT for deterministic offer/FSM fixtures.
  const qrExpiresAt = new Date(Date.now() + RESERVATION_TTL_MS).toISOString().replace(/\.\d{3}Z$/, "Z");
  const body: DynamicQr = {
    payload_id: id("qrp", `dyn|${rec.payment_intent_id}`),
    payment_intent_id: rec.payment_intent_id,
    payload,
    payload_hash: detHex(`hash|${payload}`, 64),
    amount_minor: rec.amount_minor,
    expires_at: qrExpiresAt,
  };
  return { status: 201, body };
}

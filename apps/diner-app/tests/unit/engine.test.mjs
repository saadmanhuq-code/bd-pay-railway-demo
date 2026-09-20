// Pinning tests for the shared offer engine (spec/18 §Eligibility +
// §Cap enforcement; errata S18-E7/S18-E14/S18-E20). Run via `npm test`
// (pretest compiles src/lib/offers/engine.ts + src/lib/format.ts with tsc).

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  BogoNotEnabledError,
  DiscountEqualsGrossError,
  computeDiscountMinor,
  dhakaParts,
  evaluateEligibility,
  recreditCounter,
  reserveCounters,
  windowMatches,
} from "../../.test-dist/lib/offers/engine.js";

// ---------------------------------------------------------------------------
// Discount arithmetic — integer floor, customer's disfavor by <= 1 paisa
// ---------------------------------------------------------------------------

test("discount floors (never rounds up)", () => {
  // 99,999 paisa at 25.00% = 24,999.75 -> 24,999 (floor).
  assert.equal(computeDiscountMinor(99999, 2500, null), 24999);
  // Exact division stays exact.
  assert.equal(computeDiscountMinor(120000, 2500, null), 30000);
});

test("max_discount_minor caps the floor result", () => {
  // 250,000 at 30% = 75,000, capped at 40,000 (the Sultan's seed shape).
  assert.equal(computeDiscountMinor(250000, 3000, 40000), 40000);
  // Below the cap the cap is inert.
  assert.equal(computeDiscountMinor(100000, 3000, 40000), 30000);
});

test("discount is never negative and never >= gross", () => {
  assert.equal(computeDiscountMinor(1, 500, null), 0); // floor(0.05) = 0
  // 100% would equal gross: the rail must move >= 1 paisa (typed refusal).
  assert.throws(() => computeDiscountMinor(50000, 10000, null), DiscountEqualsGrossError);
  // Cap equal to gross likewise refused at 10000 bps.
  assert.throws(() => computeDiscountMinor(50000, 10000, 50000), DiscountEqualsGrossError);
});

test("float and non-positive inputs are refused (integer paisa only)", () => {
  assert.throws(() => computeDiscountMinor(100.5, 2500, null));
  assert.throws(() => computeDiscountMinor(0, 2500, null));
  assert.throws(() => computeDiscountMinor(-100, 2500, null));
});

// ---------------------------------------------------------------------------
// Asia/Dhaka windows (fixed +06:00; start inclusive, end EXCLUSIVE)
// ---------------------------------------------------------------------------

test("2026-06-13T10:00:00Z is Saturday 16:00 Asia/Dhaka", () => {
  const p = dhakaParts("2026-06-13T10:00:00Z");
  assert.equal(p.day, "SAT");
  assert.equal(p.hhmm, "16:00");
  assert.equal(p.dateStr, "2026-06-13");
});

test("window start is inclusive, end is exclusive", () => {
  const windows = [{ days: ["SAT"], start_local: "16:00", end_local: "19:00" }];
  assert.equal(windowMatches(windows, "2026-06-13T10:00:00Z"), true); // 16:00 ON start
  assert.equal(windowMatches(windows, "2026-06-13T12:59:00Z"), true); // 18:59
  assert.equal(windowMatches(windows, "2026-06-13T13:00:00Z"), false); // 19:00 ON end -> out
  assert.equal(windowMatches(windows, "2026-06-13T09:59:00Z"), false); // 15:59
});

test("Dhaka offset rolls the calendar day across midnight", () => {
  // 19:30Z Saturday = 01:30 Asia/Dhaka SUNDAY.
  const p = dhakaParts("2026-06-13T19:30:00Z");
  assert.equal(p.day, "SUN");
  assert.equal(p.dateStr, "2026-06-14");
});

// ---------------------------------------------------------------------------
// Eligibility evaluation — ALL failing reasons returned, not just first
// ---------------------------------------------------------------------------

const BASE_OFFER = {
  offer_id: "off_test",
  merchant_id: "mrch_test",
  version: 1,
  kind: "PERCENT_OFF",
  percent_bps: 2500,
  title: "t",
  title_bn: "টেস্ট",
  windows: [{ days: ["SAT"], start_local: "15:00", end_local: "18:00" }],
  valid_from: "2026-06-01T00:00:00Z",
  valid_until: "2026-09-30T23:59:59Z",
  min_spend_minor: 50000,
  max_discount_minor: 40000,
  cap_per_day: 10,
  cap_total: 100,
  cap_per_customer: 2,
  allowed_methods: ["BANGLA_QR", "BKASH"],
  state: "ACTIVE",
};

test("eligible path computes the discount", () => {
  const res = evaluateEligibility(BASE_OFFER, {
    merchantActive: true,
    nowUtc: "2026-06-13T10:00:00Z",
    grossAmountMinor: 120000,
    method: "BANGLA_QR",
    counters: { dayUsed: 3, totalUsed: 40, customerUsed: 1 },
  });
  assert.equal(res.eligible, true);
  assert.equal(res.discount_minor, 30000);
  assert.deepEqual(res.reasons, []);
});

test("all failing reasons accumulate", () => {
  const res = evaluateEligibility(
    { ...BASE_OFFER, state: "PAUSED" },
    {
      merchantActive: false,
      nowUtc: "2026-06-13T05:00:00Z", // 11:00 Dhaka — outside window
      grossAmountMinor: 10000, // below min spend
      method: "NAGAD", // not allowed
    },
  );
  assert.equal(res.eligible, false);
  assert.equal(res.discount_minor, 0);
  for (const expected of [
    "OFFER_NOT_ACTIVE",
    "MERCHANT_NOT_ACTIVE",
    "OUTSIDE_WINDOW",
    "MIN_SPEND_NOT_MET",
    "METHOD_NOT_ALLOWED",
  ]) {
    assert.ok(res.reasons.includes(expected), `missing ${expected}`);
  }
});

test("validity bounds produce BEFORE/AFTER reasons", () => {
  const before = evaluateEligibility(BASE_OFFER, {
    merchantActive: true,
    nowUtc: "2026-05-30T10:00:00Z",
  });
  assert.ok(before.reasons.includes("BEFORE_VALIDITY"));
  const after = evaluateEligibility(BASE_OFFER, {
    merchantActive: true,
    nowUtc: "2026-10-03T10:00:00Z",
  });
  assert.ok(after.reasons.includes("AFTER_VALIDITY"));
});

test("advisory counters surface cap reasons (fast-fail only)", () => {
  const res = evaluateEligibility(BASE_OFFER, {
    merchantActive: true,
    nowUtc: "2026-06-13T10:00:00Z",
    grossAmountMinor: 120000,
    method: "BANGLA_QR",
    counters: { dayUsed: 10, totalUsed: 100, customerUsed: 2 },
  });
  assert.equal(res.eligible, false);
  assert.deepEqual(
    [...res.reasons].sort(),
    ["CAP_CUSTOMER_EXHAUSTED", "CAP_DAY_EXHAUSTED", "CAP_TOTAL_EXHAUSTED"],
  );
});

test("BOGO is refused with the typed D2 error at evaluator depth", () => {
  assert.throws(
    () =>
      evaluateEligibility(
        { ...BASE_OFFER, kind: "BOGO", percent_bps: null },
        { merchantActive: true, nowUtc: "2026-06-13T10:00:00Z" },
      ),
    BogoNotEnabledError,
  );
});

// ---------------------------------------------------------------------------
// Fail-closed counters (errata S18-E14: narrowest scope first)
// ---------------------------------------------------------------------------

test("reservation consumes every scope atomically", () => {
  const customer = { used: 0, cap_limit: 2 };
  const day = { used: 3, cap_limit: 10 };
  const total = { used: 40, cap_limit: 100 };
  const out = reserveCounters([
    { scope: "total", counter: total },
    { scope: "customer", counter: customer },
    { scope: "day", counter: day },
  ]);
  assert.deepEqual(out, { ok: true });
  assert.equal(customer.used, 1);
  assert.equal(day.used, 4);
  assert.equal(total.used, 41);
});

test("refusal names the NARROWEST exhausted scope and consumes nothing", () => {
  const customer = { used: 2, cap_limit: 2 }; // exhausted
  const day = { used: 10, cap_limit: 10 }; // also exhausted
  const total = { used: 100, cap_limit: 100 }; // also exhausted
  const out = reserveCounters([
    { scope: "total", counter: total },
    { scope: "day", counter: day },
    { scope: "customer", counter: customer },
  ]);
  assert.deepEqual(out, { ok: false, exhausted_scope: "customer" });
  assert.equal(customer.used, 2);
  assert.equal(day.used, 10);
  assert.equal(total.used, 100);
});

test("a wider exhausted scope refuses without touching narrower scopes", () => {
  const customer = { used: 0, cap_limit: 2 };
  const day = { used: 10, cap_limit: 10 }; // exhausted
  const out = reserveCounters([
    { scope: "customer", counter: customer },
    { scope: "day", counter: day },
  ]);
  assert.deepEqual(out, { ok: false, exhausted_scope: "day" });
  assert.equal(customer.used, 0); // all-or-nothing
});

test("malformed counter state fails CLOSED", () => {
  const bad = { used: 0, cap_limit: 0 };
  const out = reserveCounters([{ scope: "total", counter: bad }]);
  assert.equal(out.ok, false);
});

test("re-credit floors at zero (SET used = used - 1 WHERE used > 0)", () => {
  const c = { used: 1, cap_limit: 5 };
  recreditCounter(c);
  assert.equal(c.used, 0);
  recreditCounter(c);
  assert.equal(c.used, 0);
});

test("counter invariant 0 <= used <= cap_limit holds across interleavings", () => {
  const counter = { used: 0, cap_limit: 10 };
  let granted = 0;
  for (let i = 0; i < 64; i++) {
    const out = reserveCounters([{ scope: "total", counter }]);
    if (out.ok) granted += 1;
    assert.ok(counter.used >= 0 && counter.used <= counter.cap_limit);
  }
  assert.equal(granted, 10); // exactly cap_limit succeed; zero over-redemption
  assert.equal(counter.used, 10);
});

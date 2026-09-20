// F10 — the History screen must never invent a restaurant settlement.
//
// Runs against `src/lib/live/history.ts` straight from source (type-stripped,
// no build step), so it is runnable from the repository root.
//
// RED ON REVERT: restore the `status === "SUCCEEDED" ? "SETTLED" : null`
// fallback and the missing-evidence cases below report SETTLED again.

import { test } from "node:test";
import assert from "node:assert/strict";

import { loadLibModule } from "./live-route-harness.mjs";

const { enrichIntentView, normalizeRedemptionState } = await loadLibModule("lib/live/history");

const BASE_INTENT = {
  payment_intent_id: "pi_test",
  merchant_id: "mrch_test",
  merchant_display_name: "Acme",
  merchant_display_name_bn: "অ্যাকমে",
  customer_id: "cust_test",
  amount_minor: 125000,
  currency: "BDT",
  method: "BKASH",
  status: "SUCCEEDED",
  offer: null,
  redemption_state: null,
  created_at: "2026-06-23T00:00:00Z",
  expires_at: "2026-06-23T01:00:00Z",
};

test("pre-settlement payment success never renders SETTLED", () => {
  // The backend FSM reaches APPLIED on payment success; SETTLED needs a later
  // settlement confirmation.
  assert.equal(enrichIntentView({ ...BASE_INTENT, redemption_state: "APPLIED" }).redemption_state, "APPLIED");
  assert.equal(normalizeRedemptionState("APPLIED", "SUCCEEDED"), "APPLIED");
});

test("confirmed settlement still renders SETTLED", () => {
  assert.equal(enrichIntentView({ ...BASE_INTENT, redemption_state: "SETTLED" }).redemption_state, "SETTLED");
  assert.equal(normalizeRedemptionState("SETTLED", "SUCCEEDED"), "SETTLED");
});

test("missing or unrecognised evidence renders unknown, not SETTLED", () => {
  for (const value of [null, undefined, "", "UNKNOWN", 42, {}]) {
    assert.equal(
      normalizeRedemptionState(value, "SUCCEEDED"),
      null,
      `missing evidence (${JSON.stringify(value)}) must not become SETTLED`,
    );
  }
  assert.equal(enrichIntentView({ ...BASE_INTENT, redemption_state: null }).redemption_state, null);
  assert.equal(enrichIntentView({ ...BASE_INTENT, redemption_state: "UNKNOWN" }).redemption_state, null);
});

test("refunds and reversals stay consistent", () => {
  for (const status of ["CREATED", "FAILED", "EXPIRED", "PARTIALLY_REFUNDED", "REFUNDED"]) {
    assert.equal(normalizeRedemptionState(null, status), null);
  }
  assert.equal(normalizeRedemptionState("REVERSED", "REFUNDED"), "REVERSED");
  assert.equal(normalizeRedemptionState("RELEASED", "FAILED"), "RELEASED");
});

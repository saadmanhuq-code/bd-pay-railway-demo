// Pinning tests for the live adapter -> History screen normalization.

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  enrichIntentView,
  normalizeRedemptionState,
} from "../../.test-dist/lib/live/history.js";

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

test("normalizeRedemptionState passes known states through unchanged", () => {
  for (const state of ["RESERVED", "APPLIED", "RELEASED", "REVERSED", "SETTLED"]) {
    assert.equal(normalizeRedemptionState(state, "SUCCEEDED"), state);
  }
});

// F10 CORRECTION: these three cases previously asserted that a SUCCEEDED
// payment with no (or an unrecognised) redemption state becomes SETTLED. That
// assertion pinned the defect — payment success is not settlement evidence, and
// the History screen renders SETTLED as "settled with the restaurant" while the
// backend FSM only reaches SETTLED on a later settlement confirmation.
// Missing evidence must render as unknown.
test("normalizeRedemptionState reports unknown when settlement evidence is missing", () => {
  assert.equal(normalizeRedemptionState(null, "SUCCEEDED"), null);
  assert.equal(normalizeRedemptionState(undefined, "SUCCEEDED"), null);
  assert.equal(normalizeRedemptionState("UNKNOWN", "SUCCEEDED"), null);
});

test("normalizeRedemptionState still reports a confirmed settlement", () => {
  assert.equal(normalizeRedemptionState("SETTLED", "SUCCEEDED"), "SETTLED");
});

test("normalizeRedemptionState reports pre-settlement success as APPLIED, not SETTLED", () => {
  assert.equal(normalizeRedemptionState("APPLIED", "SUCCEEDED"), "APPLIED");
});

test("normalizeRedemptionState leaves non-SUCCEEDED intents as null", () => {
  for (const status of ["CREATED", "FAILED", "EXPIRED", "PARTIALLY_REFUNDED", "REFUNDED"]) {
    assert.equal(normalizeRedemptionState(null, status), null);
  }
});

test("enrichIntentView preserves fully populated rows", () => {
  const out = enrichIntentView({ ...BASE_INTENT, redemption_state: "SETTLED" });
  assert.equal(out.merchant_display_name, "Acme");
  assert.equal(out.customer_id, "cust_test");
  assert.equal(out.currency, "BDT");
  assert.equal(out.redemption_state, "SETTLED");
});

test("enrichIntentView fills diner display defaults for sparse gateway rows", () => {
  const sparse = {
    ...BASE_INTENT,
    merchant_display_name: undefined,
    merchant_display_name_bn: undefined,
    customer_id: undefined,
    currency: undefined,
  };
  const out = enrichIntentView(sparse);
  assert.equal(out.merchant_display_name, "BDPay Demo Merchant");
  assert.equal(out.merchant_display_name_bn, "বিডিপে ডেমো মার্চেন্ট");
  assert.equal(out.customer_id, "cust_demo");
  assert.equal(out.currency, "BDT");
});

test("enrichIntentView applies custom defaults", () => {
  const out = enrichIntentView(
    { ...BASE_INTENT, merchant_display_name: undefined, customer_id: undefined },
    {
      defaultMerchantDisplayName: "Custom Merchant",
      defaultCustomerId: "cust_custom",
    },
  );
  assert.equal(out.merchant_display_name, "Custom Merchant");
  assert.equal(out.customer_id, "cust_custom");
});

// F10 CORRECTION: was "derives SETTLED for SUCCEEDED intents without
// redemption_state" — the derivation itself is the defect.
test("enrichIntentView never invents settlement from payment success", () => {
  const out = enrichIntentView({ ...BASE_INTENT, redemption_state: null });
  assert.equal(out.redemption_state, null);
  const applied = enrichIntentView({ ...BASE_INTENT, redemption_state: "APPLIED" });
  assert.equal(applied.redemption_state, "APPLIED");
  const settled = enrichIntentView({ ...BASE_INTENT, redemption_state: "SETTLED" });
  assert.equal(settled.redemption_state, "SETTLED");
});

test("enrichIntentView keeps null redemption_state for non-SUCCEEDED statuses", () => {
  for (const status of ["CREATED", "FAILED", "EXPIRED"]) {
    const out = enrichIntentView({ ...BASE_INTENT, status, redemption_state: null });
    assert.equal(out.redemption_state, null);
  }
});

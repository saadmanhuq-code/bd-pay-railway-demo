// F09 — normal diner checkout through the live BFF, executed with no network
// (the E36 reviewer harness, re-runnable here).
//
// RED ON REVERT: `v1/payment-intents`, `v1/qr-codes/dynamic` and
// `v1/payment-intents/{id}/confirm` — the three POSTs the merchant page calls
// on reserve/confirm — fell through to 404, so a signed-in diner could not
// complete a real payment (review F09 / evidence E34, E36).
//
// Findings 340/356 (diner-checkout-customer-principal-rejected; dynamic QR
// checkout lacks the required gateway authorization): the real gateway's
// route policy (`bdpay/gateway/policy.py`) denies CUSTOMER tokens outright
// for `payment_intents.create` and `qr.issue_dynamic` (`customer_scopes=None`
// on both) and additionally requires a merchant key for intent creation. The
// assertions below that used to decode a customer JWT off the create/QR
// forwards were the old (rejected) behaviour and are replaced: those two
// routes now assert a `Bearer <merchant key>` forward carrying the session's
// customer id in the body, while confirmation — the one write the policy
// accepts a customer for — keeps asserting the customer JWT.

import { test } from "node:test";
import assert from "node:assert/strict";
import { randomBytes } from "node:crypto";

import { loadLiveRoute, makeRequest, ctxFor } from "./live-route-harness.mjs";

const GATEWAY = "https://gateway.internal.invalid";
const MERCHANT_KEY = "demo_merchant_key_fixture";

const harness = await loadLiveRoute({
  NODE_ENV: "development",
  BDPAY_ENABLE_MOCK: "false",
  NEXT_PUBLIC_BDPAY_ENABLE_MOCK: "false",
  BDPAY_SESSION_SECRET: randomBytes(32).toString("hex"),
  BDPAY_LIVE_DINER_JWT_SECRET: randomBytes(32).toString("hex"),
  BDPAY_LIVE_API_BASE: GATEWAY,
  BDPAY_LIVE_DEMO_API_KEY: MERCHANT_KEY,
});
const { route, fetches } = harness;

async function post(pathname, body, cookie, headers) {
  return route.POST(makeRequest(body, cookie, headers), ctxFor(pathname));
}

async function signIn(phone) {
  const challenge = await post("v1/auth/diner/otp/request", { phone });
  assert.equal(challenge.status, 200);
  return challenge.json();
}

// The dev-mode code is written to the server console, not the response body.
function devCode() {
  return process.env.BDPAY_LIVE_DINER_OTP ?? null;
}

async function sessionCookieFor(phone) {
  process.env.BDPAY_LIVE_DINER_OTP = "314159";
  await signIn(phone);
  const verified = await post("v1/auth/diner/otp/verify", {
    phone,
    otp_code: devCode(),
  });
  assert.equal(verified.status, 200);
  const body = await verified.json();
  return {
    cookie: verified.setCookies.get("bdpay_diner_session"),
    customerId: body.customer.customer_id,
  };
}

function claimsOf(auth) {
  assert.match(auth, /^Bearer /);
  return JSON.parse(Buffer.from(auth.split(" ")[1].split(".")[1], "base64url").toString("utf8"));
}

test("340/356: intent creation and dynamic QR forward as the MERCHANT key, carrying the session customer id", async () => {
  const { cookie, customerId } = await sessionCookieFor("01755555555");
  const before = fetches.length;

  const paths = ["v1/payment-intents", "v1/qr-codes/dynamic"];
  for (const pathname of paths) {
    const res = await post(
      pathname,
      {
        merchant_id: "mrch_fixture",
        offer_id: "off_fixture",
        gross_amount_minor: 100000,
        // A client-supplied customer id must never be honoured.
        customer_id: "cust_someone_else",
      },
      cookie,
    );
    assert.equal(res.status, 200, `${pathname} must not 404`);
  }

  const forwarded = fetches.slice(before);
  assert.equal(forwarded.length, 2);
  for (const [index, call] of forwarded.entries()) {
    assert.equal(call.url, `${GATEWAY}/${paths[index]}`);
    assert.equal(call.init.method, "POST");
    // The gateway policy denies customer tokens for these two routes
    // (customer_scopes=None) and requires a merchant key for creation — the
    // forward MUST be the static merchant key, not a signed customer JWT.
    assert.equal(call.init.headers.Authorization, `Bearer ${MERCHANT_KEY}`);
    assert.ok(call.init.headers["Idempotency-Key"]);
    const sentBody = JSON.parse(call.init.body);
    assert.equal(sentBody.customer_id, customerId);
  }
});

test("340: confirmation still forwards the customer JWT (the write the policy accepts one for)", async () => {
  const { cookie, customerId } = await sessionCookieFor("01755555556");
  const before = fetches.length;

  const res = await post(
    "v1/payment-intents/pi_fixture/confirm",
    { merchant_id: "mrch_fixture", customer_id: "cust_someone_else" },
    cookie,
  );
  assert.equal(res.status, 200);

  const forwarded = fetches.slice(before);
  assert.equal(forwarded.length, 1);
  const call = forwarded[0];
  assert.equal(call.url, `${GATEWAY}/v1/payment-intents/pi_fixture/confirm`);
  assert.equal(call.init.method, "POST");
  const claims = claimsOf(call.init.headers.Authorization);
  assert.equal(claims.sub, customerId);
  assert.ok(claims.scope.includes("payment:write"));
  assert.ok(call.init.headers["Idempotency-Key"]);
  assert.equal(JSON.parse(call.init.body).customer_id, customerId);
});

test("356: a missing merchant key refuses intent creation and dynamic QR with the 500 envelope, and reaches no backend", async () => {
  const previousKey = process.env.BDPAY_LIVE_DEMO_API_KEY;
  delete process.env.BDPAY_LIVE_DEMO_API_KEY;
  try {
    const { cookie } = await sessionCookieFor("01755555557");
    const before = fetches.length;

    for (const pathname of ["v1/payment-intents", "v1/qr-codes/dynamic"]) {
      const res = await post(pathname, { merchant_id: "mrch_fixture" }, cookie);
      assert.equal(res.status, 500);
      const errBody = await res.json();
      assert.equal(errBody.error.code, "live_demo_api_key_missing");
    }

    assert.equal(fetches.length, before, "no fetch must be attempted without a merchant key");
  } finally {
    if (previousKey !== undefined) process.env.BDPAY_LIVE_DEMO_API_KEY = previousKey;
  }
});

test("F09: a retried confirm reuses one idempotency key; two creates do not", async () => {
  const { cookie } = await sessionCookieFor("01788888888");
  const before = fetches.length;

  await post("v1/payment-intents/pi_retry/confirm", { method: "BKASH" }, cookie);
  await post("v1/payment-intents/pi_retry/confirm", { method: "BKASH" }, cookie);
  await post("v1/payment-intents", { merchant_id: "mrch_fixture" }, cookie);
  await post("v1/payment-intents", { merchant_id: "mrch_fixture" }, cookie);

  const keys = fetches.slice(before).map((call) => call.init.headers["Idempotency-Key"]);
  // Confirming the same intent twice is ONE operation: a fresh key would let a
  // retry capture twice.
  assert.equal(keys[0], keys[1]);
  // Two identical carts are two real purchases and must not be deduplicated.
  assert.notEqual(keys[2], keys[3]);
});

test("F09: checkout without a session is 401, not 404, and reaches no backend", async () => {
  const before = fetches.length;
  for (const pathname of [
    "v1/payment-intents",
    "v1/qr-codes/dynamic",
    "v1/payment-intents/pi_fixture/confirm",
  ]) {
    const res = await post(pathname, { merchant_id: "mrch_fixture" });
    assert.equal(res.status, 401);
  }
  assert.equal(fetches.length, before);
});

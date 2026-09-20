// F08 — genuine diner identity in the live BFF, executed with no network
// (the E36 reviewer harness, re-runnable here).
//
// RED ON REVERT: the challenge response carried `debug_otp_code`, the verify
// handler compared that one shared configured code, and every successful
// verification issued a session for a single configured demo customer — so two
// phones were one customer and the second phone never needed a challenge at
// all (review F08 / evidence E34, E36).

import { test } from "node:test";
import assert from "node:assert/strict";
import { randomBytes } from "node:crypto";

import { loadLiveRoute, makeRequest, ctxFor } from "./live-route-harness.mjs";

const GATEWAY = "https://gateway.internal.invalid";

const harness = await loadLiveRoute({
  NODE_ENV: "development",
  BDPAY_ENABLE_MOCK: "false",
  NEXT_PUBLIC_BDPAY_ENABLE_MOCK: "false",
  BDPAY_SESSION_SECRET: randomBytes(32).toString("hex"),
  BDPAY_LIVE_DINER_JWT_SECRET: randomBytes(32).toString("hex"),
  BDPAY_LIVE_API_BASE: GATEWAY,
});
const { route, fetches } = harness;

async function post(pathname, body, cookie, headers) {
  return route.POST(makeRequest(body, cookie, headers), ctxFor(pathname));
}

async function get(pathname, cookie) {
  return route.GET(makeRequest({}, cookie), ctxFor(pathname));
}

async function signIn(phone) {
  const challenge = await post("v1/auth/diner/otp/request", { phone });
  const challengeBody = await challenge.json();
  assert.equal(challenge.status, 200);
  // F08: the code is NEVER in a response body.
  assert.equal(challengeBody.debug_otp_code, undefined);
  assert.equal(
    JSON.stringify(challengeBody).includes("otp_code"),
    false,
    "no OTP material may appear in the challenge response",
  );
  return challengeBody;
}

// The dev-mode code is written to the server console, not the response body.
function devCode() {
  return process.env.BDPAY_LIVE_DINER_OTP ?? null;
}

test("F08: the challenge response never discloses the OTP", async () => {
  process.env.BDPAY_LIVE_DINER_OTP = "314159";
  const body = await signIn("01700000001");
  assert.match(body.challenge_id, /^chl_[0-9a-f]{24}$/);
  assert.ok(Date.parse(body.expires_at) > Date.now());
});

test("F08: two phones are two customers, and the second needs its own challenge", async () => {
  process.env.BDPAY_LIVE_DINER_OTP = "314159";
  const code = devCode();

  await signIn("01700000001");
  const first = await post("v1/auth/diner/otp/verify", {
    phone: "01700000001",
    otp_code: code,
  });
  assert.equal(first.status, 200);
  const firstBody = await first.json();

  // The second phone has NOT requested a challenge: it must not be signed in
  // by the first phone's code.
  const unchallenged = await post("v1/auth/diner/otp/verify", {
    phone: "01900000002",
    otp_code: code,
  });
  assert.equal(unchallenged.status, 401);

  await signIn("01900000002");
  const second = await post("v1/auth/diner/otp/verify", {
    phone: "01900000002",
    otp_code: code,
  });
  assert.equal(second.status, 200);
  const secondBody = await second.json();

  assert.notEqual(firstBody.customer.customer_id, secondBody.customer.customer_id);
  assert.match(firstBody.customer.customer_id, /^cust_[0-9a-f]{24}$/);
});

test("F08: a verified code cannot be replayed", async () => {
  process.env.BDPAY_LIVE_DINER_OTP = "314159";
  const code = devCode();
  await signIn("01711111111");
  assert.equal(
    (await post("v1/auth/diner/otp/verify", { phone: "01711111111", otp_code: code })).status,
    200,
  );
  const replay = await post("v1/auth/diner/otp/verify", {
    phone: "01711111111",
    otp_code: code,
  });
  assert.equal(replay.status, 401);
});

test("F08: a wrong code is still 401 and the attempt budget is finite", async () => {
  process.env.BDPAY_LIVE_DINER_OTP = "314159";
  await signIn("01722222222");
  for (let i = 0; i < 5; i += 1) {
    const wrong = await post("v1/auth/diner/otp/verify", {
      phone: "01722222222",
      otp_code: "000000",
    });
    assert.equal(wrong.status, 401);
  }
  // Budget spent: even the right code no longer works without a new challenge.
  const exhausted = await post("v1/auth/diner/otp/verify", {
    phone: "01722222222",
    otp_code: devCode(),
  });
  assert.equal(exhausted.status, 401);

  // ...and re-requesting a challenge must not hand the attacker a fresh budget.
  await signIn("01722222222");
  const rearmed = await post("v1/auth/diner/otp/verify", {
    phone: "01722222222",
    otp_code: devCode(),
  });
  assert.equal(rearmed.status, 401, "a re-requested challenge must not reset the attempt budget");
});

test("F08: production refuses a fixture OTP secret", async () => {
  const previousNodeEnv = process.env.NODE_ENV;
  process.env.NODE_ENV = "production";
  process.env.BDPAY_LIVE_DINER_OTP = "314159";
  try {
    const res = await post("v1/auth/diner/otp/request", { phone: "01733333333" });
    assert.equal(res.status, 500);
    const body = await res.json();
    assert.equal(body.error.code, "otp_fixture_secret_refused");
  } finally {
    process.env.NODE_ENV = previousNodeEnv;
  }
});

test("F08: production without a delivery channel fails closed", async () => {
  const previousNodeEnv = process.env.NODE_ENV;
  const previousOtp = process.env.BDPAY_LIVE_DINER_OTP;
  process.env.NODE_ENV = "production";
  delete process.env.BDPAY_LIVE_DINER_OTP;
  try {
    const res = await post("v1/auth/diner/otp/request", { phone: "01744444444" });
    assert.equal(res.status, 500);
    assert.equal((await res.json()).error.code, "otp_delivery_unconfigured");
  } finally {
    process.env.NODE_ENV = previousNodeEnv;
    if (previousOtp !== undefined) process.env.BDPAY_LIVE_DINER_OTP = previousOtp;
  }
});

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

test("F08: the session read reports the authenticated customer, not a demo persona", async () => {
  const { cookie, customerId } = await sessionCookieFor("01766666666");
  const res = await get("v1/auth/diner/session", cookie);
  assert.equal(res.status, 200);
  assert.equal((await res.json()).customer.customer_id, customerId);
});

test("harness made no real network calls", () => {
  for (const call of fetches) assert.ok(call.url.startsWith(GATEWAY));
});

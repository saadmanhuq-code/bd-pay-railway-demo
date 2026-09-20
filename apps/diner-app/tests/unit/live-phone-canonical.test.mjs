// Finding 341 (diner-phone-aliases-split-identity-and-attempt-budget):
// executed through the live BFF with no network (same harness as F08/F09).
//
// RED ON REVERT: `normalizeDigits` only converted Bangla digits and stripped
// spaces/hyphens, so 017…, +88017…, 88017… and 17… — four spellings of the
// SAME physical number — were stored under distinct map keys. That minted
// distinct customer ids and independent five-attempt OTP budgets for one
// phone, fragmenting payment history and letting the attempt limit be
// bypassed by switching spelling. Canonical form used everywhere here: the
// national 11-digit form `01XXXXXXXXX` (see `canonicalizePhone` in
// src/lib/live/otp.ts).

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
const { route } = harness;

async function post(pathname, body) {
  return route.POST(makeRequest(body), ctxFor(pathname));
}

function devCode() {
  return process.env.BDPAY_LIVE_DINER_OTP ?? null;
}

// Four accepted spellings of one physical Bangladeshi mobile number.
const ALIASES = ["01755551234", "+8801755551234", "8801755551234", "1755551234"];

test("341: all four spellings of one phone share ONE challenge — a code requested under one spelling verifies under another", async () => {
  process.env.BDPAY_LIVE_DINER_OTP = "271828";
  const challenge = await post("v1/auth/diner/otp/request", { phone: ALIASES[0] });
  assert.equal(challenge.status, 200);

  const verified = await post("v1/auth/diner/otp/verify", {
    phone: ALIASES[1],
    otp_code: devCode(),
  });
  assert.equal(verified.status, 200, "a challenge minted under one spelling must verify under another");
});

test("341: all four spellings mint the SAME customer id", async () => {
  const customerIds = [];
  for (const alias of ALIASES) {
    process.env.BDPAY_LIVE_DINER_OTP = "141421";
    await post("v1/auth/diner/otp/request", { phone: alias });
    const verified = await post("v1/auth/diner/otp/verify", { phone: alias, otp_code: devCode() });
    assert.equal(verified.status, 200);
    const body = await verified.json();
    customerIds.push(body.customer.customer_id);
  }
  for (const id of customerIds) {
    assert.equal(id, customerIds[0], "every spelling of the same phone must mint the same customer id");
  }
});

test("341: all four spellings mask identically", async () => {
  const masks = [];
  for (const alias of ALIASES) {
    process.env.BDPAY_LIVE_DINER_OTP = "173205";
    await post("v1/auth/diner/otp/request", { phone: alias });
    const verified = await post("v1/auth/diner/otp/verify", { phone: alias, otp_code: devCode() });
    assert.equal(verified.status, 200);
    const body = await verified.json();
    masks.push(body.customer.phone_masked);
  }
  for (const mask of masks) {
    assert.equal(mask, masks[0], "every spelling of the same phone must mask identically");
  }
});

test("341: the five-attempt budget is ONE shared budget across every spelling", async () => {
  process.env.BDPAY_LIVE_DINER_OTP = "223606";
  await post("v1/auth/diner/otp/request", { phone: ALIASES[0] });

  // Burn the budget using a DIFFERENT spelling on each attempt.
  for (let i = 0; i < 5; i += 1) {
    const wrong = await post("v1/auth/diner/otp/verify", {
      phone: ALIASES[i % ALIASES.length],
      otp_code: "000000",
    });
    assert.equal(wrong.status, 401);
  }

  // Budget spent — even the right code, tried under yet another spelling,
  // must still fail. If the budget were per-spelling this would pass.
  const exhausted = await post("v1/auth/diner/otp/verify", {
    phone: ALIASES[1],
    otp_code: devCode(),
  });
  assert.equal(exhausted.status, 401, "the attempt budget must be shared across every accepted spelling");
});

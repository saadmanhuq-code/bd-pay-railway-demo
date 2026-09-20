import { test } from "node:test";
import assert from "node:assert/strict";

import {
  LIVE_MERCHANT_LOGIN_UNCONFIGURED_CODE,
  LIVE_MERCHANT_TOTP_INVALID_CODE,
  totpAt,
  verifyLiveMerchantLogin,
} from "../../.test-dist/lib/api/live-merchant-login.js";

const fixtureB32 = "ONSWG4TFOQ";
const EMAIL = "owner@merchant.example";
const AT_MS = 1_111_111_111_000;

function configuredEnv() {
  return {
    BDPAY_LIVE_MERCHANT_EMAIL: EMAIL,
    BDPAY_LIVE_MERCHANT_TOTP_SECRET_B32: fixtureB32,
  };
}

test("live login refuses any 6-digit code when merchant TOTP is unconfigured", async () => {
  const decision = verifyLiveMerchantLogin({
    email: EMAIL,
    totpCode: "123456",
    persona: "owner",
    atMs: AT_MS,
    env: {},
  });
  assert.equal(decision.ok, false);
  if (decision.ok) throw new Error("expected failure");
  assert.equal(decision.code, LIVE_MERCHANT_LOGIN_UNCONFIGURED_CODE);
  assert.equal(decision.status, 401);
});

test("live login rejects a formatted TOTP that does not match the configured secret", async () => {
  const expected = totpAt(fixtureB32, AT_MS);
  assert.equal(typeof expected, "string");
  const wrong = expected === "000000" ? "111111" : "000000";
  const decision = verifyLiveMerchantLogin({
    email: EMAIL,
    totpCode: wrong,
    persona: "owner",
    atMs: AT_MS,
    env: configuredEnv(),
  });
  assert.equal(decision.ok, false);
  if (decision.ok) throw new Error("expected failure");
  assert.equal(decision.code, LIVE_MERCHANT_TOTP_INVALID_CODE);
});

test("live login accepts only the RFC 6238 code for the configured merchant", async () => {
  const expected = totpAt(fixtureB32, AT_MS);
  assert.equal(typeof expected, "string");
  const decision = verifyLiveMerchantLogin({
    email: ` ${EMAIL.toUpperCase()} `,
    totpCode: expected,
    persona: "developer",
    atMs: AT_MS,
    env: configuredEnv(),
  });
  assert.equal(decision.ok, true);
  if (!decision.ok) throw new Error("expected success");
  assert.equal(decision.persona, "developer");
  assert.equal(decision.email, EMAIL);
});

test("live login does not mint a session for a matching TOTP sent as the wrong email", async () => {
  const expected = totpAt(fixtureB32, AT_MS);
  const decision = verifyLiveMerchantLogin({
    email: "intruder@example.com",
    totpCode: expected,
    persona: "owner",
    atMs: AT_MS,
    env: configuredEnv(),
  });
  assert.equal(decision.ok, false);
  if (decision.ok) throw new Error("expected failure");
  assert.equal(decision.code, LIVE_MERCHANT_TOTP_INVALID_CODE);
});

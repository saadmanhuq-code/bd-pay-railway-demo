// Finding 355 (OTP verification is not single-use under concurrency).
// path: apps/diner-app/src/lib/live/otp.ts
//
// RED ON REVERT: `verifyChallenge` incremented `attempts`, then `await`ed the
// hash comparison, and only THEN deleted the challenge. Two concurrent
// verifies with the same valid code both ran their synchronous pre-checks
// before either deleted anything, both hashed correctly, and both minted a
// session — defeating replay protection. Loads `src/lib/live/otp.ts`
// directly (type-stripped, no Next runtime, no network) via the shared
// harness's `loadLibModule`.

import { test } from "node:test";
import assert from "node:assert/strict";

import { loadLibModule } from "./live-route-harness.mjs";

const otp = await loadLibModule("lib/live/otp");

test("355: two concurrent verifyChallenge calls with the valid code yield exactly one ok", async () => {
  process.env.BDPAY_LIVE_DINER_OTP = "555000";
  const phone = "01799990001";
  const issued = await otp.issueChallenge(phone);
  const code = issued.deliverableCode;

  const [first, second] = await Promise.all([
    otp.verifyChallenge(phone, code),
    otp.verifyChallenge(phone, code),
  ]);

  const results = [first, second];
  const okCount = results.filter((r) => r.ok).length;
  assert.equal(okCount, 1, "exactly one of the two concurrent verifies must succeed");

  const loser = results.find((r) => !r.ok);
  assert.ok(loser, "the other concurrent verify must fail");
  // Deterministic refusal: the challenge was already claimed by the winner.
  assert.equal(loser.code, "otp_challenge_required");
});

test("355: a wrong code followed by the right code still works, and the spent attempt is counted", async () => {
  process.env.BDPAY_LIVE_DINER_OTP = "555001";
  const phone = "01799990002";
  const issued = await otp.issueChallenge(phone);
  const code = issued.deliverableCode;

  const wrong = await otp.verifyChallenge(phone, "000000");
  assert.equal(wrong.ok, false);
  assert.equal(wrong.code, "otp_invalid");

  const right = await otp.verifyChallenge(phone, code);
  assert.equal(right.ok, true, "the correct code must still verify after a prior wrong attempt");
});

test("355: many concurrent verifies with the valid code still yield exactly one ok", async () => {
  process.env.BDPAY_LIVE_DINER_OTP = "555002";
  const phone = "01799990003";
  const issued = await otp.issueChallenge(phone);
  const code = issued.deliverableCode;

  const results = await Promise.all(Array.from({ length: 8 }, () => otp.verifyChallenge(phone, code)));
  const okCount = results.filter((r) => r.ok).length;
  assert.equal(okCount, 1, "exactly one of many concurrent verifies must succeed");
});

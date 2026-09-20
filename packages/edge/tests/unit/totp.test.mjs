// The shared mock-server TOTP verifier (src/mock/totp.ts, moved verbatim
// from ops-console/developer-portal — see the lane/349 workorder) is pure
// node:crypto — no next/server, so it compiles straight to .test-dist/ like
// env-flag does. This test moved here from apps/ops-console/tests/unit/ along
// with the module it exercises; ops-console's own pretest/test scripts now
// cover src/lib/format.ts instead (see apps/ops-console/tests/unit/format.test.mjs).

import { test } from "node:test";
import assert from "node:assert/strict";
import { createHmac } from "node:crypto";

import { verifyMockTotp } from "../../.test-dist/mock/totp.js";

// An arbitrary fixed base32 secret for this test only (RFC 4648 alphabet).
const SECRET_B32 = "JBSWY3DPEHPK3PXP";
const STEP_SECONDS = 30;
const AT_MS = Date.parse("2026-01-01T00:00:00Z");

// Reimplementation of the RFC 6238 TOTP code for the fixed secret/time above,
// written independently of src/lib/mock/totp.ts (using only node:crypto) so
// this test actually proves the module computes the standard algorithm
// rather than just being consistent with itself.
function decodeBase32(value) {
  const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";
  const normalized = value.replace(/[\s=]/g, "").toUpperCase();
  let bits = 0;
  let bitCount = 0;
  const bytes = [];
  for (const ch of normalized) {
    const idx = alphabet.indexOf(ch);
    bits = (bits << 5) | idx;
    bitCount += 5;
    if (bitCount >= 8) {
      bytes.push((bits >> (bitCount - 8)) & 0xff);
      bitCount -= 8;
    }
  }
  return Buffer.from(bytes);
}

function counterBuffer(counter) {
  const buf = Buffer.alloc(8);
  let value = BigInt(counter);
  for (let i = 7; i >= 0; i -= 1) {
    buf[i] = Number(value & 0xffn);
    value >>= 8n;
  }
  return buf;
}

function expectedCode(atMs, stepOffset = 0) {
  const secret = decodeBase32(SECRET_B32);
  const counter = Math.floor(atMs / 1000 / STEP_SECONDS) + stepOffset;
  const digest = createHmac("sha1", secret).update(counterBuffer(counter)).digest();
  const offset = digest.readUInt8(digest.length - 1) & 0x0f;
  const binary =
    ((digest.readUInt8(offset) & 0x7f) << 24) |
    ((digest.readUInt8(offset + 1) & 0xff) << 16) |
    ((digest.readUInt8(offset + 2) & 0xff) << 8) |
    (digest.readUInt8(offset + 3) & 0xff);
  return String(binary % 1_000_000).padStart(6, "0");
}

function withSecret(fn) {
  const previous = process.env.BDPAY_MOCK_TOTP_SECRET_B32;
  process.env.BDPAY_MOCK_TOTP_SECRET_B32 = SECRET_B32;
  try {
    return fn();
  } finally {
    if (previous === undefined) delete process.env.BDPAY_MOCK_TOTP_SECRET_B32;
    else process.env.BDPAY_MOCK_TOTP_SECRET_B32 = previous;
  }
}

test("verifyMockTotp accepts the current-step code for a configured secret", () => {
  withSecret(() => {
    assert.equal(verifyMockTotp(expectedCode(AT_MS), AT_MS), "ok");
  });
});

test("verifyMockTotp accepts the adjacent +/-1 step window", () => {
  withSecret(() => {
    assert.equal(verifyMockTotp(expectedCode(AT_MS, -1), AT_MS), "ok");
    assert.equal(verifyMockTotp(expectedCode(AT_MS, 1), AT_MS), "ok");
  });
});

test("verifyMockTotp rejects a wrong code", () => {
  withSecret(() => {
    // Well outside the +/-1 step window accepted above.
    assert.equal(verifyMockTotp(expectedCode(AT_MS, 5), AT_MS), "invalid");
    assert.equal(verifyMockTotp("000000", AT_MS), "invalid");
  });
});

test("verifyMockTotp reports secret_missing when no secret is configured in production", () => {
  const previousSecret = process.env.BDPAY_MOCK_TOTP_SECRET_B32;
  const previousEnv = process.env.NODE_ENV;
  delete process.env.BDPAY_MOCK_TOTP_SECRET_B32;
  process.env.NODE_ENV = "production";
  try {
    assert.equal(verifyMockTotp(expectedCode(AT_MS), AT_MS), "secret_missing");
  } finally {
    process.env.NODE_ENV = previousEnv;
    if (previousSecret === undefined) delete process.env.BDPAY_MOCK_TOTP_SECRET_B32;
    else process.env.BDPAY_MOCK_TOTP_SECRET_B32 = previousSecret;
  }
});

// Oracle test for the shared HMAC-signed session factory
// (src/mock/signed-session.ts, exercised here via createSignedSession).
//
// The oracle below is the ORIGINAL developer-portal src/lib/mock/session.ts
// function bodies (token format, HMAC construction, base64url encoding,
// clock handling, cookie option values), copied verbatim minus type
// annotations, from the implementation this lane replaced. It is
// deliberately NOT implemented in terms of the module under test — it is an
// independent re-implementation so this test actually proves byte-for-byte
// equivalence rather than just internal self-consistency. A second oracle,
// copied the same way from the ORIGINAL ops-console/diner-app
// src/lib/mock/session.ts (the "sub"-shaped payload, the shape shared by
// both those apps before this lane), proves the factory's genericity across
// payload shapes and TTLs, not just the persona-shaped one.
//
// If a future edit to signed-session.ts changes the token format, the HMAC
// construction, the encoding, the clock handling or the cookie option
// values, this test fails — that is the point.

import { test } from "node:test";
import assert from "node:assert/strict";

import { createSignedSession } from "../../.test-dist/mock/signed-session.js";

// ---------------------------------------------------------------------------
// Oracle 1 — the ORIGINAL apps/developer-portal/src/lib/mock/session.ts
// (persona payload, 8h TTL), before it became a thin wrapper of the factory.
// ---------------------------------------------------------------------------

const ORACLE_PORTAL_TOKEN_VERSION = "v1";
const ORACLE_PORTAL_TTL_SECONDS = 8 * 60 * 60;
const ORACLE_PORTAL_VALID_PERSONAS = new Set(["owner", "developer", "finance_maker", "finance_checker"]);

const oracleTextEncoder = new TextEncoder();
const oracleTextDecoder = new TextDecoder();

function oracleBytesToBase64Url(bytes) {
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
}

function oracleBase64UrlToBytes(value) {
  try {
    const base64 = value.replace(/-/g, "+").replace(/_/g, "/");
    const padded = base64.padEnd(base64.length + ((4 - (base64.length % 4)) % 4), "=");
    const binary = atob(padded);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
    return bytes;
  } catch {
    return null;
  }
}

function oracleEncodeText(value) {
  return oracleBytesToBase64Url(oracleTextEncoder.encode(value));
}

function oracleDecodeText(value) {
  const bytes = oracleBase64UrlToBytes(value);
  return bytes === null ? null : oracleTextDecoder.decode(bytes);
}

async function oracleHmac(payload, secret) {
  const key = await crypto.subtle.importKey(
    "raw",
    oracleTextEncoder.encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const signature = await crypto.subtle.sign("HMAC", key, oracleTextEncoder.encode(payload));
  return oracleBytesToBase64Url(new Uint8Array(signature));
}

function oracleConstantTimeEqual(a, b) {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i += 1) {
    diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  }
  return diff === 0;
}

function oracleIsPortalPersona(value) {
  return ORACLE_PORTAL_VALID_PERSONAS.has(value);
}

function oracleParsePortalPayload(raw) {
  try {
    const payload = JSON.parse(raw);
    if (typeof payload.persona !== "string" || !oracleIsPortalPersona(payload.persona)) return null;
    if (typeof payload.iat !== "number" || !Number.isInteger(payload.iat)) return null;
    if (typeof payload.exp !== "number" || !Number.isInteger(payload.exp)) return null;
    return { persona: payload.persona, iat: payload.iat, exp: payload.exp };
  } catch {
    return null;
  }
}

async function oracleIssuePortalSession(persona, secret, issuedAt) {
  if (!secret) return null;
  const payload = { persona, iat: issuedAt, exp: issuedAt + ORACLE_PORTAL_TTL_SECONDS };
  const encodedPayload = oracleEncodeText(JSON.stringify(payload));
  const signature = await oracleHmac(`${ORACLE_PORTAL_TOKEN_VERSION}.${encodedPayload}`, secret);
  return `${ORACLE_PORTAL_TOKEN_VERSION}.${encodedPayload}.${signature}`;
}

async function oracleVerifyPortalSession(token, secret, at) {
  if (!secret) return null;
  const [version, encodedPayload, signature, extra] = token.split(".");
  if (version !== ORACLE_PORTAL_TOKEN_VERSION || !encodedPayload || !signature || extra !== undefined) {
    return null;
  }
  const expected = await oracleHmac(`${version}.${encodedPayload}`, secret);
  if (!oracleConstantTimeEqual(signature, expected)) return null;
  const rawPayload = oracleDecodeText(encodedPayload);
  if (rawPayload === null) return null;
  const payload = oracleParsePortalPayload(rawPayload);
  if (payload === null) return null;
  if (payload.iat > at || payload.exp <= at) return null;
  return payload;
}

function oraclePortalCookieOptions(nodeEnv) {
  return {
    httpOnly: true,
    sameSite: "lax",
    path: "/",
    maxAge: ORACLE_PORTAL_TTL_SECONDS,
    secure: nodeEnv === "production",
  };
}

function oraclePortalClearCookieOptions(nodeEnv) {
  return {
    httpOnly: true,
    sameSite: "lax",
    path: "/",
    maxAge: 0,
    secure: nodeEnv === "production",
  };
}

// ---------------------------------------------------------------------------
// Oracle 2 — the ORIGINAL apps/ops-console (and, but for the cookie name,
// TTL and dev secret, apps/diner-app) src/lib/mock/session.ts ("sub" payload
// with a prefix-validated subject).
// ---------------------------------------------------------------------------

async function oracleIssueSubSession(sub, secret, issuedAt, ttlSeconds) {
  if (!secret) return null;
  const payload = { sub, iat: issuedAt, exp: issuedAt + ttlSeconds };
  const encodedPayload = oracleEncodeText(JSON.stringify(payload));
  const signature = await oracleHmac(`${ORACLE_PORTAL_TOKEN_VERSION}.${encodedPayload}`, secret);
  return `${ORACLE_PORTAL_TOKEN_VERSION}.${encodedPayload}.${signature}`;
}

// ---------------------------------------------------------------------------
// Fixed inputs shared by every case below.
// ---------------------------------------------------------------------------

const SECRET = "test-fixed-secret-for-oracle-comparison-only";
const ISSUED_AT = Math.floor(Date.parse("2026-03-15T12:00:00Z") / 1000);

test("factory issues a byte-identical token to the original developer-portal implementation (persona payload)", async () => {
  const oracleToken = await oracleIssuePortalSession("finance_checker", SECRET, ISSUED_AT);

  const factory = createSignedSession({
    cookieName: "bdpay_portal_session",
    ttlSeconds: ORACLE_PORTAL_TTL_SECONDS,
    secretEnvVars: ["BDPAY_SESSION_SECRET_ORACLE_TEST", "BDPAY_MOCK_SESSION_SECRET_ORACLE_TEST"],
    devSecret: "unused-because-secret-env-var-is-set-below",
    parseExtra: (raw) =>
      typeof raw.persona === "string" && ORACLE_PORTAL_VALID_PERSONAS.has(raw.persona)
        ? { persona: raw.persona }
        : null,
  });

  const previous = process.env.BDPAY_SESSION_SECRET_ORACLE_TEST;
  process.env.BDPAY_SESSION_SECRET_ORACLE_TEST = SECRET;
  try {
    const factoryToken = await factory.issue({ persona: "finance_checker" }, ISSUED_AT);
    assert.equal(factoryToken, oracleToken, "factory token must be byte-identical to the original oracle token");

    // Cross-compatibility both ways: either implementation must accept a
    // token minted by the other, at the same instant.
    const viaOracleVerify = await oracleVerifyPortalSession(factoryToken, SECRET, ISSUED_AT);
    assert.deepEqual(viaOracleVerify, { persona: "finance_checker", iat: ISSUED_AT, exp: ISSUED_AT + ORACLE_PORTAL_TTL_SECONDS });

    const viaFactoryVerify = await factory.verify(oracleToken, ISSUED_AT);
    assert.deepEqual(viaFactoryVerify, { persona: "finance_checker", iat: ISSUED_AT, exp: ISSUED_AT + ORACLE_PORTAL_TTL_SECONDS });
  } finally {
    if (previous === undefined) delete process.env.BDPAY_SESSION_SECRET_ORACLE_TEST;
    else process.env.BDPAY_SESSION_SECRET_ORACLE_TEST = previous;
  }
});

test("factory reproduces the ops-console/diner-app 'sub' payload shape and TTL byte-identically", async () => {
  const ttlSeconds = 12 * 60 * 60; // diner-app's TTL, the largest of the three
  const oracleToken = await oracleIssueSubSession("cust_0001", SECRET, ISSUED_AT, ttlSeconds);

  const factory = createSignedSession({
    cookieName: "bdpay_diner_session",
    ttlSeconds,
    secretEnvVars: ["BDPAY_SESSION_SECRET_ORACLE_TEST2", "BDPAY_MOCK_SESSION_SECRET_ORACLE_TEST2"],
    devSecret: "unused-because-secret-env-var-is-set-below",
    parseExtra: (raw) => (typeof raw.sub === "string" && raw.sub.startsWith("cust_") ? { sub: raw.sub } : null),
  });

  const previous = process.env.BDPAY_SESSION_SECRET_ORACLE_TEST2;
  process.env.BDPAY_SESSION_SECRET_ORACLE_TEST2 = SECRET;
  try {
    const factoryToken = await factory.issue({ sub: "cust_0001" }, ISSUED_AT);
    assert.equal(factoryToken, oracleToken);
  } finally {
    if (previous === undefined) delete process.env.BDPAY_SESSION_SECRET_ORACLE_TEST2;
    else process.env.BDPAY_SESSION_SECRET_ORACLE_TEST2 = previous;
  }
});

test("factory cookieOptions()/clearCookieOptions() match the original literal shapes in dev and production", async () => {
  const factory = createSignedSession({
    cookieName: "bdpay_portal_session",
    ttlSeconds: ORACLE_PORTAL_TTL_SECONDS,
    secretEnvVars: ["BDPAY_SESSION_SECRET_ORACLE_TEST3", "BDPAY_MOCK_SESSION_SECRET_ORACLE_TEST3"],
    devSecret: "dev-secret",
    parseExtra: () => null,
  });

  const previousEnv = process.env.NODE_ENV;
  try {
    process.env.NODE_ENV = "development";
    assert.deepEqual(factory.cookieOptions(), oraclePortalCookieOptions("development"));
    assert.deepEqual(factory.clearCookieOptions(), oraclePortalClearCookieOptions("development"));

    process.env.NODE_ENV = "production";
    assert.deepEqual(factory.cookieOptions(), oraclePortalCookieOptions("production"));
    assert.deepEqual(factory.clearCookieOptions(), oraclePortalClearCookieOptions("production"));
  } finally {
    process.env.NODE_ENV = previousEnv;
  }
});

test("verify rejects a tampered signature, an expired token, and a not-yet-valid token, matching the oracle", async () => {
  const factory = createSignedSession({
    cookieName: "bdpay_portal_session",
    ttlSeconds: ORACLE_PORTAL_TTL_SECONDS,
    secretEnvVars: ["BDPAY_SESSION_SECRET_ORACLE_TEST4", "BDPAY_MOCK_SESSION_SECRET_ORACLE_TEST4"],
    devSecret: "unused",
    parseExtra: (raw) =>
      typeof raw.persona === "string" && ORACLE_PORTAL_VALID_PERSONAS.has(raw.persona)
        ? { persona: raw.persona }
        : null,
  });

  const previous = process.env.BDPAY_SESSION_SECRET_ORACLE_TEST4;
  process.env.BDPAY_SESSION_SECRET_ORACLE_TEST4 = SECRET;
  try {
    const token = await factory.issue({ persona: "owner" }, ISSUED_AT);

    // Tampered signature.
    const [v, p, sig] = token.split(".");
    const tampered = `${v}.${p}.${sig.slice(0, -1)}${sig.endsWith("A") ? "B" : "A"}`;
    assert.equal(await factory.verify(tampered, ISSUED_AT), null);
    assert.equal(await oracleVerifyPortalSession(tampered, SECRET, ISSUED_AT), null);

    // Expired: checked exactly at exp (exclusive) — matches `payload.exp <= at`.
    const exp = ISSUED_AT + ORACLE_PORTAL_TTL_SECONDS;
    assert.equal(await factory.verify(token, exp), null);
    assert.equal(await oracleVerifyPortalSession(token, SECRET, exp), null);
    assert.notEqual(await factory.verify(token, exp - 1), null);

    // Not yet valid: iat > at.
    assert.equal(await factory.verify(token, ISSUED_AT - 1), null);
    assert.equal(await oracleVerifyPortalSession(token, SECRET, ISSUED_AT - 1), null);
  } finally {
    if (previous === undefined) delete process.env.BDPAY_SESSION_SECRET_ORACLE_TEST4;
    else process.env.BDPAY_SESSION_SECRET_ORACLE_TEST4 = previous;
  }
});

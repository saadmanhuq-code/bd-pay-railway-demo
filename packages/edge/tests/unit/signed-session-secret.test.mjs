// Fail-closed session secret resolution (src/mock/signed-session.ts).
//
// RED ON REVERT: sessionSecret() used to fall back to the hardcoded, public
// per-app devSecret whenever BDPAY_SESSION_SECRET / BDPAY_MOCK_SESSION_SECRET
// were empty — including in production when BDPAY_ENABLE_MOCK was on (the
// Railway demo). A deploy missing the env var then signed ops-console,
// developer-portal and diner-app sessions with a key anyone can read.

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  createSignedSession,
  resolveSessionSecret,
  SessionSecretConfigError,
  SESSION_SECRET_MIN_LENGTH,
} from "../../.test-dist/mock/signed-session.js";

const VARS = ["BDPAY_SESSION_SECRET_FC_TEST", "BDPAY_MOCK_SESSION_SECRET_FC_TEST"];
const DEV_SECRET = "bdpay-fail-closed-test-dev-session-secret-v1";
const GOOD_SECRET = "a".repeat(64);
const ISSUED_AT = Math.floor(Date.parse("2026-03-15T12:00:00Z") / 1000);

function makeSession() {
  return createSignedSession({
    cookieName: "bdpay_ops_session",
    ttlSeconds: 8 * 60 * 60,
    secretEnvVars: VARS,
    devSecret: DEV_SECRET,
    parseExtra: (raw) => (typeof raw.sub === "string" ? { sub: raw.sub } : null),
  });
}

async function withEnv(overrides, fn) {
  const keys = ["NODE_ENV", "BDPAY_ENABLE_MOCK", ...VARS];
  const saved = Object.fromEntries(keys.map((k) => [k, process.env[k]]));
  for (const k of keys) delete process.env[k];
  Object.assign(process.env, overrides);
  try {
    return await fn();
  } finally {
    for (const k of keys) {
      if (saved[k] === undefined) delete process.env[k];
      else process.env[k] = saved[k];
    }
  }
}

test("production with no secret throws a clear error naming the env var (even with BDPAY_ENABLE_MOCK=1)", async () => {
  for (const mock of [undefined, "1"]) {
    const env = { NODE_ENV: "production", ...(mock ? { BDPAY_ENABLE_MOCK: mock } : {}) };
    await withEnv(env, async () => {
      const session = makeSession();
      await assert.rejects(() => session.issue({ sub: "oper_1" }, ISSUED_AT), (err) => {
        assert.ok(err instanceof SessionSecretConfigError);
        assert.match(err.message, /BDPAY_SESSION_SECRET_FC_TEST is required when NODE_ENV=production/);
        assert.doesNotMatch(err.message, new RegExp(DEV_SECRET));
        return true;
      });
      await assert.rejects(() => session.verify("v1.e30.sig", ISSUED_AT), SessionSecretConfigError);
    });
  }
  // A whitespace-only value counts as missing.
  assert.throws(
    () => resolveSessionSecret(VARS, DEV_SECRET, { NODE_ENV: "production", [VARS[0]]: "   " }),
    /BDPAY_SESSION_SECRET_FC_TEST is required/,
  );
});

test("production rejects a secret shorter than the minimum length", () => {
  const short = "x".repeat(SESSION_SECRET_MIN_LENGTH - 1);
  assert.throws(
    () => resolveSessionSecret(VARS, DEV_SECRET, { NODE_ENV: "production", [VARS[0]]: short }),
    (err) => err instanceof SessionSecretConfigError && /too short/.test(err.message) && !err.message.includes(short),
  );
  const exact = "x".repeat(SESSION_SECRET_MIN_LENGTH);
  assert.equal(resolveSessionSecret(VARS, DEV_SECRET, { NODE_ENV: "production", [VARS[0]]: exact }), exact);
});

test("production with a secret signs and verifies, and the legacy alias still works", async () => {
  await withEnv({ NODE_ENV: "production", [VARS[0]]: GOOD_SECRET }, async () => {
    const session = makeSession();
    const token = await session.issue({ sub: "oper_1" }, ISSUED_AT);
    assert.match(token, /^v1\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$/);
    assert.deepEqual(await session.verify(token, ISSUED_AT), {
      sub: "oper_1",
      iat: ISSUED_AT,
      exp: ISSUED_AT + 8 * 60 * 60,
    });
  });
  await withEnv({ NODE_ENV: "production", [VARS[1]]: GOOD_SECRET }, async () => {
    const token = await makeSession().issue({ sub: "oper_1" }, ISSUED_AT);
    assert.ok(token);
  });
  // The token for a valid secret is identical in production and dev (format unchanged).
  const prodToken = await withEnv({ NODE_ENV: "production", [VARS[0]]: GOOD_SECRET }, () =>
    makeSession().issue({ sub: "oper_1" }, ISSUED_AT),
  );
  const devToken = await withEnv({ NODE_ENV: "development", [VARS[0]]: GOOD_SECRET }, () =>
    makeSession().issue({ sub: "oper_1" }, ISSUED_AT),
  );
  assert.equal(prodToken, devToken);
});

test("dev/test with no secret keeps working via the dev fallback", async () => {
  for (const nodeEnv of [undefined, "development", "test"]) {
    await withEnv(nodeEnv ? { NODE_ENV: nodeEnv } : {}, async () => {
      assert.equal(resolveSessionSecret(VARS, DEV_SECRET), DEV_SECRET);
      const session = makeSession();
      const token = await session.issue({ sub: "oper_1" }, ISSUED_AT);
      assert.ok(token);
      assert.equal((await session.verify(token, ISSUED_AT))?.sub, "oper_1");
    });
  }
});

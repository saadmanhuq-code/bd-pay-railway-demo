// The pure decision half of the shared SEC-01 mock-route production guard
// (`mockRouteRefused`, exported from src/env-flag.ts alongside
// `isMockEnabled` — see mock/guard.ts for the NextResponse-shaped wrapper
// every app's src/lib/mock/guard.ts re-exports). No next/server import, so
// it compiles straight to .test-dist/ and is exercised directly here.

import { test } from "node:test";
import assert from "node:assert/strict";

import { mockRouteRefused } from "../../.test-dist/env-flag.js";

test("mockRouteRefused refuses in production with no opt-in", () => {
  assert.equal(mockRouteRefused({ NODE_ENV: "production" }), true);
  assert.equal(mockRouteRefused({ NODE_ENV: "production", BDPAY_ENABLE_MOCK: "0" }), true);
  assert.equal(mockRouteRefused({ NODE_ENV: "production", BDPAY_ENABLE_MOCK: "false" }), true);
});

test("mockRouteRefused allows production only with an explicit affirmative opt-in", () => {
  for (const value of ["1", "true", "yes", "on", " TRUE "]) {
    assert.equal(mockRouteRefused({ NODE_ENV: "production", BDPAY_ENABLE_MOCK: value }), false);
  }
});

test("mockRouteRefused never refuses outside production, opt-in or not", () => {
  assert.equal(mockRouteRefused({ NODE_ENV: "development" }), false);
  assert.equal(mockRouteRefused({ NODE_ENV: "test", BDPAY_ENABLE_MOCK: "0" }), false);
  assert.equal(mockRouteRefused({}), false);
});

test("mockRouteRefused ignores a public/client NEXT_PUBLIC_BDPAY_ENABLE_MOCK opt-in", () => {
  assert.equal(
    mockRouteRefused({ NODE_ENV: "production", NEXT_PUBLIC_BDPAY_ENABLE_MOCK: "1" }),
    true,
    "a client-exposed var alone must never re-open the server-side mock route",
  );
});

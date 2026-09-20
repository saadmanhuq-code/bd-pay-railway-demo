// SEC-01 strict boolean parse for the mock opt-in env vars.
//
// This is a dependency-free leaf module (NO imports — in particular it does NOT
// import next/server) so it is safe to share between the SERVER guard
// (src/lib/mock/guard.ts, which reads BDPAY_ENABLE_MOCK) and the CLIENT base
// resolver (src/lib/api/client.ts, which reads NEXT_PUBLIC_BDPAY_ENABLE_MOCK)
// without dragging any server-only code into the browser bundle.
//
// Both consumers used to test truthiness of the raw env string. That is unsafe:
// the natural "disable" inputs BDPAY_ENABLE_MOCK=0 and =false are non-empty
// strings and therefore TRUTHY, which would leave the passwordless mock route
// ENABLED when the operator clearly meant to close it. This parser flips that:
// the mock is enabled ONLY for an explicit affirmative value; everything else —
// including "0", "false", "off", "no", "", and unset — means DISABLED.

const TRUTHY = new Set(["1", "true", "yes", "on"]);

/**
 * Strict boolean parse of a mock opt-in env var. Returns true ONLY when the
 * value (trimmed, lowercased) is one of "1", "true", "yes", "on". Any other
 * value — including "0", "false", "off", "no", "", and undefined — returns
 * false (mock DISABLED). Never treats an arbitrary non-empty string as enabled.
 */
export function isMockEnabled(value: string | undefined): boolean {
  if (value == null) return false;
  return TRUTHY.has(value.trim().toLowerCase());
}

/**
 * D04 — the pure decision half of the SEC-01 production guard for the
 * in-app deterministic mock server (see mock/guard.ts for the
 * NextResponse-shaped wrapper every app's src/lib/mock/guard.ts re-exports).
 * Kept in this file, rather than a separate module, so it can reuse
 * `isMockEnabled` with no import statement at all — `mock/guard.ts` imports
 * `next/server`, so it cannot be compiled by this package's dependency-free
 * `tsconfig.test.json`; this decision can.
 *
 * True when the mock routes under app/api/mock/** must refuse the request: a
 * production build (`NODE_ENV === "production"`) without an explicit opt-in
 * via the server-only `BDPAY_ENABLE_MOCK`. The public
 * `NEXT_PUBLIC_BDPAY_ENABLE_MOCK` is deliberately NOT read here — a
 * public/client-exposed build var must never re-open a server-side
 * passwordless auth bypass. Both default off in production.
 */
export function mockRouteRefused(env: NodeJS.ProcessEnv = process.env): boolean {
  return env.NODE_ENV === "production" && !isMockEnabled(env.BDPAY_ENABLE_MOCK);
}

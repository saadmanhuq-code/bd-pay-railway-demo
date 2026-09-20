// Edge-safe signed session cookie for the ops-console deterministic mock.
// The mock API remains disabled in production unless BDPAY_ENABLE_MOCK is
// explicitly set, but the cookie itself still uses a real HMAC and expiry.
//
// Thin per-app instance of the shared factory in @bdpay/edge/mock/signed-session
// (see packages/edge/tests/unit/signed-session.test.mjs for the oracle test
// proving this produces byte-identical tokens to the original implementation).

import { createSignedSession, type CookieReader } from "@bdpay/edge/mock/signed-session";

interface OpsSessionExtra {
  sub: string;
}

const session = createSignedSession<OpsSessionExtra>({
  cookieName: "bdpay_ops_session",
  ttlSeconds: 8 * 60 * 60,
  secretEnvVars: ["BDPAY_SESSION_SECRET", "BDPAY_MOCK_SESSION_SECRET"],
  devSecret: "bdpay-ops-console-dev-session-secret-v1",
  parseExtra: (raw) => (typeof raw.sub === "string" && raw.sub.startsWith("oper_") ? { sub: raw.sub } : null),
});

export const OPS_SESSION_COOKIE = session.COOKIE_NAME;
export const OPS_SESSION_TTL_SECONDS = session.TTL_SECONDS;

export async function issueOpsSession(operatorId: string, issuedAt?: number): Promise<string | null> {
  return session.issue({ sub: operatorId }, issuedAt);
}

export async function verifyOpsSession(
  token: string,
  at?: number,
): Promise<{ sub: string; iat: number; exp: number } | null> {
  return session.verify(token, at);
}

export async function hasValidOpsSession(req: CookieReader): Promise<boolean> {
  return session.hasValid(req);
}

export function opsSessionCookieOptions() {
  return session.cookieOptions();
}

export function clearOpsSessionCookieOptions() {
  return session.clearCookieOptions();
}

// Edge-safe signed session cookie for the diner-app deterministic mock.
// The cookie stands in for the platform customer JWT while still carrying a
// real HMAC and expiry instead of a static sentinel value.
//
// Thin per-app instance of the shared factory in @bdpay/edge/mock/signed-session
// (see packages/edge/tests/unit/signed-session.test.mjs for the oracle test
// proving this produces byte-identical tokens to the original implementation).

import { createSignedSession, type CookieReader } from "@bdpay/edge/mock/signed-session";

interface DinerSessionExtra {
  sub: string;
}

const session = createSignedSession<DinerSessionExtra>({
  cookieName: "bdpay_diner_session",
  ttlSeconds: 12 * 60 * 60,
  secretEnvVars: ["BDPAY_SESSION_SECRET", "BDPAY_MOCK_SESSION_SECRET"],
  devSecret: "bdpay-diner-app-dev-session-secret-v1",
  parseExtra: (raw) => (typeof raw.sub === "string" && raw.sub.startsWith("cust_") ? { sub: raw.sub } : null),
});

export const DINER_SESSION_COOKIE = session.COOKIE_NAME;
export const DINER_SESSION_TTL_SECONDS = session.TTL_SECONDS;

export async function issueDinerSession(customerId: string, issuedAt?: number): Promise<string | null> {
  return session.issue({ sub: customerId }, issuedAt);
}

export async function verifyDinerSession(
  token: string,
  at?: number,
): Promise<{ sub: string; iat: number; exp: number } | null> {
  return session.verify(token, at);
}

export async function hasValidDinerSession(req: CookieReader): Promise<boolean> {
  return session.hasValid(req);
}

export function dinerSessionCookieOptions() {
  return session.cookieOptions();
}

export function clearDinerSessionCookieOptions() {
  return session.clearCookieOptions();
}

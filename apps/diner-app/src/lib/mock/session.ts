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
  /** Last 3 digits of the verified mobile, for the masked "01•••••••NNN"
   * header only (already shown on screen; never the full number). Optional
   * so tokens issued without it stay byte-identical to the original format. */
  ph?: string;
}

const session = createSignedSession<DinerSessionExtra>({
  cookieName: "bdpay_diner_session",
  ttlSeconds: 12 * 60 * 60,
  secretEnvVars: ["BDPAY_SESSION_SECRET", "BDPAY_MOCK_SESSION_SECRET"],
  devSecret: "bdpay-diner-app-dev-session-secret-v1",
  parseExtra: (raw) => {
    if (typeof raw.sub !== "string" || !raw.sub.startsWith("cust_")) return null;
    return typeof raw.ph === "string" && /^\d{3}$/.test(raw.ph) ? { sub: raw.sub, ph: raw.ph } : { sub: raw.sub };
  },
});

export const DINER_SESSION_COOKIE = session.COOKIE_NAME;
export const DINER_SESSION_TTL_SECONDS = session.TTL_SECONDS;

export async function issueDinerSession(
  customerId: string,
  issuedAt?: number,
  phoneLast3?: string,
): Promise<string | null> {
  return session.issue(phoneLast3 && /^\d{3}$/.test(phoneLast3) ? { sub: customerId, ph: phoneLast3 } : { sub: customerId }, issuedAt);
}

/** Verified session payload from the request cookie, or null. */
export async function readDinerSession(req: CookieReader): Promise<{ sub: string; ph?: string; iat: number; exp: number } | null> {
  return session.readPayload(req);
}

export async function verifyDinerSession(
  token: string,
  at?: number,
): Promise<{ sub: string; ph?: string; iat: number; exp: number } | null> {
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

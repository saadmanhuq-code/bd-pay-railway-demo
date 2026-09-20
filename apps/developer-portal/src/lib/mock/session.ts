// Edge-safe signed session cookie for the developer-portal deterministic mock.
// The payload preserves the existing merchant persona behavior, but the cookie
// is no longer client-editable state.
//
// Thin per-app instance of the shared factory in @bdpay/edge/mock/signed-session
// (see packages/edge/tests/unit/signed-session.test.mjs for the oracle test
// proving this produces byte-identical tokens to the original implementation).

import { createSignedSession, type CookieReader } from "@bdpay/edge/mock/signed-session";

export type PortalPersona = "owner" | "developer" | "finance_maker" | "finance_checker";

interface PortalSessionExtra {
  persona: PortalPersona;
}

const VALID_PERSONAS = new Set<string>(["owner", "developer", "finance_maker", "finance_checker"]);

function isPortalPersona(value: unknown): value is PortalPersona {
  return typeof value === "string" && VALID_PERSONAS.has(value);
}

const session = createSignedSession<PortalSessionExtra>({
  cookieName: "bdpay_portal_session",
  ttlSeconds: 8 * 60 * 60,
  secretEnvVars: ["BDPAY_SESSION_SECRET", "BDPAY_MOCK_SESSION_SECRET"],
  devSecret: "bdpay-developer-portal-dev-session-secret-v1",
  parseExtra: (raw) => (isPortalPersona(raw.persona) ? { persona: raw.persona } : null),
});

export const PORTAL_SESSION_COOKIE = session.COOKIE_NAME;
export const PORTAL_SESSION_TTL_SECONDS = session.TTL_SECONDS;

export async function issuePortalSession(persona: PortalPersona, issuedAt?: number): Promise<string | null> {
  return session.issue({ persona }, issuedAt);
}

export async function verifyPortalSession(
  token: string,
  at?: number,
): Promise<{ persona: PortalPersona; iat: number; exp: number } | null> {
  return session.verify(token, at);
}

export async function readPortalSessionPersona(req: CookieReader): Promise<PortalPersona | null> {
  const payload = await session.readPayload(req);
  return payload?.persona ?? null;
}

export async function hasValidPortalSession(req: CookieReader): Promise<boolean> {
  return session.hasValid(req);
}

export function portalSessionCookieOptions() {
  return session.cookieOptions();
}

export function clearPortalSessionCookieOptions() {
  return session.clearCookieOptions();
}

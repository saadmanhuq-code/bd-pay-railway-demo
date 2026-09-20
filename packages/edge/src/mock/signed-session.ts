// D03 — the edge-safe HMAC-signed session cookie shared by all three apps'
// deterministic mock login (dp: bdpay_portal_session, ops: bdpay_ops_session,
// diner: bdpay_diner_session — see each app's src/lib/mock/session.ts).
//
// This module is PURE: no imports at all, Web Crypto (`crypto.subtle`) only,
// so it runs the same in the Node runtime and at the edge. It reproduces,
// byte-for-byte, what used to be three independent copies of the same token
// format, HMAC construction, base64url encoding and cookie option values —
// see packages/edge/tests/unit/signed-session.test.mjs, which issues a token
// through this factory and compares it against each original app
// implementation's function bodies captured as a fixture oracle.
//
// Token format: "v1.<base64url(JSON payload)>.<base64url(HMAC-SHA256 sig)>",
// where the signed message is "v1.<base64url(JSON payload)>". The payload is
// always `{ ...extra, iat, exp }` — `extra` is whatever app-specific fields
// `parseExtra` validates (dp: persona, ops/diner: sub), `iat`/`exp` are
// integer unix seconds validated here, identically, by every app today.

export interface CookieReader {
  cookies: {
    get(name: string): { value: string } | undefined;
  };
}

export interface SignedSessionCookieOptions {
  httpOnly: true;
  sameSite: "lax";
  path: "/";
  maxAge: number;
  secure: boolean;
}

export interface SignedSessionClearCookieOptions {
  httpOnly: true;
  sameSite: "lax";
  path: "/";
  maxAge: 0;
  secure: boolean;
}

export interface SignedSessionConfig<TExtra extends object> {
  /** The cookie name this instance issues/reads/clears. */
  cookieName: string;
  /** Session lifetime in seconds; also the cookie's `maxAge`. */
  ttlSeconds: number;
  /**
   * The env var names read (in order, first non-empty wins) for the signing
   * secret, exactly as each app reads them today — every app currently reads
   * the same pair, `["BDPAY_SESSION_SECRET", "BDPAY_MOCK_SESSION_SECRET"]`.
   */
  secretEnvVars: readonly [string, string];
  /** The fixed development fallback secret, used only outside production
   * and only when neither secret env var is configured. */
  devSecret: string;
  /**
   * Validates the app-specific payload fields from the parsed JSON body
   * (already known to be a non-null object). Returns the validated extra
   * fields, or null to reject the token. `iat`/`exp` are validated
   * separately by this factory, identically for every app.
   */
  parseExtra: (raw: Record<string, unknown>) => TExtra | null;
}

export interface SignedSessionInstance<TExtra extends object> {
  COOKIE_NAME: string;
  TTL_SECONDS: number;
  issue(extra: TExtra, issuedAt?: number): Promise<string | null>;
  verify(token: string, at?: number): Promise<(TExtra & { iat: number; exp: number }) | null>;
  hasValid(req: CookieReader): Promise<boolean>;
  /** Verifies the cookie on `req` and returns the full payload, or null. */
  readPayload(req: CookieReader): Promise<(TExtra & { iat: number; exp: number }) | null>;
  cookieOptions(): SignedSessionCookieOptions;
  clearCookieOptions(): SignedSessionClearCookieOptions;
}

const TOKEN_VERSION = "v1";

const textEncoder = new TextEncoder();
const textDecoder = new TextDecoder();

function nowSeconds(): number {
  return Math.floor(Date.now() / 1000);
}

function bytesToBase64Url(bytes: Uint8Array): string {
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
}

function base64UrlToBytes(value: string): Uint8Array | null {
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

function encodeText(value: string): string {
  return bytesToBase64Url(textEncoder.encode(value));
}

function decodeText(value: string): string | null {
  const bytes = base64UrlToBytes(value);
  return bytes === null ? null : textDecoder.decode(bytes);
}

async function hmac(payload: string, secret: string): Promise<string> {
  const key = await crypto.subtle.importKey(
    "raw",
    textEncoder.encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const signature = await crypto.subtle.sign("HMAC", key, textEncoder.encode(payload));
  return bytesToBase64Url(new Uint8Array(signature));
}

function constantTimeEqual(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i += 1) {
    diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  }
  return diff === 0;
}

/**
 * Builds one signed-session instance (cookie name, TTL, issue/verify/etc.)
 * for a given app's payload shape. See the module doc comment above for the
 * exact token format every app must keep producing.
 */
export function createSignedSession<TExtra extends object>(
  config: SignedSessionConfig<TExtra>,
): SignedSessionInstance<TExtra> {
  const { cookieName, ttlSeconds, secretEnvVars, devSecret, parseExtra } = config;

  function sessionSecret(): string {
    const [first, second] = secretEnvVars;
    const configured = process.env[first]?.trim() || process.env[second]?.trim();
    if (configured) return configured;
    if (process.env.NODE_ENV === "production") return "";
    return devSecret;
  }

  function parsePayload(raw: string): (TExtra & { iat: number; exp: number }) | null {
    try {
      const parsed = JSON.parse(raw) as Record<string, unknown>;
      if (parsed === null || typeof parsed !== "object") return null;
      const extra = parseExtra(parsed);
      if (extra === null) return null;
      if (typeof parsed.iat !== "number" || !Number.isInteger(parsed.iat)) return null;
      if (typeof parsed.exp !== "number" || !Number.isInteger(parsed.exp)) return null;
      return { ...extra, iat: parsed.iat, exp: parsed.exp } as TExtra & { iat: number; exp: number };
    } catch {
      return null;
    }
  }

  async function issue(extra: TExtra, issuedAt = nowSeconds()): Promise<string | null> {
    const secret = sessionSecret();
    if (!secret) return null;
    const payload = { ...extra, iat: issuedAt, exp: issuedAt + ttlSeconds };
    const encodedPayload = encodeText(JSON.stringify(payload));
    const signature = await hmac(`${TOKEN_VERSION}.${encodedPayload}`, secret);
    return `${TOKEN_VERSION}.${encodedPayload}.${signature}`;
  }

  async function verify(token: string, at = nowSeconds()): Promise<(TExtra & { iat: number; exp: number }) | null> {
    const secret = sessionSecret();
    if (!secret) return null;
    const [version, encodedPayload, signature, extraPart] = token.split(".");
    if (version !== TOKEN_VERSION || !encodedPayload || !signature || extraPart !== undefined) {
      return null;
    }
    const expected = await hmac(`${version}.${encodedPayload}`, secret);
    if (!constantTimeEqual(signature, expected)) return null;
    const rawPayload = decodeText(encodedPayload);
    if (rawPayload === null) return null;
    const payload = parsePayload(rawPayload);
    if (payload === null) return null;
    if (payload.iat > at || payload.exp <= at) return null;
    return payload;
  }

  async function readPayload(req: CookieReader): Promise<(TExtra & { iat: number; exp: number }) | null> {
    const token = req.cookies.get(cookieName)?.value;
    return token ? verify(token) : null;
  }

  async function hasValid(req: CookieReader): Promise<boolean> {
    return (await readPayload(req)) !== null;
  }

  function cookieOptions(): SignedSessionCookieOptions {
    return {
      httpOnly: true,
      sameSite: "lax",
      path: "/",
      maxAge: ttlSeconds,
      secure: process.env.NODE_ENV === "production",
    };
  }

  function clearCookieOptions(): SignedSessionClearCookieOptions {
    return {
      httpOnly: true,
      sameSite: "lax",
      path: "/",
      maxAge: 0,
      secure: process.env.NODE_ENV === "production",
    };
  }

  return {
    COOKIE_NAME: cookieName,
    TTL_SECONDS: ttlSeconds,
    issue,
    verify,
    hasValid,
    readPayload,
    cookieOptions,
    clearCookieOptions,
  };
}

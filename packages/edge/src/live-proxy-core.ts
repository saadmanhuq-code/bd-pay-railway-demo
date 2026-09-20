// D01 — the live gateway BFF proxy core, shared by all three apps'
// app/api/live/[...path]/route.ts handlers.
//
// This module is PURE: no `next` import, no framework dependency at all — it
// only reads `process.env` (overridable via an explicit `env` param for
// tests) and builds plain strings/objects. `live-proxy.ts` in this same
// package wraps these with the NextRequest/NextResponse-shaped helpers; a
// route that only needs a pure helper (building a URL, mapping a status to an
// error envelope) can import straight from here without pulling in
// `next/server`.

/** Reads BDPAY_LIVE_API_BASE (preferred) or BDPAY_GATEWAY_INTERNAL_BASE, with
 * any trailing slashes stripped. Returns "" when neither is configured. */
export function gatewayBase(env: NodeJS.ProcessEnv = process.env): string {
  return (env.BDPAY_LIVE_API_BASE || env.BDPAY_GATEWAY_INTERNAL_BASE || "").replace(/\/+$/, "");
}

/** Reads the shared demo API key used to authenticate this BFF's own server
 * -to-gateway calls. Returns "" when unconfigured. */
export function demoApiKey(env: NodeJS.ProcessEnv = process.env): string {
  return env.BDPAY_LIVE_DEMO_API_KEY || "";
}

/**
 * Joins `path` onto the configured gateway base and applies `search` (an
 * already-encoded query string, e.g. from `req.nextUrl.search`, including its
 * leading "?" or ""). Returns null when no gateway base is configured — the
 * caller is expected to turn that into a 500 (see `gatewayBaseMissing()` in
 * `live-proxy.ts`).
 */
export function gatewayUrl(path: string[], search = ""): string | null {
  const base = gatewayBase();
  if (!base) return null;
  const url = new URL(`/${path.join("/")}`, `${base}/`);
  url.search = search;
  return url.toString();
}

/** Upstream response headers this BFF passes through to its own caller. */
export const PASSTHROUGH_HEADERS: readonly string[] = [
  "content-type",
  "cache-control",
  "x-bdpay-api-version",
  "x-bdpay-request-id",
];

/** Copies exactly `names` (default `PASSTHROUGH_HEADERS`) from `upstream`
 * into a fresh `Headers`, skipping any that are absent. Every other upstream
 * header (notably `set-cookie`) is deliberately dropped. */
export function copyHeaders(upstream: Response, names: readonly string[] = PASSTHROUGH_HEADERS): Headers {
  const headers = new Headers();
  for (const name of names) {
    const value = upstream.headers.get(name);
    if (value) headers.set(name, value);
  }
  return headers;
}

/**
 * The error `type` this BFF reports for a given HTTP status. A superset of
 * every mapping used by any of the three apps' local `error()` — ops-console
 * only ever emits 401/404/500 and diner-app only 400/401/404/500, so this
 * table reproduces every existing call site's output unchanged.
 */
export function errorType(status: number): string {
  switch (status) {
    case 401:
      return "authentication";
    case 403:
      return "authorization";
    case 404:
      return "not_found";
    case 400:
      return "invalid_request";
    default:
      return "internal";
  }
}

export interface ErrorEnvelope {
  error: {
    type: string;
    code: string;
    message: string;
    request_id: string;
    doc_url: string;
  };
}

/** The shared error response body every app's live BFF returns. */
export function errorEnvelope(status: number, code: string, message: string): ErrorEnvelope {
  return {
    error: {
      type: errorType(status),
      code,
      message,
      request_id: "frontend_live_adapter",
      doc_url: `https://docs.bdpay.example/errors/${code}`,
    },
  };
}

/** The 404 every app's live route falls back to for a path it does not
 * recognize. */
export const UNKNOWN_ROUTE = { code: "unknown_route", message: "Unknown live route." } as const;

/** The 500 every app's live route returns when no gateway base URL is
 * configured. */
export const GATEWAY_BASE_MISSING = {
  code: "live_gateway_base_missing",
  message: "Live gateway base URL is not configured.",
} as const;

/** The 500 returned when the shared demo API key is required but
 * unconfigured. */
export const DEMO_API_KEY_MISSING = {
  code: "live_demo_api_key_missing",
  message: "Live demo API key is not configured.",
} as const;

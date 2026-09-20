// D01 — the NextRequest/NextResponse-shaped half of the live gateway BFF
// proxy core (see live-proxy-core.ts for the pure helpers this wraps).

import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";
import {
  GATEWAY_BASE_MISSING,
  UNKNOWN_ROUTE,
  copyHeaders,
  errorEnvelope,
  gatewayUrl,
} from "./live-proxy-core";

/** Builds the shared error envelope as a NextResponse. */
export function error(status: number, code: string, message: string): NextResponse {
  return NextResponse.json(errorEnvelope(status, code, message), { status });
}

/** The 404 every app's live route falls back to for an unrecognized path. */
export function unknownRoute(): NextResponse {
  return error(404, UNKNOWN_ROUTE.code, UNKNOWN_ROUTE.message);
}

/** The 500 every app's live route returns when no gateway base is
 * configured. */
export function gatewayBaseMissing(): NextResponse {
  return error(500, GATEWAY_BASE_MISSING.code, GATEWAY_BASE_MISSING.message);
}

/** Turns a raw upstream `Response` into a NextResponse carrying its body,
 * status and passthrough headers (see `copyHeaders` in live-proxy-core.ts). */
export function passthrough(upstream: Response): NextResponse {
  return new NextResponse(upstream.body, {
    status: upstream.status,
    headers: copyHeaders(upstream),
  });
}

export interface ProxyGatewayOptions {
  /** Bearer token to send upstream. Omitted entirely means no Authorization
   * header is sent (the developer-portal's public-status routes proxy with
   * no token at all). */
  token?: string;
}

/**
 * Forwards `req` to the gateway at `path` and streams the upstream response
 * straight back as a NextResponse (status + the shared passthrough headers).
 *
 * `method: req.method` below is equivalent to every existing call site's
 * former literal `"GET"` — proxyGateway is only ever reached from a route's
 * `GET` handler today — written this way so it stays correct if a future
 * route ever forwards a different verb through it.
 */
export async function proxyGateway(
  req: NextRequest,
  path: string[],
  opts: ProxyGatewayOptions = {},
): Promise<NextResponse> {
  const url = gatewayUrl(path, req.nextUrl.search);
  if (url === null) return gatewayBaseMissing();
  const headers: Record<string, string> = {
    Accept: req.headers.get("accept") || "application/json",
  };
  if (opts.token) headers.Authorization = `Bearer ${opts.token}`;
  const upstream = await fetch(url, {
    method: req.method,
    cache: "no-store",
    headers,
  });
  return passthrough(upstream);
}

/**
 * Fetches `path` from the gateway as JSON: returns the parsed body on a 2xx
 * response, or a passthrough NextResponse for any other status (so the
 * caller can render the upstream error instead of fabricating one).
 */
export async function gatewayJson(
  req: NextRequest,
  path: string[],
  opts: ProxyGatewayOptions = {},
): Promise<Record<string, unknown> | NextResponse> {
  const url = gatewayUrl(path, req.nextUrl.search);
  if (url === null) return gatewayBaseMissing();
  const headers: Record<string, string> = { Accept: "application/json" };
  if (opts.token) headers.Authorization = `Bearer ${opts.token}`;
  const upstream = await fetch(url, {
    method: "GET",
    cache: "no-store",
    headers,
  });
  if (!upstream.ok) return passthrough(upstream);
  return (await upstream.json()) as Record<string, unknown>;
}

// D02 — the NextRequest/NextResponse-shaped half of the edge auth + CSP
// proxy (see edge-proxy-core.ts for the pure helpers this wraps).
//
// Each app's proxy.ts calls createEdgeProxy(...) with exactly its own
// session predicate and public-page allow-list, and re-exports the result as
// its `proxy` function. Two jobs, both on the edge before any page renders:
//
//   1. EDGE AUTH (fail-closed). Every page route is denied unless the
//      request carries a valid session cookie, EXCEPT the app's public
//      allow-list. An unauthenticated request to a protected page is
//      redirected (307) to /login. This is a coarse EDGE gate layered on top
//      of the API's own per-request auth; it is defense-in-depth, not a
//      replacement for it.
//
//   2. CONTENT-SECURITY-POLICY with a per-request nonce. A fresh nonce is
//      minted per request and threaded into script-src; it is forwarded on
//      the request Content-Security-Policy header so Next.js stamps it onto
//      its inline hydration scripts (which 'strict-dynamic' would otherwise
//      block). x-nonce is also forwarded for app code that wants to read it
//      via next/headers. Requires every route to be dynamically rendered
//      (`export const dynamic = "force-dynamic"` in the app's root layout);
//      a statically prerendered page is built before any request exists and
//      so cannot carry a per-request nonce.

import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";
import { buildCsp, isPublicPage, mintNonce } from "./edge-proxy-core";

export interface EdgeProxyConfig {
  /** The app's own session check (e.g. hasValidPortalSession). */
  hasSession: (req: NextRequest) => Promise<boolean>;
  /** Pathnames public for an exact match only. */
  publicExact: string[];
  /** Pathname prefixes public for themselves and anything under them. */
  publicPrefixes: string[];
}

/** Builds the app's `proxy(req)` export from its session predicate and
 * public-page allow-list. */
export function createEdgeProxy(config: EdgeProxyConfig): (req: NextRequest) => Promise<NextResponse> {
  const { hasSession, publicExact, publicPrefixes } = config;

  return async function proxy(req: NextRequest): Promise<NextResponse> {
    const { pathname } = req.nextUrl;

    const nonce = mintNonce();
    const csp = buildCsp(nonce);

    // Edge auth (fail-closed): a non-public page with no valid session → /login.
    if (!isPublicPage(pathname, { exact: publicExact, prefixes: publicPrefixes }) && !(await hasSession(req))) {
      const url = req.nextUrl.clone();
      url.pathname = "/login";
      url.search = "";
      const redirect = NextResponse.redirect(url, 307);
      redirect.headers.set("Content-Security-Policy", csp);
      return redirect;
    }

    // Forward the nonce AND the CSP on the REQUEST headers. Next.js reads the
    // nonce out of the request's Content-Security-Policy header and
    // automatically stamps it onto every inline <script> it injects (the
    // RSC/hydration bootstrap). Without this request header Next emits
    // un-nonced inline scripts which 'strict-dynamic' then blocks → broken
    // hydration. The x-nonce header is a convenience for app code that wants
    // to read it via next/headers.
    const requestHeaders = new Headers(req.headers);
    requestHeaders.set("x-nonce", nonce);
    requestHeaders.set("Content-Security-Policy", csp);

    const res = NextResponse.next({ request: { headers: requestHeaders } });
    res.headers.set("Content-Security-Policy", csp);
    return res;
  };
}

// The matcher every app's proxy.ts declares as a literal `export const config
// = { matcher: [...] }` (Next parses that export statically, so it cannot be
// computed from an imported value) — kept here so a change to one is a
// deliberate change to both. Each app's proxy.ts matcher must equal this
// string exactly; see the comment at its own `config` export.
//
// Run on every page route, but NEVER on the API (it self-authenticates and
// owns the login/logout/session endpoints — redirecting /api would make it
// impossible to ever set the session cookie) or on Next.js internals/static
// assets. This matcher is the infra allow-list; the page allow-list passed to
// createEdgeProxy() is the auth allow-list.
//
// Exclusion-token shapes are deliberate, NOT uniform:
//   - `api/` keeps its trailing-slash BOUNDARY: it excludes only the real
//     /api/ tree, so a same-prefix PAGE like /api-keys still runs the auth
//     check (the bare `api` lookahead the first cut used wrongly skipped it).
//   - `_next/` excludes the ENTIRE reserved Next.js namespace — static chunks
//     (/_next/static/…), the image optimizer, and /_next/data. The optimizer
//     is served at the BARE path /_next/image (with a ?url=…&w=…&q=… query),
//     so a trailing-slash `_next/image/` token would FAIL to match it and the
//     optimizer would be auth-gated → broken/blocked images. Excluding the
//     whole `_next/` prefix is correct because no real app page can live
//     under the reserved /_next/ namespace. (Matchers see only the pathname;
//     the ?url=… query is never part of the match, so the bare-path
//     exclusion covers the queried form too.)
//   - favicon.ico and `.well-known/` are the remaining non-page internals.
export const EDGE_PROXY_MATCHER = "/((?!api/|_next/|favicon.ico|.well-known/).*)";

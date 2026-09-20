// SEC-03 edge auth + Content-Security-Policy proxy (developer-portal).
//
// The CSP + fail-closed auth machinery is shared (@bdpay/edge/edge-proxy —
// see its own header comment for the two jobs it does). This file is only
// what is genuinely per-app: WHICH pages are public, and how a session is
// verified.
//
// SESSION MODEL (mirrors app/api/mock/v1/[...path]/route.ts): the portal session
// cookie is an HMAC-signed, expiring token whose payload carries the merchant
// persona (one of owner / developer / finance_maker / finance_checker). The
// API is the source of truth for minting the token; this edge check verifies
// the same signature + expiry before any protected page renders.

import type { NextRequest } from "next/server";
import { createEdgeProxy } from "@bdpay/edge/edge-proxy";
import { hasValidPortalSession } from "@/lib/mock/session";

// --- per-app public allow-list ----------------------------------------------
// Genuinely public, pre-auth surfaces of the merchant developer portal:
//   /login         — auth entry
//   /signup        — merchant signup (pre-auth)
//   /sandbox-signup— public self-serve sandbox signup (PublicShell, no auth)
//   /status        — public connector-health status page (PublicShell, no auth)
//   /onboarding    — pre-auth applicant tracking (API allows pre-auth lookup)
//   /pay           — public payment-link landing /pay/l/{code} (PublicShell)
const PUBLIC_EXACT = ["/login", "/signup", "/sandbox-signup", "/status", "/onboarding"];
const PUBLIC_PREFIXES = ["/pay"];

export const proxy = createEdgeProxy({
  hasSession: (req: NextRequest) => hasValidPortalSession(req),
  publicExact: PUBLIC_EXACT,
  publicPrefixes: PUBLIC_PREFIXES,
});

// This matcher MUST equal @bdpay/edge/edge-proxy's EDGE_PROXY_MATCHER
// exactly. It is written out as a literal string here — not imported —
// because Next parses `config.matcher` statically at build time and cannot
// resolve a value coming from an import; see EDGE_PROXY_MATCHER's own
// comment in the package for what each exclusion token does and why.
export const config = {
  matcher: ["/((?!api/|_next/|favicon.ico|.well-known/).*)"],
};

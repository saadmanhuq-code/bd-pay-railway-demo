// SEC-03 edge auth + Content-Security-Policy proxy (diner-app).
//
// The CSP + fail-closed auth machinery is shared (@bdpay/edge/edge-proxy —
// see its own header comment for the two jobs it does). This file is only
// what is genuinely per-app: WHICH pages are public, and how a session is
// verified.
//
// SESSION MODEL (mirrors app/api/mock/[...path]/route.ts): the diner session
// cookie is an HMAC-signed, expiring mock customer session token. The API is
// the source of truth for minting the token; this edge check verifies the
// same signature + expiry before any protected page renders.

import type { NextRequest } from "next/server";
import { createEdgeProxy } from "@bdpay/edge/edge-proxy";
import { hasValidDinerSession } from "@/lib/mock/session";

// --- per-app public allow-list ----------------------------------------------
// Browse (/) and merchant detail (/merchants/*) are public so diners can
// discover deals before signing in. Login stays public; pay/history stay gated.
// Public discovery (EatClub-style): browse + merchant detail before login.
// Redemption still requires a session (API-enforced on reserve/pay).
const PUBLIC_EXACT = ["/", "/login"];
const PUBLIC_PREFIXES: string[] = ["/merchants", "/data"];

export const proxy = createEdgeProxy({
  hasSession: (req: NextRequest) => hasValidDinerSession(req),
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

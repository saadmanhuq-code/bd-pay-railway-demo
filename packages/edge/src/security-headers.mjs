// D03 — the static, request-independent security response headers applied to
// every route via each app's next.config.mjs `headers()` hook.
//
// Plain ESM (not TypeScript): next.config.mjs is loaded directly by Node, not
// compiled by the Next/TypeScript toolchain, so it can only import a plain
// .mjs module — not a shared .ts helper.
//
// The one header that must be request-scoped — the Content-Security-Policy,
// whose script-src carries a per-request nonce — is set in each app's
// proxy.ts instead (via @bdpay/edge/edge-proxy); a static config value cannot
// mint a fresh nonce per request. Everything here is safe to bake in at
// build time.

export const SECURITY_HEADERS = [
  // Force HTTPS for two years incl. subdomains; eligible for the preload list.
  // Harmless when served over plain HTTP (browsers ignore it on http://).
  { key: "Strict-Transport-Security", value: "max-age=63072000; includeSubDomains; preload" },
  // Defense-in-depth clickjacking block (legacy header). The CSP set in
  // proxy also carries frame-ancestors 'none' (the modern equivalent).
  { key: "X-Frame-Options", value: "DENY" },
  // Disable MIME sniffing.
  { key: "X-Content-Type-Options", value: "nosniff" },
  // Send only the origin on cross-origin navigation; full URL same-origin.
  { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
  // Lock down powerful browser features this app does not use.
  { key: "Permissions-Policy", value: "camera=(), microphone=(), geolocation=(), payment=()" },
];

/** The single `headers()` route entry every app's next.config.mjs returns. */
export function securityHeadersRoute() {
  return { source: "/:path*", headers: SECURITY_HEADERS };
}

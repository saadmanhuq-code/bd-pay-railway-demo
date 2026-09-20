// D02 — the pure half of the edge auth + Content-Security-Policy proxy every
// app runs (as `proxy.ts`, Next 16's successor to `middleware.ts`) before any
// page renders. No `next` import — `edge-proxy.ts` in this same package
// wraps these with the actual NextRequest/NextResponse-shaped `proxy(req)`
// function each app's proxy.ts exports.

export interface PublicPageConfig {
  /** Pathnames that are public for an EXACT match only (e.g. "/login"). */
  exact: readonly string[];
  /** Pathname prefixes that are public for the prefix itself and anything
   * under it (e.g. "/pay" covers "/pay" and "/pay/l/abc", but never a
   * same-prefix sibling like "/payments"). */
  prefixes: readonly string[];
}

/**
 * True when `pathname` is a genuinely public, pre-auth page per `config` —
 * an ALLOW-list, not a deny-list, so a route added in the future is
 * protected by default (the fail-closed property `edge-proxy.ts` relies on).
 */
export function isPublicPage(pathname: string, config: PublicPageConfig): boolean {
  if (config.exact.includes(pathname)) return true;
  return config.prefixes.some((prefix) => pathname === prefix || pathname.startsWith(`${prefix}/`));
}

/**
 * The origin the browser fetch client needs added to `connect-src` when the
 * gateway lives on a different origin than the app itself. Reads
 * NEXT_PUBLIC_BDPAY_API_BASE so the policy stays in lockstep with where the
 * client actually fetches. Returns "" for an unset, relative, or unparsable
 * base — 'self' already covers a same-origin gateway, and no addition is
 * needed.
 *
 * The default MUST stay the literal expression
 * `process.env.NEXT_PUBLIC_BDPAY_API_BASE`: Next inlines NEXT_PUBLIC_* only
 * where that exact member expression appears in source, and it does so for
 * the proxy bundle just as for the browser bundle. Reading it through an
 * `env` object (e.g. `env.NEXT_PUBLIC_BDPAY_API_BASE` with `env =
 * process.env`) compiles to a runtime lookup instead, which is empty in any
 * deployment that sets NEXT_PUBLIC_* at build time only — and the browser's
 * gateway fetches would then be blocked by this very CSP. Verified against
 * the built proxy chunk on 2026-09-07 (lane 332). Tests pass the base
 * explicitly.
 */
export function gatewayConnectOrigin(base: string | undefined = process.env.NEXT_PUBLIC_BDPAY_API_BASE): string {
  if (!base) return "";
  try {
    return new URL(base).origin; // absolute URL → scheme://host[:port]
  } catch {
    return ""; // relative/blank base is same-origin; 'self' already covers it
  }
}

/**
 * The strict, nonce-based Content-Security-Policy every app serves. No
 * 'unsafe-inline' for scripts; 'strict-dynamic' lets the nonced bootstrap
 * script load further app chunks without each needing its own nonce.
 * style-src keeps 'unsafe-inline' because Next/React inject inline styles
 * without a proxy-visible nonce hook; dropping it would break rendering and
 * cannot be earned with a nonce here. `apiBase` defaults to the literal
 * `process.env.NEXT_PUBLIC_BDPAY_API_BASE` for the inlining reason explained
 * on `gatewayConnectOrigin`.
 */
export function buildCsp(nonce: string, apiBase: string | undefined = process.env.NEXT_PUBLIC_BDPAY_API_BASE): string {
  const gatewayOrigin = gatewayConnectOrigin(apiBase);
  const connectSrc = gatewayOrigin ? `connect-src 'self' ${gatewayOrigin}` : "connect-src 'self'";
  return [
    "default-src 'self'",
    `script-src 'self' 'nonce-${nonce}' 'strict-dynamic'`,
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data: blob:",
    "font-src 'self'",
    connectSrc,
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
    "upgrade-insecure-requests",
  ].join("; ");
}

/** Mints a fresh per-request nonce: 16 random bytes, base64-encoded (24
 * characters with padding). */
export function mintNonce(): string {
  return Buffer.from(crypto.randomUUID().replace(/-/g, ""), "hex").toString("base64");
}

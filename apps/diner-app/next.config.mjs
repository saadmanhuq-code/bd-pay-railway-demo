/** @type {import('next').NextConfig} */

// SEC-03 edge security headers.
//
// Static, request-independent response headers applied to EVERY route via the
// Next.js headers() hook. The one header that must be request-scoped — the
// Content-Security-Policy, whose script-src carries a per-request nonce — is set
// in proxy.ts instead (a static config value cannot mint a fresh nonce per
// request). Everything here is safe to bake in at build time.
//
// The SECURITY_HEADERS table is shared across all three apps via
// @bdpay/edge/security-headers (plain .mjs — next.config.mjs is loaded
// directly by Node, not compiled by the Next/TypeScript toolchain, so it can
// only import a plain ESM module, not a shared .ts helper).
import { securityHeadersRoute } from "@bdpay/edge/security-headers";

const nextConfig = {
  reactStrictMode: true,
  // The package has no build step of its own: apps consume it straight from
  // TypeScript/ESM source, so Next must run its own transform over it rather
  // than treating it as a precompiled external dependency.
  transpilePackages: ["@bdpay/edge"],
  async headers() {
    return [securityHeadersRoute()];
  },
};

export default nextConfig;

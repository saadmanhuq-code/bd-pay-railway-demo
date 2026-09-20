// D04 — the shared SEC-01 production guard for the in-app deterministic
// mock server, used identically by all three apps
// (developer-portal/ops-console/diner-app) via
// src/lib/mock/guard.ts -> re-export of `mockDisabled` from this module.
//
// The mock routes under app/api/mock/** stand in for the real BDPay gateway
// during local development. They accept a passwordless login (email/BD
// mobile plus a server-verified mock TOTP/OTP), so they MUST NOT be
// reachable in a production build. This guard is called as the first
// statement of every mock route handler; it returns a 404 NextResponse when
// the pure decision `mockRouteRefused` (in ../env-flag.ts, alongside
// `isMockEnabled` — see that module for the full SEC-01 two-var model) says
// so, else null and the handler proceeds.

import { NextResponse } from "next/server";

import { mockRouteRefused } from "../env-flag";

/**
 * Returns a 404 NextResponse when the mock server is disabled (production
 * without an explicit opt-in via the server-only BDPAY_ENABLE_MOCK), else
 * null.
 */
export function mockDisabled(): NextResponse | null {
  if (mockRouteRefused()) {
    return new NextResponse("Not Found", { status: 404 });
  }
  return null;
}

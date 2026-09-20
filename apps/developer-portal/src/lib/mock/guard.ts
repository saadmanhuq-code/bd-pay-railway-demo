// SEC-01 production guard for the in-app deterministic mock server.
//
// The mock routes under app/api/mock/** stand in for the real BDPay gateway
// during local development. They accept any email plus a server-verified mock
// TOTP with no password, so they MUST NOT be reachable in a production build.
// This guard is called as the first statement of every mock route handler.
//
// Shared implementation: @bdpay/edge/mock/guard (see env-flag.ts there for
// mockRouteRefused, the pure, unit-tested decision this wraps).

export { mockDisabled } from "@bdpay/edge/mock/guard";

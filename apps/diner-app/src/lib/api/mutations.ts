// Mutation manifest — the no-write-guard cross-check source of truth.
// Every non-GET route this app may call MUST be listed here and implemented
// only in src/lib/api/client.ts. Adding a mutation anywhere else fails the
// build (scripts/no-write-guard.mjs).

export interface MutationRoute {
  path: string;
  purpose: string;
}

export const MUTATIONS: MutationRoute[] = [
  { path: "/v1/auth/diner/otp/request", purpose: "D1 login step 1 — send OTP to a BD mobile" },
  { path: "/v1/auth/diner/otp/verify", purpose: "D1 login step 2 — verify OTP, open diner session" },
  { path: "/v1/auth/diner/logout", purpose: "close the diner session" },
  {
    path: "/v1/payment-intents",
    purpose:
      "spec/18 reservation: offer_id + gross_amount_minor -> atomic reserve + net-amount intent",
  },
  {
    path: "/v1/payment-intents/:id/confirm",
    purpose: "confirm the intent (simulated rail in dev mock)",
  },
  {
    path: "/v1/qr-codes/dynamic",
    purpose: "issue the dynamic Bangla QR bound to the net-amount intent (spec/13)",
  },
  {
    path: "/v1/sandbox/demo/diner-pay",
    purpose: "SANDBOX-only one-tap demo: create→confirm→capture against the simulator",
  },
];

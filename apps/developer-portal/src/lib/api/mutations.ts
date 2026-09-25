// Explicit mutation manifest (spec/15 read-only-default rule).
// The portal renders from GET endpoints only; every write below is a named
// POST issued exclusively from client.ts. scripts/no-write-guard.mjs scans
// src/ at build time and fails on any non-GET fetch outside client.ts, and
// cross-checks client.ts against this manifest.

export interface MutationRoute {
  name: string;
  method: "POST";
  path: string; // ":id"-style parameter segments
}

export const MUTATION_MANIFEST: MutationRoute[] = [
  { name: "login", method: "POST", path: "/v1/auth/merchant/login" },
  { name: "logout", method: "POST", path: "/v1/auth/merchant/logout" },
  { name: "signup", method: "POST", path: "/v1/merchants/signup" },
  { name: "uploadKybDocument", method: "POST", path: "/v1/onboarding/applications/:id/documents" },
  { name: "createApiKey", method: "POST", path: "/v1/api-keys" },
  { name: "rotateApiKey", method: "POST", path: "/v1/api-keys/:id/rotate" },
  { name: "revokeApiKey", method: "POST", path: "/v1/api-keys/:id/revoke" },
  { name: "createWebhookEndpoint", method: "POST", path: "/v1/webhook-endpoints" },
  { name: "deleteWebhookEndpoint", method: "POST", path: "/v1/webhook-endpoints/:id/delete" },
  { name: "testWebhookEndpoint", method: "POST", path: "/v1/webhook-endpoints/:id/test" },
  { name: "replayWebhookDelivery", method: "POST", path: "/v1/webhook-deliveries/:id/replay" },
  { name: "issueStaticQr", method: "POST", path: "/v1/merchants/:id/qr-codes" },
  { name: "suspendQr", method: "POST", path: "/v1/qr-codes/:id/suspend" },
  { name: "reactivateQr", method: "POST", path: "/v1/qr-codes/:id/reactivate" },
  { name: "revokeQr", method: "POST", path: "/v1/qr-codes/:id/revoke" },
  { name: "submitDisputeEvidence", method: "POST", path: "/v1/disputes/:id/evidence" },
  { name: "createDisbursementBatch", method: "POST", path: "/v1/disbursement-batches" },
  { name: "approveDisbursementBatch", method: "POST", path: "/v1/disbursement-batches/:id/approve" },
  { name: "rejectDisbursementBatch", method: "POST", path: "/v1/disbursement-batches/:id/reject" },
  { name: "createPaymentLink", method: "POST", path: "/v1/payment-links" },
  { name: "cancelPaymentLink", method: "POST", path: "/v1/payment-links/:id/cancel" },
  { name: "createOffer", method: "POST", path: "/v1/offers" },
  { name: "activateOffer", method: "POST", path: "/v1/offers/:id/activate" },
  { name: "pauseOffer", method: "POST", path: "/v1/offers/:id/pause" },
  { name: "resumeOffer", method: "POST", path: "/v1/offers/:id/resume" },
  { name: "archiveOffer", method: "POST", path: "/v1/offers/:id/archive" },
  { name: "editOffer", method: "POST", path: "/v1/offers/:id/edit" },
  { name: "createPublicLinkIntent", method: "POST", path: "/v1/public/payment-links/:id/intents" },
  {
    name: "simulatePublicLinkIntent",
    method: "POST",
    path: "/v1/public/payment-links/:id/intents/:id/simulate",
  },
  { name: "createSandboxSignup", method: "POST", path: "/v1/sandbox/signups" },
  { name: "verifySandboxSignup", method: "POST", path: "/v1/sandbox/signups/:id/verify" },
];

// Explicit mutation manifest (spec/15 read-only-default rule).
// The console renders from GET endpoints only; every write below is a named
// POST issued exclusively from client.ts. scripts/no-write-guard.mjs scans
// src/ at build time and fails on any non-GET fetch outside client.ts, and
// cross-checks client.ts against this manifest.

export interface MutationRoute {
  name: string;
  method: "POST";
  path: string; // ":id"-style parameter segments
}

export const MUTATION_MANIFEST: MutationRoute[] = [
  { name: "login", method: "POST", path: "/v1/auth/operator/login" },
  { name: "logout", method: "POST", path: "/v1/auth/operator/logout" },
  { name: "approveApproval", method: "POST", path: "/v1/approval-requests/:id/approve" },
  { name: "rejectApproval", method: "POST", path: "/v1/approval-requests/:id/reject" },
  { name: "withdrawApproval", method: "POST", path: "/v1/approval-requests/:id/withdraw" },
  { name: "assignCase", method: "POST", path: "/v1/ops-cases/:id/assign" },
  { name: "commentCase", method: "POST", path: "/v1/ops-cases/:id/comment" },
  { name: "attachCaseEvidence", method: "POST", path: "/v1/ops-cases/:id/attach-evidence" },
  { name: "resolveCase", method: "POST", path: "/v1/ops-cases/:id/resolve" },
  { name: "requestDisputeEvidence", method: "POST", path: "/v1/disputes/:id/request-evidence" },
  { name: "submitDisputeEvidence", method: "POST", path: "/v1/disputes/:id/evidence" },
  { name: "markDisputeUnderReview", method: "POST", path: "/v1/disputes/:id/mark-under-review" },
  { name: "resolveDispute", method: "POST", path: "/v1/disputes/:id/resolve" },
  { name: "fileChargeback", method: "POST", path: "/v1/disputes/:id/file-chargeback" },
  { name: "firstTouchCompensation", method: "POST", path: "/v1/compensation-queue/:id/first-touch" },
  { name: "setCompensationRunbookStep", method: "POST", path: "/v1/compensation-queue/:id/runbook-step" },
  { name: "attachCompensationEvidence", method: "POST", path: "/v1/compensation-queue/:id/attach-evidence" },
  { name: "resolveCompensation", method: "POST", path: "/v1/compensation-queue/:id/resolve" },
  { name: "resolveReconException", method: "POST", path: "/v1/recon-exceptions/:id/resolve" },
  { name: "requestConnectorModeChange", method: "POST", path: "/v1/connectors/:id/mode" },
  { name: "resetCircuit", method: "POST", path: "/v1/connectors/:id/circuit/reset" },
  { name: "disableOperator", method: "POST", path: "/v1/operators/:id/disable" },
  { name: "camlcoFreeze", method: "POST", path: "/v1/camlco/freeze" },
];

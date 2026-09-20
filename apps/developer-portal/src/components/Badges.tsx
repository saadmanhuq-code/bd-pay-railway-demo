"use client";

import React from "react";
import { ageLabel } from "@/lib/format";
import { useLang } from "@/lib/i18n/LangContext";

const GOOD = new Set(["ACTIVE", "SUCCEEDED", "DELIVERED", "RELEASED", "RESOLVED_MERCHANT", "ACCEPTED", "RELEASE_SCHEDULED", "CHARGEBACK_CLOSED"]);
const BAD = new Set(["FAILED", "REVOKED", "EXPIRED", "REJECTED", "DELETED", "EXHAUSTED", "RESOLVED_CUSTOMER", "SUSPENDED", "TERMINATED", "CANCELLED"]);
const WARN = new Set([
  "ROTATION_PENDING",
  "PENDING",
  "PROCESSING",
  "PENDING_CHECKER",
  "PENDING_FINANCE_COSIGN",
  "EVIDENCE_PENDING",
  "UNDER_REVIEW",
  "CHARGEBACK_FILED",
  "REQUIRES_CAPTURE",
  "REQUIRES_CONFIRMATION",
  "REQUIRES_ACTION",
  "REQUIRES_PAYMENT_METHOD",
  "UPLOADED",
  "REQUIRED",
  "DOCUMENTS_PENDING",
  "KYB_IN_PROGRESS",
  "SANCTIONS_REVIEW",
  "ENHANCED_DUE_DILIGENCE",
  "AGREEMENT_PENDING",
  "PARTIALLY_REFUNDED",
  "PENDING_SPONSOR_RANGE",
  "OPEN",
]);

export function StateBadge({ value }: { value: string }) {
  const cls = GOOD.has(value) ? "badge badge-good" : BAD.has(value) ? "badge badge-bad" : WARN.has(value) ? "badge badge-warn" : "badge";
  return <span className={cls}>{value}</span>;
}

// Data-age badge — every dashboard panel shows one (spec/15 §Failure modes:
// explicit staleness, never silently old).
export function AgeBadge({ asOf }: { asOf: string }) {
  const { t } = useLang();
  return (
    <span className="badge badge-age" title={asOf}>
      {t("data_age")}: {ageLabel(asOf)}
    </span>
  );
}

export function EnvBadge({ env }: { env: string }) {
  return <span className={`badge ${env === "live" ? "badge-good" : "badge-warn"}`}>{env}</span>;
}

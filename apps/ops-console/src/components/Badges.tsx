"use client";

import React from "react";
import { ageLabel } from "@/lib/format";
import { useLang } from "@/lib/i18n/LangContext";

const GOOD = new Set(["APPROVED", "RESOLVED", "VERIFIED", "HEALTHY", "CLOSED", "ACTIVE", "PASSED", "CONFIRMED", "RESOLVED_MERCHANT", "RESOLVED_CUSTOMER", "PRODUCTION", "ACKNOWLEDGED"]);
const BAD = new Set(["REJECTED", "FAILED", "UNHEALTHY", "OPEN_CIRCUIT", "DISABLED", "EXPIRED", "APPROVED_EXECUTION_FAILED", "LOCKED", "WRITTEN_OFF", "P0"]);
const WARN = new Set(["PENDING_SECOND_APPROVER", "PENDING_APPROVAL", "DEGRADED", "HALF_OPEN", "IN_PROGRESS", "INVESTIGATING", "UNDER_REVIEW", "EVIDENCE_PENDING", "WAITING_EXTERNAL", "QUEUED", "CHARGEBACK_FILED", "P1", "SANDBOX", "RUNNING"]);

export function StateBadge({ value, circuit }: { value: string; circuit?: boolean }) {
  const key = circuit && value === "OPEN" ? "OPEN_CIRCUIT" : value;
  const cls = GOOD.has(key) ? "badge badge-good" : BAD.has(key) ? "badge badge-bad" : WARN.has(key) ? "badge badge-warn" : "badge";
  return <span className={cls}>{value}</span>;
}

export function PriorityBadge({ value }: { value: string }) {
  const cls = value === "P0" ? "badge badge-bad badge-p0" : value === "P1" ? "badge badge-warn" : "badge";
  return <span className={cls}>{value}</span>;
}

// Data-age badge — every dashboard panel shows one (spec/15 §Failure modes:
// "stale dashboard during incident" — explicit staleness, never silently old).
export function AgeBadge({ asOf }: { asOf: string }) {
  const { t } = useLang();
  return (
    <span className="badge badge-age" title={asOf}>
      {t("data_age")}: {ageLabel(asOf)}
    </span>
  );
}

export function SlaBadge({ breached }: { breached: boolean }) {
  const { t } = useLang();
  if (!breached) return null;
  return <span className="badge badge-bad">{t("sla_breached")}</span>;
}

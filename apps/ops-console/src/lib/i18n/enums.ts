// Human labels for API enum values (FE QA 2026-09-28 pm).
//
// The console used to print raw wire enums (IN_PROGRESS, RTGS_MANUAL, …) in
// badges, filters and table cells, English-only even in Bengali mode. The raw
// value stays the source of truth: it is still what filters send to the API
// and it is kept as the element's title attribute for operators who search
// runbooks by code. Unknown values fall back to a humanised English label.

import type { Bi, Lang } from "./copy";

export const ENUM_LABELS: Record<string, Bi> = {
  // generic states
  OPEN: { en: "Open", bn: "খোলা" },
  IN_PROGRESS: { en: "In progress", bn: "চলমান" },
  WAITING_EXTERNAL: { en: "Waiting on external", bn: "বাহ্যিক অপেক্ষায়" },
  RESOLVED: { en: "Resolved", bn: "নিষ্পন্ন" },
  CANCELLED: { en: "Cancelled", bn: "বাতিল" },
  CLOSED: { en: "Closed", bn: "বন্ধ" },
  ACTIVE: { en: "Active", bn: "সক্রিয়" },
  DISABLED: { en: "Disabled", bn: "নিষ্ক্রিয়" },
  LOCKED: { en: "Locked", bn: "লক করা" },
  EXPIRED: { en: "Expired", bn: "মেয়াদোত্তীর্ণ" },
  WITHDRAWN: { en: "Withdrawn", bn: "প্রত্যাহৃত" },
  APPROVED: { en: "Approved", bn: "অনুমোদিত" },
  REJECTED: { en: "Rejected", bn: "প্রত্যাখ্যাত" },
  APPROVED_EXECUTION_FAILED: { en: "Approved · execution failed", bn: "অনুমোদিত · কার্যকর ব্যর্থ" },
  PENDING_APPROVAL: { en: "Pending approval", bn: "অনুমোদনের অপেক্ষায়" },
  PENDING_SECOND_APPROVER: { en: "Awaiting 2nd approver", bn: "দ্বিতীয় অনুমোদনকারীর অপেক্ষায়" },
  QUEUED: { en: "Queued", bn: "সারিতে" },
  RUNNING: { en: "Running", bn: "চলছে" },
  PASSED: { en: "Passed", bn: "উত্তীর্ণ" },
  FAILED: { en: "Failed", bn: "ব্যর্থ" },
  NEVER_RUN: { en: "Never run", bn: "কখনো চালানো হয়নি" },
  INVESTIGATING: { en: "Investigating", bn: "তদন্তাধীন" },
  UNDER_REVIEW: { en: "Under review", bn: "পর্যালোচনাধীন" },
  IN_REVIEW: { en: "In review", bn: "পর্যালোচনায়" },
  EVIDENCE_PENDING: { en: "Evidence pending", bn: "প্রমাণের অপেক্ষায়" },
  RESOLVED_MERCHANT: { en: "Resolved · merchant", bn: "নিষ্পন্ন · মার্চেন্টের পক্ষে" },
  RESOLVED_CUSTOMER: { en: "Resolved · customer", bn: "নিষ্পন্ন · গ্রাহকের পক্ষে" },
  CHARGEBACK_FILED: { en: "Chargeback filed", bn: "চার্জব্যাক দাখিল" },
  CHARGEBACK_CLOSED: { en: "Chargeback closed", bn: "চার্জব্যাক বন্ধ" },
  WRITTEN_OFF: { en: "Written off", bn: "অবলোপিত" },
  CONFIRMED: { en: "Confirmed", bn: "নিশ্চিত" },
  PENDING: { en: "Pending", bn: "অপেক্ষমাণ" },
  BATCHED: { en: "Batched", bn: "ব্যাচভুক্ত" },
  RELEASED: { en: "Released", bn: "ছাড় করা" },
  RETURNED: { en: "Returned", bn: "ফেরত" },
  DEBIT: { en: "Debit", bn: "ডেবিট" },
  CREDIT: { en: "Credit", bn: "ক্রেডিট" },
  // connector health / mode / circuit
  HEALTHY: { en: "Healthy", bn: "সুস্থ" },
  DEGRADED: { en: "Degraded", bn: "অবনমিত" },
  UNHEALTHY: { en: "Unhealthy", bn: "অসুস্থ" },
  HALF_OPEN: { en: "Half-open", bn: "অর্ধ-খোলা" },
  SANDBOX: { en: "Sandbox", bn: "স্যান্ডবক্স" },
  SIMULATOR: { en: "Simulator", bn: "সিমুলেটর" },
  PRODUCTION: { en: "Production", bn: "প্রোডাকশন" },
  MANUAL_LOCAL: { en: "Manual (local)", bn: "ম্যানুয়াল (স্থানীয়)" },
  // case types
  AML_ALERT: { en: "AML alert", bn: "এএমএল সতর্কতা" },
  RECON_EXCEPTION: { en: "Recon exception", bn: "সমন্বয় ব্যতিক্রম" },
  DISPUTE: { en: "Dispute", bn: "বিরোধ" },
  COMPENSATION: { en: "Compensation", bn: "ক্ষতিপূরণ" },
  RTGS_MANUAL: { en: "RTGS manual", bn: "আরটিজিএস ম্যানুয়াল" },
  WEBHOOK_POISON: { en: "Webhook poison", bn: "ওয়েবহুক ত্রুটি" },
  SANCTIONS_MANUAL_ENTRY: { en: "Sanctions manual entry", bn: "নিষেধাজ্ঞা ম্যানুয়াল এন্ট্রি" },
  INCIDENT: { en: "Incident", bn: "ঘটনা" },
  DATA_BREACH: { en: "Data breach", bn: "তথ্য লঙ্ঘন" },
  OTHER: { en: "Other", bn: "অন্যান্য" },
  // recon exception kinds
  AMOUNT_MISMATCH: { en: "Amount mismatch", bn: "পরিমাণে অমিল" },
  STATE_MISMATCH: { en: "State mismatch", bn: "অবস্থায় অমিল" },
  MISSING_AT_LEDGER: { en: "Missing at ledger", bn: "খতিয়ানে নেই" },
  MISSING_AT_RAIL: { en: "Missing at rail", bn: "রেইলে নেই" },
  DUPLICATE_AT_RAIL: { en: "Duplicate at rail", bn: "রেইলে দ্বৈত" },
  // payment methods (brand names)
  BKASH: { en: "bKash", bn: "বিকাশ" },
  NAGAD: { en: "Nagad", bn: "নগদ" },
  CARD: { en: "Card", bn: "কার্ড" },
  NPSB_IBFT: { en: "NPSB IBFT", bn: "এনপিএসবি আইবিএফটি" },
  BANGLA_QR: { en: "Bangla QR", bn: "বাংলা কিউআর" },
};

const ACRONYMS = /\b(rtgs|aml|qr|npsb|ibft|api|sla|tcsa|pgw|beftn|kyc|otp|totp|id)\b/g;

/** "SOME_NEW_STATE" -> "Some new state"; "RtgsMessage" -> "RTGS message". */
export function humanizeEnum(value: string): string {
  const spaced = value
    .replace(/([a-z0-9])([A-Z])/g, "$1 $2")
    .replace(/[_-]+/g, " ")
    .trim()
    .toLowerCase()
    .replace(ACRONYMS, (a) => a.toUpperCase());
  return spaced ? spaced.charAt(0).toUpperCase() + spaced.slice(1) : value;
}

export function enumLabel(value: string | null | undefined, lang: Lang): string {
  if (!value) return "—";
  const bi = ENUM_LABELS[value];
  if (bi) return lang === "bn" ? bi.bn : bi.en;
  return humanizeEnum(value);
}

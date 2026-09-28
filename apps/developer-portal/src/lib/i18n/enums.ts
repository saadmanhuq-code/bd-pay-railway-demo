// Human labels for API enum values (FE QA 2026-09-28 pm; mirrors the
// ops-console helper). Badges and method cells used to print raw wire codes
// (ROTATION_PENDING, BANGLA_QR, …). The raw value stays the source of truth
// and is kept as the element's title; unknown values fall back to a
// humanised English label.

import type { Bi, Lang } from "./copy";

export const ENUM_LABELS: Record<string, Bi> = {
  // payment intents
  SUCCEEDED: { en: "Succeeded", bn: "সফল" },
  FAILED: { en: "Failed", bn: "ব্যর্থ" },
  PROCESSING: { en: "Processing", bn: "প্রক্রিয়াধীন" },
  CANCELLED: { en: "Cancelled", bn: "বাতিল" },
  CREATED: { en: "Created", bn: "তৈরি" },
  PARTIALLY_REFUNDED: { en: "Partially refunded", bn: "আংশিক ফেরত" },
  REQUIRES_CAPTURE: { en: "Awaiting capture", bn: "ক্যাপচারের অপেক্ষায়" },
  REQUIRES_CONFIRMATION: { en: "Awaiting confirmation", bn: "নিশ্চিতকরণের অপেক্ষায়" },
  REQUIRES_ACTION: { en: "Action required", bn: "পদক্ষেপ প্রয়োজন" },
  REQUIRES_PAYMENT_METHOD: { en: "Awaiting payment method", bn: "পেমেন্ট পদ্ধতির অপেক্ষায়" },
  // keys / webhooks / generic
  ACTIVE: { en: "Active", bn: "সক্রিয়" },
  ROTATION_PENDING: { en: "Rotation pending", bn: "রোটেশন অপেক্ষমাণ" },
  REVOKED: { en: "Revoked", bn: "প্রত্যাহৃত" },
  EXPIRED: { en: "Expired", bn: "মেয়াদোত্তীর্ণ" },
  DISABLED: { en: "Disabled", bn: "নিষ্ক্রিয়" },
  DELETED: { en: "Deleted", bn: "মুছে ফেলা" },
  DELIVERED: { en: "Delivered", bn: "পৌঁছেছে" },
  EXHAUSTED: { en: "Retries exhausted", bn: "পুনঃচেষ্টা শেষ" },
  PENDING: { en: "Pending", bn: "অপেক্ষমাণ" },
  OPEN: { en: "Open", bn: "খোলা" },
  ACCEPTED: { en: "Accepted", bn: "গৃহীত" },
  REJECTED: { en: "Rejected", bn: "প্রত্যাখ্যাত" },
  WITHDRAWN: { en: "Withdrawn", bn: "প্রত্যাহৃত" },
  UNDER_REVIEW: { en: "Under review", bn: "পর্যালোচনাধীন" },
  // disputes
  EVIDENCE_PENDING: { en: "Evidence pending", bn: "প্রমাণের অপেক্ষায়" },
  RESOLVED_MERCHANT: { en: "Won (merchant)", bn: "নিষ্পন্ন · মার্চেন্টের পক্ষে" },
  RESOLVED_CUSTOMER: { en: "Lost (customer)", bn: "নিষ্পন্ন · গ্রাহকের পক্ষে" },
  CHARGEBACK_FILED: { en: "Chargeback filed", bn: "চার্জব্যাক দাখিল" },
  CHARGEBACK_CLOSED: { en: "Chargeback closed", bn: "চার্জব্যাক বন্ধ" },
  // disbursements / settlement
  PENDING_CHECKER: { en: "Awaiting checker", bn: "চেকারের অপেক্ষায়" },
  PENDING_FINANCE_COSIGN: { en: "Awaiting finance co-sign", bn: "ফাইন্যান্স সহ-স্বাক্ষরের অপেক্ষায়" },
  RELEASED: { en: "Released", bn: "ছাড় করা" },
  RELEASE_SCHEDULED: { en: "Release scheduled", bn: "ছাড় নির্ধারিত" },
  // onboarding / KYB
  REQUIRED: { en: "Required", bn: "প্রয়োজন" },
  UPLOADED: { en: "Uploaded", bn: "আপলোড হয়েছে" },
  APPLICATION_SUBMITTED: { en: "Application submitted", bn: "আবেদন জমা হয়েছে" },
  DOCUMENTS_PENDING: { en: "Documents pending", bn: "নথির অপেক্ষায়" },
  KYB_IN_PROGRESS: { en: "KYB in progress", bn: "কেওয়াইবি চলমান" },
  SANCTIONS_REVIEW: { en: "Sanctions review", bn: "নিষেধাজ্ঞা পর্যালোচনা" },
  ENHANCED_DUE_DILIGENCE: { en: "Enhanced due diligence", bn: "বর্ধিত যাচাই" },
  RISK_ASSESSED: { en: "Risk assessed", bn: "ঝুঁকি মূল্যায়িত" },
  AGREEMENT_PENDING: { en: "Agreement pending", bn: "চুক্তির অপেক্ষায়" },
  PENDING_SPONSOR_RANGE: { en: "Awaiting sponsor range", bn: "স্পনসর রেঞ্জের অপেক্ষায়" },
  SUSPENDED: { en: "Suspended", bn: "স্থগিত" },
  TERMINATED: { en: "Terminated", bn: "সমাপ্ত" },
  // payment methods (brand names)
  BKASH: { en: "bKash", bn: "বিকাশ" },
  NAGAD: { en: "Nagad", bn: "নগদ" },
  CARD: { en: "Card", bn: "কার্ড" },
  NPSB_IBFT: { en: "NPSB IBFT", bn: "এনপিএসবি আইবিএফটি" },
  BANGLA_QR: { en: "Bangla QR", bn: "বাংলা কিউআর" },
};

const ACRONYMS = /\b(rtgs|aml|qr|npsb|ibft|api|kyb|kyc|va|otp|totp|id)\b/g;

/** "SOME_NEW_STATE" -> "Some new state"; "VA_RECON_REPORT" -> "VA recon report". */
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

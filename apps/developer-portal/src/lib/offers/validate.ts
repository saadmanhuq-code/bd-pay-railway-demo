// Client-side convenience validation for the offer create/edit forms.
// Mirrors bdpay/kernel/offers/models.py rule-for-rule so merchants see the
// refusal BEFORE the round trip — the server re-validates every rule and is
// authoritative (spec/15 posture: the portal renders, it never recomputes).
//
// Pure functions, no I/O, no clock reads: callers pass every input explicitly.

import {
  MAX_WINDOW_ROWS,
  PERCENT_BPS_CEILING,
  PERCENT_BPS_FLOOR,
  VALIDITY_MAX_DAYS,
  WINDOW_DAY_CODES,
  type OfferWindow,
} from "@/lib/api/offerTypes";

const HHMM_RE = /^(?:[01][0-9]|2[0-3]):[0-5][0-9]$/;

export interface FieldIssue {
  field: string;
  code: string; // matches the server's error code where one exists
}

export function validateWindowRow(w: OfferWindow): FieldIssue[] {
  const issues: FieldIssue[] = [];
  if (w.days.length === 0) issues.push({ field: "windows", code: "invalid_window" });
  if (new Set(w.days).size !== w.days.length) issues.push({ field: "windows", code: "invalid_window" });
  for (const d of w.days) {
    if (!(WINDOW_DAY_CODES as readonly string[]).includes(d)) {
      issues.push({ field: "windows", code: "invalid_window" });
    }
  }
  if (!HHMM_RE.test(w.start_local) || !HHMM_RE.test(w.end_local)) {
    issues.push({ field: "windows", code: "invalid_window" });
  } else if (!(w.start_local < w.end_local)) {
    // Lexicographic compare is correct for zero-padded HH:MM.
    issues.push({ field: "windows", code: "invalid_window" });
  }
  return issues;
}

// spec/18 create-time window rules: 1..7 rows (S18-E8: empty set refused),
// no same-day overlap of half-open [start, end) ranges.
export function validateWindows(windows: OfferWindow[]): FieldIssue[] {
  if (windows.length === 0) return [{ field: "windows", code: "windows_required" }];
  if (windows.length > MAX_WINDOW_ROWS) return [{ field: "windows", code: "invalid_window" }];
  const issues = windows.flatMap(validateWindowRow);
  if (issues.length > 0) return issues;
  for (let i = 0; i < windows.length; i++) {
    for (let j = i + 1; j < windows.length; j++) {
      const a = windows[i]!;
      const b = windows[j]!;
      const sharesDay = a.days.some((d) => b.days.includes(d));
      if (sharesDay && a.start_local < b.end_local && b.start_local < a.end_local) {
        return [{ field: "windows", code: "overlapping_windows" }];
      }
    }
  }
  return [];
}

export function validatePercentBps(percentBps: number | null): FieldIssue[] {
  if (percentBps === null || !Number.isInteger(percentBps)) {
    return [{ field: "percent_bps", code: "invalid_request" }];
  }
  if (percentBps < PERCENT_BPS_FLOOR || percentBps > PERCENT_BPS_CEILING) {
    return [{ field: "percent_bps", code: "percent_bps_out_of_range" }];
  }
  return [];
}

export function validateValidity(validFrom: string, validUntil: string): FieldIssue[] {
  const from = Date.parse(validFrom);
  const until = Date.parse(validUntil);
  if (!Number.isFinite(from) || !Number.isFinite(until)) {
    return [{ field: "validity", code: "invalid_validity_range" }];
  }
  if (!(from < until)) return [{ field: "validity", code: "invalid_validity_range" }];
  if (until - from > VALIDITY_MAX_DAYS * 24 * 3_600_000) {
    return [{ field: "validity", code: "invalid_validity_range" }];
  }
  return [];
}

// Caps: every cap >= 1 when present; cap_per_day <= cap_total when both
// present; at least one of cap_per_day / cap_total REQUIRED — uncapped offers
// do not exist; the circuit breaker is not optional (spec/18).
export function validateCaps(capPerDay: number | null, capTotal: number | null, capPerCustomer: number | null): FieldIssue[] {
  const issues: FieldIssue[] = [];
  for (const [field, v] of [
    ["cap_per_day", capPerDay],
    ["cap_total", capTotal],
    ["cap_per_customer", capPerCustomer],
  ] as const) {
    if (v !== null && (!Number.isInteger(v) || v < 1)) issues.push({ field, code: "invalid_request" });
  }
  if (capPerDay === null && capTotal === null) issues.push({ field: "caps", code: "cap_required" });
  if (capPerDay !== null && capTotal !== null && capPerDay > capTotal) {
    issues.push({ field: "caps", code: "invalid_request" });
  }
  return issues;
}

export function validateAmounts(minSpendMinor: number | null, maxDiscountMinor: number | null): FieldIssue[] {
  const issues: FieldIssue[] = [];
  if (minSpendMinor !== null && (!Number.isInteger(minSpendMinor) || minSpendMinor < 0)) {
    issues.push({ field: "min_spend_minor", code: "amount_must_be_integer" });
  }
  if (maxDiscountMinor !== null && (!Number.isInteger(maxDiscountMinor) || maxDiscountMinor < 1)) {
    issues.push({ field: "max_discount_minor", code: "amount_must_be_integer" });
  }
  return issues;
}

export interface CreateFormSnapshot {
  percentBps: number | null;
  title: string;
  titleBn: string;
  windows: OfferWindow[];
  validFrom: string;
  validUntil: string;
  minSpendMinor: number | null;
  maxDiscountMinor: number | null;
  capPerDay: number | null;
  capTotal: number | null;
  capPerCustomer: number | null;
  allowedMethods: string[];
}

export function validateCreateForm(s: CreateFormSnapshot): FieldIssue[] {
  const issues: FieldIssue[] = [];
  if (s.title.trim() === "" || s.titleBn.trim() === "") issues.push({ field: "title", code: "invalid_request" });
  if (s.allowedMethods.length === 0) issues.push({ field: "allowed_methods", code: "invalid_request" });
  issues.push(...validatePercentBps(s.percentBps));
  issues.push(...validateWindows(s.windows));
  issues.push(...validateValidity(s.validFrom, s.validUntil));
  issues.push(...validateCaps(s.capPerDay, s.capTotal, s.capPerCustomer));
  issues.push(...validateAmounts(s.minSpendMinor, s.maxDiscountMinor));
  return issues;
}

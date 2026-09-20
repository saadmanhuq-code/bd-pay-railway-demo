// Money formatting — integer/BigInt math ONLY. amount_minor fields are
// integer paisa (conventions §6); floats are banned on the money path.

export function formatBdt(amountMinor: number | string): string {
  let v: bigint;
  if (typeof amountMinor === "number") {
    if (!Number.isInteger(amountMinor)) {
      throw new Error("amount_minor must be an integer (paisa)");
    }
    v = BigInt(amountMinor);
  } else {
    v = BigInt(amountMinor.trim());
  }
  const negative = v < 0n;
  const abs = negative ? -v : v;
  const taka = abs / 100n;
  const paisa = abs % 100n;
  const takaStr = groupThousands(taka.toString());
  const paisaStr = paisa.toString().padStart(2, "0");
  return `${negative ? "-" : ""}BDT ${takaStr}.${paisaStr}`;
}

export function groupThousands(digits: string): string {
  let out = "";
  let count = 0;
  for (let i = digits.length - 1; i >= 0; i--) {
    out = digits[i] + out;
    count++;
    if (count % 3 === 0 && i > 0) {
      out = "," + out;
    }
  }
  return out;
}

export function formatTs(iso: string | null | undefined): string {
  if (!iso) return "—";
  return iso.replace("T", " ").replace(/\.\d+Z$/, "Z").replace("Z", " UTC");
}

export function shortId(id: string, keep = 14): string {
  return id.length <= keep ? id : `${id.slice(0, keep)}…`;
}

// Human age between a snapshot timestamp and now — display only, never the
// data path (mock data timestamps are fixed constants).
export function ageLabel(iso: string): string {
  const then = Date.parse(iso);
  const diffMs = Date.now() - then;
  if (!Number.isFinite(diffMs)) return "unknown age";
  const s = Math.max(0, Math.floor(diffMs / 1000));
  if (s < 90) return `${s}s old`;
  const m = Math.floor(s / 60);
  if (m < 90) return `${m}m old`;
  const h = Math.floor(m / 60);
  if (h < 48) return `${h}h old`;
  return `${Math.floor(h / 24)}d old`;
}

// Evidence-deadline countdown — display only (Date.now is never in the mock
// data path).
export function deadlineCountdown(deadline: string | null): { label: string; late: boolean } | null {
  if (!deadline) return null;
  const diffMs = Date.parse(deadline) - Date.now();
  if (!Number.isFinite(diffMs)) return null;
  if (diffMs <= 0) return { label: "deadline passed", late: true };
  const hours = Math.floor(diffMs / 3_600_000);
  const days = Math.floor(hours / 24);
  if (days >= 1) return { label: `${days}d ${hours - days * 24}h left`, late: false };
  return { label: `${hours}h left`, late: hours < 24 };
}

// ---------------------------------------------------------------------------
// Bengali numeral normalization — client-side convenience only; the server
// re-runs BangladeshNumericNormalization and is authoritative.
// ---------------------------------------------------------------------------

const BN_DIGITS = "০১২৩৪৫৬৭৮৯";

export function normalizeBengaliDigits(input: string): string {
  let out = "";
  for (const ch of input) {
    const idx = BN_DIGITS.indexOf(ch);
    out += idx >= 0 ? String(idx) : ch;
  }
  return out;
}

// Parse a BDT amount string ("1,250.50", "১২৫০.৫০") into integer paisa as a
// string. String/BigInt math only — never parseFloat. Returns null on any
// malformed input.
export function parseAmountToMinor(raw: string): string | null {
  const normalized = normalizeBengaliDigits(raw).replace(/,/g, "").trim();
  if (normalized === "") return null;
  const m = normalized.match(/^(\d+)(?:\.(\d{1,2}))?$/);
  if (!m) return null;
  const takaPart = m[1] ?? "0";
  const paisaPart = (m[2] ?? "").padEnd(2, "0");
  try {
    const minor = BigInt(takaPart) * 100n + BigInt(paisaPart === "" ? "0" : paisaPart);
    return minor.toString();
  } catch {
    return null;
  }
}

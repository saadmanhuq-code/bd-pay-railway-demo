// Money formatting — integer/BigInt math ONLY. amount_minor fields are
// integer paisa (conventions §6); floats are banned on the money path.
// Same helpers as the developer-portal (lifted verbatim where applicable),
// plus Bengali-numeral rendering for the Bengali-first surface.

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

// Bengali-first display: "৳ ১,২৫০.৫০" with Bengali numerals.
export function formatBdtBn(amountMinor: number | string): string {
  const ascii = formatBdt(amountMinor).replace(/^(-?)BDT /, "$1");
  const sign = ascii.startsWith("-") ? "-" : "";
  const body = ascii.replace(/^-/, "");
  return `${sign}৳ ${toBengaliDigits(body)}`;
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

// ---------------------------------------------------------------------------
// Bengali numeral normalization — client-side convenience only; the server
// re-runs BangladeshNumericNormalization and is authoritative.
// (Lift: bangla-decision-agent whatsapp-intake normalizer — G5 row 4, gated
// at diner-app build; the in-repo Python port is the verified donor.)
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

export function toBengaliDigits(input: string): string {
  let out = "";
  for (const ch of input) {
    const code = ch.charCodeAt(0);
    out += code >= 48 && code <= 57 ? BN_DIGITS[code - 48] : ch;
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

// BD mobile validation after Bengali-digit normalization: 01[3-9]XXXXXXXX.
export function normalizeBdMobile(raw: string): string | null {
  const ascii = normalizeBengaliDigits(raw).replace(/[\s-]/g, "");
  const m = ascii.match(/^(?:\+?880|0)?(1[3-9]\d{8})$/);
  if (!m) return null;
  return `0${m[1]}`;
}

export function maskBdMobile(msisdn: string): string {
  // PII posture: never render the full number back; keep last 3 digits.
  return msisdn.length >= 11 ? `01•••••••${msisdn.slice(-3)}` : "01•••";
}

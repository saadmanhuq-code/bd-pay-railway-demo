#!/usr/bin/env node
// Bengali corpus build gate (spec/18 G5 — mojibake harness lifted from the
// protein-chain-bd pattern; same regexes the in-repo port pins in
// tests/platform/test_bn_encoding.py).
//
// Checks src/lib/i18n/copy.ts:
//   1. Every copy entry carries BOTH `en` and `bn` (key symmetry — the khep
//      bn.json check, G5 follow-up action 3).
//   2. No mojibake byte-sequences anywhere in the corpus (MOJIBAKE_RE).
//   3. Every `bn` value contains at least one Bengali-block character
//      (BENGALI_BLOCK_RE) — ASCII-only "translations" are refused.
//   4. No placeholder values ([BN-TBD] / TODO / PLACEHOLDER / FIXME).
//   5. The 11 spec/18 ReasonCode bn strings are present BYTE-IDENTICAL to
//      the kernel's authoritative table (bdpay/kernel/offers/copy_bn.py,
//      itself pinned by tests/platform/test_bn_encoding.py) — so this app's
//      consumer-displayable refusal copy can never drift from the engine.

import { readFileSync } from "node:fs";
import { join } from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";

const ROOT = fileURLToPath(new URL("..", import.meta.url));
const COPY_FILE = join(ROOT, "src", "lib", "i18n", "copy.ts");

// Same patterns as the G5 harness (docs/SPEC18-G5-LIFT-VERIFICATION.md row 2):
// MOJIBAKE_RE = Atilde[80-BF] | acircEUR | acircEURtrade | iuml.. | agrave-cent | agrave-sect
// \uXXXX escapes so this gate file itself carries no suspicious byte runs.
const MOJIBAKE_RE = new RegExp(
  "\u00C3[\u0080-\u00BF]|\u00E2\u20AC\u2122|\u00E2\u20AC|\u00EF\u00BF\u00BD|\u00E0\u00A6|\u00E0\u00A7",
);
// Bengali Unicode block U+0980-U+09FF.
const BENGALI_BLOCK_RE = new RegExp("[\u0980-\u09FF]");
const PLACEHOLDER_RE = /\[BN-TBD\]|TODO|PLACEHOLDER|FIXME/;

// spec/18 ReasonCode -> message_bn, byte-identical to the kernel
// REASON_COPY table (bdpay/kernel/offers/copy_bn.py).
const KERNEL_REASON_BN = {
  OFFER_NOT_ACTIVE: "অফারটি এখন সক্রিয় নেই",
  OUTSIDE_WINDOW: "এই সময়ে অফারটি প্রযোজ্য নয়",
  BEFORE_VALIDITY: "অফারের মেয়াদ এখনও শুরু হয়নি",
  AFTER_VALIDITY: "অফারের মেয়াদ শেষ হয়ে গেছে",
  MIN_SPEND_NOT_MET: "ন্যূনতম কেনাকাটার পরিমাণ পূরণ হয়নি",
  METHOD_NOT_ALLOWED: "এই পেমেন্ট পদ্ধতিতে অফারটি প্রযোজ্য নয়",
  CAP_DAY_EXHAUSTED: "আজকের জন্য অফারের সীমা শেষ হয়ে গেছে",
  CAP_TOTAL_EXHAUSTED: "অফারের মোট সীমা শেষ হয়ে গেছে",
  CAP_CUSTOMER_EXHAUSTED: "আপনি এই অফারটি ইতিমধ্যে ব্যবহার করেছেন",
  MERCHANT_NOT_ACTIVE: "মার্চেন্ট এই মুহূর্তে সক্রিয় নেই",
  BOGO_LINE_ITEMS_MISSING: "BOGO অফারের জন্য পণ্যের বিবরণ প্রয়োজন",
};

const text = readFileSync(COPY_FILE, "utf8");
const failures = [];

// 2. Whole-file mojibake scan first (cheapest catastrophic failure).
const mojibakeHit = text.match(MOJIBAKE_RE);
if (mojibakeHit) {
  failures.push(`mojibake byte-sequence found in copy.ts: ${JSON.stringify(mojibakeHit[0])}`);
}

// 1 + 3 + 4. Entry-level checks. Copy entries are `{ en: "...", bn: "..." }`
// object literals; parse them pairwise.
const entryRe = /(\w+):\s*\{\s*en:\s*"((?:[^"\\]|\\.)*)",\s*bn:\s*"((?:[^"\\]|\\.)*)",?\s*\}/gs;
let entryCount = 0;
for (const m of text.matchAll(entryRe)) {
  entryCount++;
  const [, key, en, bn] = m;
  if (en.trim() === "") failures.push(`${key}: empty en value`);
  if (bn.trim() === "") failures.push(`${key}: empty bn value`);
  if (!BENGALI_BLOCK_RE.test(bn)) {
    failures.push(`${key}: bn value contains no Bengali-block character: ${JSON.stringify(bn)}`);
  }
  if (PLACEHOLDER_RE.test(en) || PLACEHOLDER_RE.test(bn)) {
    failures.push(`${key}: placeholder value`);
  }
}
if (entryCount === 0) {
  failures.push("no copy entries found — entry regex matched nothing (corpus drift?)");
}

// Asymmetry: an `en:` without a sibling `bn:` never matches entryRe, so
// count raw occurrences and compare.
const enCount = (text.match(/\ben:\s*"/g) ?? []).length;
const bnCount = (text.match(/\bbn:\s*"/g) ?? []).length;
if (enCount !== bnCount) {
  failures.push(`key asymmetry: ${enCount} en values vs ${bnCount} bn values`);
}
if (entryCount !== enCount) {
  failures.push(`entry parse mismatch: ${entryCount} parsed entries vs ${enCount} en values — malformed copy entry`);
}

// 5. Kernel ReasonCode parity, byte-identical.
for (const [reason, bn] of Object.entries(KERNEL_REASON_BN)) {
  if (!text.includes(reason)) {
    failures.push(`ReasonCode ${reason} missing from copy.ts`);
  } else if (!text.includes(bn)) {
    failures.push(`ReasonCode ${reason}: bn copy is not byte-identical to the kernel table`);
  }
}

if (failures.length > 0) {
  for (const f of failures) console.error(`check-bn-corpus: ${f}`);
  console.error(`check-bn-corpus: ${failures.length} violation(s).`);
  process.exit(1);
}
console.log(
  `check-bn-corpus: clean (${entryCount} bilingual entries; ${Object.keys(KERNEL_REASON_BN).length} kernel ReasonCodes byte-identical)`,
);

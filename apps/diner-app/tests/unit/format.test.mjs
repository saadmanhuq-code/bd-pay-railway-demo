// Pinning tests for the money/Bengali-digit helpers (G5 row 4 lift —
// bangla-decision-agent normalizer pattern; integer/BigInt math only).

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  formatBdt,
  formatBdtBn,
  maskBdMobile,
  normalizeBdMobile,
  normalizeBengaliDigits,
  parseAmountToMinor,
  toBengaliDigits,
} from "../../.test-dist/lib/format.js";

test("formatBdt groups thousands and pads paisa", () => {
  assert.equal(formatBdt(2030000), "BDT 20,300.00");
  assert.equal(formatBdt("125050"), "BDT 1,250.50");
  assert.equal(formatBdt(1), "BDT 0.01");
  assert.equal(formatBdt(-9500), "-BDT 95.00");
});

test("formatBdt refuses non-integer amounts (floats banned)", () => {
  assert.throws(() => formatBdt(10.5));
});

test("formatBdtBn renders Bengali numerals with the taka sign", () => {
  assert.equal(formatBdtBn(125050), "৳ ১,২৫০.৫০");
});

test("normalizeBengaliDigits maps all ten digits", () => {
  assert.equal(normalizeBengaliDigits("০১২৩৪৫৬৭৮৯"), "0123456789");
  assert.equal(normalizeBengaliDigits("৳ ১,২৫০.৫০"), "৳ 1,250.50");
});

test("toBengaliDigits is the inverse on ASCII digits", () => {
  assert.equal(toBengaliDigits("1,250.50"), "১,২৫০.৫০");
});

test("parseAmountToMinor handles ASCII, Bengali, commas, paisa", () => {
  assert.equal(parseAmountToMinor("1250"), "125000");
  assert.equal(parseAmountToMinor("1,250.50"), "125050");
  assert.equal(parseAmountToMinor("১২৫০.৫০"), "125050");
  assert.equal(parseAmountToMinor("০.০১"), "1");
});

test("parseAmountToMinor rejects malformed input", () => {
  assert.equal(parseAmountToMinor(""), null);
  assert.equal(parseAmountToMinor("12.345"), null); // sub-paisa refused
  assert.equal(parseAmountToMinor("abc"), null);
  assert.equal(parseAmountToMinor("-5"), null);
});

test("normalizeBdMobile accepts BD forms and Bengali digits", () => {
  assert.equal(normalizeBdMobile("01712345678"), "01712345678");
  assert.equal(normalizeBdMobile("+8801712345678"), "01712345678");
  assert.equal(normalizeBdMobile("8801712345678"), "01712345678");
  assert.equal(normalizeBdMobile("০১৭১২৩৪৫৬৭৮"), "01712345678");
  assert.equal(normalizeBdMobile("017 1234-5678"), "01712345678");
});

test("normalizeBdMobile rejects non-BD-mobile shapes", () => {
  assert.equal(normalizeBdMobile("0212345678"), null); // landline prefix
  assert.equal(normalizeBdMobile("0171234567"), null); // too short
  assert.equal(normalizeBdMobile("01112345678"), null); // 011 not in 01[3-9]
});

test("maskBdMobile keeps only the last 3 digits (PII posture)", () => {
  assert.equal(maskBdMobile("01712345678"), "01•••••••678");
});

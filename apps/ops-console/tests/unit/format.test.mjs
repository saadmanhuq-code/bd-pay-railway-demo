// Pinning tests for the ops-console's own money/timestamp/id display
// helpers (src/lib/format.ts). This replaces totp.test.mjs now that
// src/lib/mock/totp.ts moved to the shared @bdpay/edge/mock/totp (see the
// lane/349 workorder) — the moved module is unit-tested once, in
// packages/edge/tests/unit/totp.test.mjs, instead of once per app.
// Modeled on apps/diner-app/tests/unit/format.test.mjs.

import { test } from "node:test";
import assert from "node:assert/strict";

import { ageLabel, formatBdt, formatTs, shortId } from "../../.test-dist/lib/format.js";

test("formatBdt groups thousands and pads paisa", () => {
  assert.equal(formatBdt(2030000), "BDT 20,300.00");
  assert.equal(formatBdt("125050"), "BDT 1,250.50");
  assert.equal(formatBdt(1), "BDT 0.01");
  assert.equal(formatBdt(-9500), "-BDT 95.00");
});

test("formatBdt refuses non-integer amounts (floats banned)", () => {
  assert.throws(() => formatBdt(10.5));
});

test("formatTs renders Asia/Dhaka wall time (UTC+6), or an em dash for none", () => {
  // 12:30Z → 18:30 Asia/Dhaka
  assert.equal(formatTs("2026-03-15T12:30:00.000Z"), "2026-03-15 18:30:00 Asia/Dhaka");
  assert.equal(formatTs(null), "—");
  assert.equal(formatTs(undefined), "—");
});

test("shortId truncates only past the keep length, with an ellipsis", () => {
  assert.equal(shortId("short-id"), "short-id");
  assert.equal(shortId("a-very-long-identifier-string"), "a-very-long-id…");
  assert.equal(shortId("0123456789abcdef", 8), "01234567…");
});

test("ageLabel renders seconds/minutes/hours/days for a recent-past timestamp", () => {
  const secondsAgo = (s) => new Date(Date.now() - s * 1000).toISOString();
  assert.match(ageLabel(secondsAgo(30)), /^(29|30)s old$/);
  assert.match(ageLabel(secondsAgo(5 * 60)), /^5m old$/);
  assert.match(ageLabel(secondsAgo(5 * 60 * 60)), /^5h old$/);
  assert.match(ageLabel(secondsAgo(5 * 24 * 60 * 60)), /^5d old$/);
});

test("ageLabel reports unknown age for an unparsable timestamp", () => {
  assert.equal(ageLabel("not-a-date"), "unknown age");
});

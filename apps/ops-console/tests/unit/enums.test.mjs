// Pins the human labels for API enum values (FE QA 2026-09-28 pm): badges,
// filters and table cells no longer print raw wire enums, and Bengali mode
// gets Bengali labels.
import { test } from "node:test";
import assert from "node:assert/strict";

import { enumLabel, humanizeEnum } from "../../.test-dist/lib/i18n/enums.js";

test("known enums get a human label in both languages", () => {
  assert.equal(enumLabel("IN_PROGRESS", "en"), "In progress");
  assert.equal(enumLabel("IN_PROGRESS", "bn"), "চলমান");
  assert.equal(enumLabel("WAITING_EXTERNAL", "en"), "Waiting on external");
  assert.equal(enumLabel("BANGLA_QR", "en"), "Bangla QR");
});

test("unknown enums fall back to a humanised English label, empty to a dash", () => {
  assert.equal(enumLabel("SOMETHING_NEW", "bn"), "Something new");
  assert.equal(humanizeEnum("RtgsMessage"), "RTGS message");
  assert.equal(humanizeEnum("AmlAlert"), "AML alert");
  assert.equal(enumLabel("", "en"), "—");
  assert.equal(enumLabel(null, "en"), "—");
});

// Pins the human labels for API enum values in the portal (FE QA
// 2026-09-28 pm): badges and method cells no longer print raw wire codes.
import { test } from "node:test";
import assert from "node:assert/strict";

import { enumLabel, humanizeEnum } from "../../.test-dist/lib/i18n/enums.js";

test("known enums get a human label in both languages", () => {
  assert.equal(enumLabel("ROTATION_PENDING", "en"), "Rotation pending");
  assert.equal(enumLabel("BANGLA_QR", "en"), "Bangla QR");
  assert.equal(enumLabel("REQUIRES_CONFIRMATION", "bn"), "নিশ্চিতকরণের অপেক্ষায়");
});

test("unknown enums fall back to a humanised label, empty to a dash", () => {
  assert.equal(humanizeEnum("VA_RECON_REPORT"), "VA recon report");
  assert.equal(enumLabel("SOMETHING_NEW", "bn"), "Something new");
  assert.equal(enumLabel(undefined, "en"), "—");
});

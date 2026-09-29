// Pins the portal display name derived from the login email (FE QA 2026-09-29).
import { test } from "node:test";
import assert from "node:assert/strict";

import { displayNameFromEmail } from "../../.test-dist/lib/displayName.js";

test("display name is the title-cased local part of the login email", () => {
  assert.equal(displayNameFromEmail("salma.akter@shop.example"), "Salma Akter");
  assert.equal(displayNameFromEmail("qa_pm2@bdpay.demo"), "Qa Pm2");
  assert.equal(displayNameFromEmail("রহিম@x.bd"), "রহিম");
  assert.equal(displayNameFromEmail("@x.bd"), "@x.bd");
});

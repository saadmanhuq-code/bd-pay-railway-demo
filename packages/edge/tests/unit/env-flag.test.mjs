import { test } from "node:test";
import assert from "node:assert/strict";

import { isMockEnabled } from "../../.test-dist/env-flag.js";

test("mock opt-in accepts only explicit affirmative values", () => {
  for (const value of ["1", "true", "yes", "on", " TRUE "]) {
    assert.equal(isMockEnabled(value), true);
  }
});

test("mock opt-in keeps production mocks disabled for natural false values", () => {
  for (const value of [undefined, "", "0", "false", "off", "no", "anything"]) {
    assert.equal(isMockEnabled(value), false);
  }
});

// BP-23: ops mock case ids must be unique per seed key (list vs detail).
import { test } from "node:test";
import assert from "node:assert/strict";

import { detHex } from "../../.test-dist/lib/mock/detHex.js";

function caseId(key) {
  return `case_${detHex(key)}`;
}

test("detHex distinguishes case seed keys (BP-23)", () => {
  const ids = Array.from({ length: 12 }, (_, i) => caseId(`case-${i + 1}`));
  assert.equal(new Set(ids).size, ids.length);
  assert.notEqual(caseId("case-1"), caseId("case-6"));
  assert.match(caseId("case-1"), /^case_[0-9a-f]{24}$/);
});

test("detHex is deterministic", () => {
  assert.equal(detHex("case-6"), detHex("case-6"));
  assert.equal(detHex("comp-1", 64).length, 64);
});

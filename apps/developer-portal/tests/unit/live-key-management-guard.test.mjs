import { test } from "node:test";
import assert from "node:assert/strict";

import {
  LIVE_KEY_MANAGEMENT_REQUIRES_MERCHANT_AUTH_CODE,
  isLiveApiKeyMutation,
  liveKeyManagementRequiresMerchantAuthEnvelope,
} from "../../.test-dist/lib/api/live-key-management-guard.js";

test("live API-key mutations fail closed instead of forwarding to the gateway", () => {
  const mutationPaths = [
    ["v1", "api-keys"],
    ["v1", "api-keys", "akey_demo", "rotate"],
    ["v1", "api-keys", "akey_demo", "revoke"],
  ];
  let sharedDemoGatewayCalls = 0;

  for (const path of mutationPaths) {
    if (!isLiveApiKeyMutation("POST", path)) {
      sharedDemoGatewayCalls += 1;
      continue;
    }
    const envelope = liveKeyManagementRequiresMerchantAuthEnvelope();
    assert.equal(envelope.error.type, "authorization");
    assert.equal(envelope.error.code, LIVE_KEY_MANAGEMENT_REQUIRES_MERCHANT_AUTH_CODE);
    assert.match(envelope.error.message, /merchant-member authentication/i);
  }

  assert.equal(sharedDemoGatewayCalls, 0);
});

test("live API-key reads are still gateway-forwardable", () => {
  assert.equal(isLiveApiKeyMutation("GET", ["v1", "api-keys"]), false);
});

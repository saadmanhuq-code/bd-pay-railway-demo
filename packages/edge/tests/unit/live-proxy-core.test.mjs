import { test } from "node:test";
import assert from "node:assert/strict";

import {
  copyHeaders,
  errorEnvelope,
  errorType,
  gatewayBase,
  gatewayUrl,
} from "../../.test-dist/live-proxy-core.js";

test("gatewayBase prefers BDPAY_LIVE_API_BASE over BDPAY_GATEWAY_INTERNAL_BASE and strips trailing slashes", () => {
  assert.equal(
    gatewayBase({
      BDPAY_LIVE_API_BASE: "https://live.example//",
      BDPAY_GATEWAY_INTERNAL_BASE: "https://internal.example",
    }),
    "https://live.example",
  );
  assert.equal(gatewayBase({ BDPAY_GATEWAY_INTERNAL_BASE: "https://internal.example///" }), "https://internal.example");
  assert.equal(gatewayBase({}), "");
});

test("gatewayUrl joins the path and search string and returns null without a configured base", () => {
  const previousLive = process.env.BDPAY_LIVE_API_BASE;
  const previousInternal = process.env.BDPAY_GATEWAY_INTERNAL_BASE;
  delete process.env.BDPAY_LIVE_API_BASE;
  delete process.env.BDPAY_GATEWAY_INTERNAL_BASE;
  try {
    assert.equal(gatewayUrl(["v1", "public", "status"], "?x=1"), null);

    process.env.BDPAY_LIVE_API_BASE = "https://gateway.internal.invalid";
    assert.equal(
      gatewayUrl(["v1", "public", "status"], "?x=1"),
      "https://gateway.internal.invalid/v1/public/status?x=1",
    );
    assert.equal(gatewayUrl(["v1", "api-keys"]), "https://gateway.internal.invalid/v1/api-keys");
  } finally {
    if (previousLive === undefined) delete process.env.BDPAY_LIVE_API_BASE;
    else process.env.BDPAY_LIVE_API_BASE = previousLive;
    if (previousInternal === undefined) delete process.env.BDPAY_GATEWAY_INTERNAL_BASE;
    else process.env.BDPAY_GATEWAY_INTERNAL_BASE = previousInternal;
  }
});

test("errorType maps the full status table used across every app", () => {
  assert.equal(errorType(401), "authentication");
  assert.equal(errorType(403), "authorization");
  assert.equal(errorType(404), "not_found");
  assert.equal(errorType(400), "invalid_request");
  assert.equal(errorType(500), "internal");
  assert.equal(errorType(418), "internal");
});

test("errorEnvelope shape", () => {
  assert.deepEqual(errorEnvelope(404, "unknown_route", "Unknown live route."), {
    error: {
      type: "not_found",
      code: "unknown_route",
      message: "Unknown live route.",
      request_id: "frontend_live_adapter",
      doc_url: "https://docs.bdpay.example/errors/unknown_route",
    },
  });
});

test("copyHeaders copies exactly the four passthrough names and nothing else", () => {
  const upstream = new Response(null, {
    headers: {
      "content-type": "application/json",
      "cache-control": "no-store",
      "x-bdpay-api-version": "2026-01-01",
      "x-bdpay-request-id": "req_1",
      "x-other-header": "nope",
      "set-cookie": "session=abc",
    },
  });
  const headers = copyHeaders(upstream);
  assert.equal(headers.get("content-type"), "application/json");
  assert.equal(headers.get("cache-control"), "no-store");
  assert.equal(headers.get("x-bdpay-api-version"), "2026-01-01");
  assert.equal(headers.get("x-bdpay-request-id"), "req_1");
  assert.equal(headers.get("x-other-header"), null);
  assert.equal(headers.get("set-cookie"), null);
  assert.equal([...headers.keys()].length, 4);
});

test("copyHeaders honors a custom name list", () => {
  const upstream = new Response(null, { headers: { "x-custom": "yes", "content-type": "text/plain" } });
  const headers = copyHeaders(upstream, ["x-custom"]);
  assert.equal(headers.get("x-custom"), "yes");
  assert.equal(headers.get("content-type"), null);
});

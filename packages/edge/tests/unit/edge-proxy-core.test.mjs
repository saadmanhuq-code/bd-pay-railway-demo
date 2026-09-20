import { test } from "node:test";
import assert from "node:assert/strict";

import { buildCsp, isPublicPage, mintNonce } from "../../.test-dist/edge-proxy-core.js";

test("isPublicPage matches exact pages and prefix boundaries", () => {
  const config = { exact: ["/login", "/status"], prefixes: ["/pay"] };
  assert.equal(isPublicPage("/login", config), true);
  assert.equal(isPublicPage("/status", config), true);
  assert.equal(isPublicPage("/pay", config), true);
  assert.equal(isPublicPage("/pay/l/abc123", config), true);
  assert.equal(isPublicPage("/payments", config), false, "a same-prefix sibling page must not leak through");
  assert.equal(isPublicPage("/dashboard", config), false);
  assert.equal(isPublicPage("/", config), false);
});

test("isPublicPage with no allow-list denies everything", () => {
  assert.equal(isPublicPage("/login", { exact: [], prefixes: [] }), false);
});

test("buildCsp carries the nonce, strict-dynamic, and never unsafe-inline for scripts", () => {
  const csp = buildCsp("abc123", undefined);
  assert.ok(csp.includes("script-src 'self' 'nonce-abc123' 'strict-dynamic'"));
  const scriptSrcClause = csp.split("; ").find((part) => part.startsWith("script-src"));
  assert.ok(scriptSrcClause);
  assert.ok(!scriptSrcClause.includes("unsafe-inline"));
  assert.ok(csp.includes("frame-ancestors 'none'"));
});

test("buildCsp keeps connect-src self-only with no configured (or a relative) API base", () => {
  assert.ok(buildCsp("n", undefined).includes("; connect-src 'self'; "));
  assert.ok(buildCsp("n", "").includes("; connect-src 'self'; "));
  assert.ok(buildCsp("n", "/api").includes("; connect-src 'self'; "));
  assert.ok(buildCsp("n", "not a url").includes("; connect-src 'self'; "));
});

test("buildCsp adds the gateway origin to connect-src only for an absolute API base", () => {
  const csp = buildCsp("n", "https://gateway.example:8443/v1/foo");
  assert.ok(csp.includes("; connect-src 'self' https://gateway.example:8443; "));
});

test("buildCsp defaults to the process NEXT_PUBLIC_BDPAY_API_BASE the same way the client does", () => {
  // The default parameter is the literal `process.env.NEXT_PUBLIC_BDPAY_API_BASE`
  // expression so Next can inline it at build time (see the comment on
  // gatewayConnectOrigin); under node:test it is a plain runtime read, which
  // is what this asserts.
  const previous = process.env.NEXT_PUBLIC_BDPAY_API_BASE;
  process.env.NEXT_PUBLIC_BDPAY_API_BASE = "https://runtime.example/v1";
  try {
    assert.ok(buildCsp("n").includes("; connect-src 'self' https://runtime.example; "));
  } finally {
    if (previous === undefined) delete process.env.NEXT_PUBLIC_BDPAY_API_BASE;
    else process.env.NEXT_PUBLIC_BDPAY_API_BASE = previous;
  }
});

test("mintNonce returns 24 base64 characters and differs between calls", () => {
  const a = mintNonce();
  const b = mintNonce();
  assert.equal(a.length, 24);
  assert.equal(b.length, 24);
  assert.notEqual(a, b);
  assert.match(a, /^[A-Za-z0-9+/]+={0,2}$/);
});

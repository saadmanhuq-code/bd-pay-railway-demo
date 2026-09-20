# @bdpay/edge

Shared, framework-adjacent code for the three bd-pay Next.js apps
(`apps/developer-portal`, `apps/diner-app`, `apps/ops-console`). This package
replaces what used to be three byte-for-byte (or near byte-for-byte) copies of
the same modules, one per app.

## What lives here

- **`live-proxy-core` / `live-proxy`** — the live gateway BFF proxy core used
  by every app's `app/api/live/[...path]/route.ts`: reading the gateway base
  URL and demo API key from `process.env`, building a gateway URL, the shared
  error envelope (`{ error: { type, code, message, request_id, doc_url } }`),
  copying the passthrough response headers, and forwarding a request to the
  gateway. `live-proxy-core` is pure (no `next` import, safe anywhere);
  `live-proxy` adds the `NextRequest`/`NextResponse`-shaped helpers.
- **`edge-proxy-core` / `edge-proxy`** — the edge auth + CSP-nonce logic each
  app's `proxy.ts` runs before any page renders: the public-page allow-list
  predicate, the Content-Security-Policy builder, and nonce minting.
  `edge-proxy-core` is pure; `edge-proxy` exports `createEdgeProxy(...)`,
  which each app's `proxy.ts` wraps with its own session predicate and
  allow-list.
- **`env-flag`** — `isMockEnabled`, and `mockRouteRefused` (the pure SEC-01
  mock-route decision — see `mock/guard` below), both in one dependency-free
  leaf module (no imports, ever). It is imported from a browser-bundled file
  (`src/lib/api/client.ts` in every app), so it must never grow an import of
  its own — that is the one rule that matters most in this package.
  `mockRouteRefused` lives here rather than in its own file for the same
  reason: a separate pure module under `mock/` would need a relative import
  of `env-flag` from a subdirectory, and there is no single import-specifier
  spelling that satisfies both this package's dependency-free `tsc`
  test-compile (which needs an explicit `.js` extension to run the emitted
  ESM under plain Node) and Turbopack/webpack's bundler resolution (which
  rejects that same `.js` extension pointing at a `.ts` file). Keeping the
  two functions in one already-tested, already-import-free file sidesteps
  the conflict entirely.
- **`mock/signed-session`** — `createSignedSession<TExtra>({ cookieName,
  ttlSeconds, secretEnvVars, devSecret, parseExtra })`, the edge-safe
  HMAC-signed session cookie shared by all three apps' deterministic mock
  login. Pure (no imports at all, Web Crypto only — safe at the edge or in
  Node). Each app's `src/lib/mock/session.ts` is a thin instance built from
  its own payload validator (dp: a fixed persona set; ops: a `sub` prefixed
  `oper_`; diner: a `sub` prefixed `cust_`) and re-exports the exact names it
  exported before this package existed, so nothing else in any app changes.
  See `tests/unit/signed-session.test.mjs` for the oracle test: it
  re-implements the *original* per-app `session.ts` function bodies
  independently of this module and proves token/cookie-option
  byte-for-byte equivalence.
- **`mock/guard`** — `mockDisabled()`, the shared SEC-01 production guard
  (404s the in-app deterministic mock routes in production unless
  `BDPAY_ENABLE_MOCK` is explicitly set) wrapping `mockRouteRefused` from
  `env-flag` in a `NextResponse`. Every app's `src/lib/mock/guard.ts`
  re-exports it unchanged.
- **`mock/totp`** — `verifyMockTotp`, the RFC 6238 TOTP verifier for the
  developer-portal/ops-console mock step-up flow (diner-app has no TOTP: its
  mock login is phone + OTP, a different module). Moved here verbatim from
  the two apps' byte-identical copies; pure `node:crypto`, no `next` import.
- **`ui/DataTable`, `ui/TotpModal`** — the generic sortable table (moved
  here verbatim from developer-portal/ops-console's byte-identical copies)
  and the TOTP step-up confirmation modal as a factory:
  `createTotpModal<CopyKey>({ DualLabel, useLang })`, fed with the consuming
  app's own DualLabel component and useLang hook, so that no module in this
  package imports an app's `@/*` path alias. Each app instantiates it once in
  `src/components/TotpModal.tsx` and its pages import from there. React-bound
  (the package's `peerDependencies` on `react`/`react-dom` exist for the
  `ui/`, `i18n/` and `use-session` modules). Type-checked by this package's
  own `tsconfig.json` as well as by every app's `next build`;
  exercised indirectly through each app's own `next build`.
- **`use-session`** — `createUseSession<TSession>(fetchSession)`, returning
  a `useSession(redirectOnMissing = true)` hook with the same
  loading/reload/redirect-on-missing-session behavior every app had before.
  dp and ops build it from their own `getSession()`; diner-app from
  `getDinerSession()`. Each app keeps its own `src/lib/useSession.ts`
  exporting `useSession`, so no consumer of the hook changes. React/Next-bound
  (`useRouter`), not unit-tested here — see Tests below.
- **`i18n/lang-context`, `i18n/DualLabel`** — `createLangContext({
  storageKey, copy, pick })` and `createDualLabel(copy)`, the bn/en language
  context (localStorage-persisted) and the "render both languages at once"
  component required on safety-critical strings (spec/15 §i18n). Shared
  verbatim between developer-portal and ops-console, which differed only by
  `STORAGE_KEY` and comments; each app's `LangContext.tsx`/`DualLabel.tsx` is
  now a thin instance. **diner-app's copies are intentionally NOT factored
  through this module** — its `LangContext.tsx` differs by more than storage
  key and comments (a Bengali-first default language, no
  navigator-language sniffing) and its `DualLabel.tsx` renders the bn/en
  spans in the opposite order (bn first), both real behavioral differences
  for its Bengali-first surface, not cosmetic ones. React-bound, not
  unit-tested here.
- **`security-headers`** — the static `SECURITY_HEADERS` table and a
  `securityHeadersRoute()` helper for each app's `next.config.mjs`. Written as
  plain `.mjs` (not `.ts`) because `next.config.mjs` is loaded directly by
  Node, not compiled by the Next/TypeScript toolchain.

## Consuming this package

Each app depends on `@bdpay/edge` through the npm workspace (no version to
bump, no publish step) and lists it in `next.config.mjs`:

```js
transpilePackages: ["@bdpay/edge"],
```

`transpilePackages` tells Next to run its own TypeScript/JS transform over
this package's source instead of treating it as a precompiled external
dependency — there is **no build step** here. Apps import straight from the
`.ts`/`.mjs`/`.tsx` sources via this package's `exports` subpaths (e.g.
`@bdpay/edge/live-proxy`, `@bdpay/edge/mock/signed-session`,
`@bdpay/edge/ui/DataTable`), and Next (and each app's own `tsc` program
during `next build`) type-checks those sources directly, against the
*app's* strict + `noUncheckedIndexedAccess` settings.

`react`/`react-dom` are `peerDependencies` (`>=18 <19`), needed only for
`ui/DataTable.tsx` and `ui/TotpModal.tsx`; every other module in this
package is framework-free.

## Tests

`npm test --workspace packages/edge` runs `pretest` (a `tsconfig.test.json`
compile of the pure, dependency-light modules to `.test-dist/`, plus a
generated `.test-dist/package.json` so Node treats the output as ESM) and then
`node --test tests/unit/*.test.mjs`. This mirrors exactly how every app runs
its own unit tests — same two script names, same shape — so there is nothing
package-specific to learn.

`live-proxy.ts`, `edge-proxy.ts` and `mock/guard.ts` (the modules that import
`next/server`) are exercised indirectly through each app's own route/harness
tests rather than compiled here, since `next/server` is not resolvable
outside a Next app. `use-session.ts`, `i18n/lang-context.tsx` and
`i18n/DualLabel.tsx` are React/Next-bound the same way (hooks, context,
`next/navigation`) and are likewise exercised only through each app's own
build. `ui/DataTable.tsx` and `ui/TotpModal.tsx` are plain React but
`TotpModal.tsx`'s `@/*`-aliased imports are not resolvable outside a
consuming app either, so `packages/edge/tsconfig.json`'s `include` is
`src/**/*.ts` only — `.tsx` sources are not covered by this package's own
`npx tsc --noEmit`, only by each app's `next build`.

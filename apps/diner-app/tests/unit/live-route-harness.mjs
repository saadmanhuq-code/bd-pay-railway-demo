// No-network loader for the live BFF route (the E36 reviewer harness, made
// re-runnable in this repo's own test runner).
//
// It executes the UNCHANGED TypeScript route/session/otp/history modules after
// type stripping, with `next/server` replaced by a response/cookie test double
// and `fetch` replaced by a recorder. No Next server, no browser, no gateway,
// no rail — and no network: every fetch is recorded and answered from a queue,
// so a route that tried to reach out would be visible, not silent.
//
// Runs on Node 20 (the version `.gitlab-ci.yml` pins for the diner-app job).

import fs from "node:fs";
import path from "node:path";
import os from "node:os";
import { fileURLToPath, pathToFileURL } from "node:url";
import { createRequire } from "node:module";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const APP = path.resolve(HERE, "..", "..");
const ROUTE_REL = path.join("app", "api", "live", "[...path]", "route.ts");
// The shared @bdpay/edge workspace package the live route imports from (see
// D01 in the lane/332 workorder). Transpiled the same way as the app's own
// src/lib tree, into <outDir>/__edge/, with "@bdpay/edge/<name>" imports
// rewritten to point there — see the `emit` rewrite rule below.
const EDGE_SRC = path.resolve(APP, "..", "..", "packages", "edge", "src");

const require = createRequire(import.meta.url);

// Type stripping has to work on the Node the pipeline pins (`image: node:20`
// for the `edge_apps` job), so the primary path is the `typescript` compiler
// that is ALREADY a devDependency of this app (5.7.3) — no new dependency, and
// `transpileModule` does no type checking, no module resolution and no I/O.
// `node:module`'s built-in `stripTypeScriptTypes` only exists from Node 22.13,
// so it is a fallback for the case where this file is run from a checkout whose
// `apps/diner-app/node_modules` has not been installed.
function selectTranspiler() {
  try {
    const ts = require("typescript");
    return {
      name: `typescript@${ts.version}`,
      strip: (source, fileName) =>
        ts.transpileModule(source, {
          fileName,
          compilerOptions: {
            target: ts.ScriptTarget.ES2022,
            module: ts.ModuleKind.ESNext,
            isolatedModules: true,
            useDefineForClassFields: true,
          },
        }).outputText,
    };
  } catch (error) {
    if (error?.code !== "MODULE_NOT_FOUND") throw error;
  }
  const builtin = require("node:module").stripTypeScriptTypes;
  if (typeof builtin !== "function") {
    throw new Error(
      "No TypeScript transpiler available: install apps/diner-app dependencies " +
        "(`npm ci`) so `typescript` resolves, or run on Node >= 22.13.",
    );
  }
  return { name: "node:module.stripTypeScriptTypes", strip: (source) => builtin(source, { mode: "transform" }) };
}

export const { name: transpilerName, strip: stripTypes } = selectTranspiler();

function transpileTree(outDir) {
  const files = [];
  const walk = (dir) => {
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) walk(full);
      else if (entry.name.endsWith(".ts") && !entry.name.endsWith(".d.ts")) files.push(full);
    }
  };
  walk(path.join(APP, "src", "lib"));

  // The package has no build step of its own (apps consume it straight from
  // source via transpilePackages) — this harness type-strips it exactly the
  // way it strips the app's own src/lib tree, so it never drifts from what
  // `next build` actually does. Recurses into subdirectories (mock/, ui/,
  // i18n/) the same way `walk` does for the app's own src/lib tree, since
  // diner-app's session.ts and otp.ts import from "@bdpay/edge/mock/...".
  const edgeFiles = [];
  const walkEdge = (dir) => {
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) walkEdge(full);
      else if (entry.name.endsWith(".ts") && !entry.name.endsWith(".d.ts")) edgeFiles.push(full);
    }
  };
  walkEdge(EDGE_SRC);

  const emit = (srcFile, outFile) => {
    const source = fs.readFileSync(srcFile, "utf8");
    const js = stripTypes(source, srcFile);
    const rewritten = js.replace(/from\s+["']([^"']+)["']/g, (match, spec) => {
      if (spec === "next/server") {
        let rel = path
          .relative(path.dirname(outFile), path.join(outDir, "next-server.mjs"))
          .split(path.sep)
          .join("/");
        if (!rel.startsWith(".")) rel = `./${rel}`;
        return `from "${rel}"`;
      }
      if (spec.startsWith("@bdpay/edge/")) {
        const name = spec.slice("@bdpay/edge/".length);
        const target = path.join(outDir, "__edge", `${name}.mjs`);
        let rel = path.relative(path.dirname(outFile), target).split(path.sep).join("/");
        if (!rel.startsWith(".")) rel = `./${rel}`;
        return `from "${rel}"`;
      }
      if (spec.startsWith("@/")) {
        const target = path.join(outDir, `${spec.slice(2)}.mjs`);
        let rel = path.relative(path.dirname(outFile), target).split(path.sep).join("/");
        if (!rel.startsWith(".")) rel = `./${rel}`;
        return `from "${rel}"`;
      }
      // A bare relative import (e.g. `./live-proxy-core` inside the package's
      // own live-proxy.ts) resolves, in SOURCE space, to a sibling .ts file
      // with no extension. Node ESM needs an explicit ".mjs" extension, and
      // the sibling's OUTPUT location depends on which source tree it came
      // from (the app's own src/lib, or the @bdpay/edge package) — resolve
      // the source-space target first, then map it into the matching output
      // directory the same way `@/` and `@bdpay/edge/` do above.
      if (spec.startsWith("./") || spec.startsWith("../")) {
        const resolvedSource = path.resolve(path.dirname(srcFile), spec);
        const withinEdge = resolvedSource === EDGE_SRC || resolvedSource.startsWith(EDGE_SRC + path.sep);
        const target = withinEdge
          ? path.join(outDir, "__edge", `${path.relative(EDGE_SRC, resolvedSource)}.mjs`)
          : path.join(outDir, `${path.relative(path.join(APP, "src"), resolvedSource)}.mjs`);
        let rel = path.relative(path.dirname(outFile), target).split(path.sep).join("/");
        if (!rel.startsWith(".")) rel = `./${rel}`;
        return `from "${rel}"`;
      }
      return match;
    });
    fs.mkdirSync(path.dirname(outFile), { recursive: true });
    fs.writeFileSync(outFile, rewritten);
  };

  for (const file of files) {
    const rel = path.relative(path.join(APP, "src"), file).replace(/\.ts$/, ".mjs");
    emit(file, path.join(outDir, rel));
  }
  for (const file of edgeFiles) {
    const rel = path.relative(EDGE_SRC, file).replace(/\.ts$/, ".mjs");
    emit(file, path.join(outDir, "__edge", rel));
  }
  const routeOut = path.join(outDir, "route.mjs");
  emit(path.join(APP, ROUTE_REL), routeOut);
  return routeOut;
}

const NEXT_SERVER_STUB = `
export class NextResponse extends Response {
  constructor(body, init) {
    super(body, init);
    this.setCookies = new Map();
    this.cookies = { set: (name, value) => this.setCookies.set(name, value) };
  }
  static json(body, init) {
    return new NextResponse(JSON.stringify(body), {
      ...init,
      headers: { "content-type": "application/json" },
    });
  }
}
export class NextRequest {}
`;

/**
 * Load the live route with a fake Next boundary and a recording fetch.
 * Returns { route, fetches, enqueue, request }.
 */
export async function loadLiveRoute(env) {
  const outDir = fs.mkdtempSync(path.join(os.tmpdir(), "bdpay-diner-harness-"));
  fs.writeFileSync(path.join(outDir, "next-server.mjs"), NEXT_SERVER_STUB);
  const routeOut = transpileTree(outDir);

  for (const [key, value] of Object.entries(env)) process.env[key] = value;

  const fetches = [];
  const queued = [];
  globalThis.fetch = async (url, init) => {
    fetches.push({ url: String(url), init });
    const next = queued.shift();
    if (next === undefined) {
      return new Response(JSON.stringify({ ok: true }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }
    return next;
  };

  const route = await import(pathToFileURL(routeOut).href);
  return {
    route,
    fetches,
    enqueue(body, status = 200) {
      queued.push(
        new Response(JSON.stringify(body), {
          status,
          headers: { "content-type": "application/json" },
        }),
      );
    },
    cleanup() {
      fs.rmSync(outDir, { recursive: true, force: true });
    },
  };
}

/**
 * Load one `src/lib/**` module from source (type-stripped, no build step), so a
 * check can run straight from the repo root without `npm run pretest`.
 */
export async function loadLibModule(relative) {
  const outDir = fs.mkdtempSync(path.join(os.tmpdir(), "bdpay-diner-lib-"));
  fs.writeFileSync(path.join(outDir, "next-server.mjs"), NEXT_SERVER_STUB);
  transpileTree(outDir);
  return import(pathToFileURL(path.join(outDir, `${relative}.mjs`)).href);
}

export function makeRequest(body = {}, cookie = null, headers = {}) {
  return {
    json: async () => body,
    cookies: { get: () => (cookie ? { value: cookie } : undefined) },
    headers: { get: (name) => headers[name] ?? null },
    nextUrl: { search: "" },
  };
}

export function ctxFor(pathname) {
  return { params: Promise.resolve({ path: pathname.split("/") }) };
}

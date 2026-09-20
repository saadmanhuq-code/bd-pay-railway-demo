#!/usr/bin/env node
// No-write-guard (spec/15 read-only-default rule, bishomiq pattern — same
// guard the developer-portal and ops-console ship).
//
// Scans src/ for fetch calls carrying a non-GET verb outside the single
// allowed module (src/lib/api/client.ts) and fails the build if any exist.
// Also cross-checks: every mutation route in the manifest
// (src/lib/api/mutations.ts) must appear in client.ts, and client.ts must not
// use verbs other than GET/POST.

import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, relative } from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";

const ROOT = fileURLToPath(new URL("..", import.meta.url));
const SRC = join(ROOT, "src");
const CLIENT = join(SRC, "lib", "api", "client.ts");
const MANIFEST = join(SRC, "lib", "api", "mutations.ts");

const DENIED_VERBS = ["POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"];

function walk(dir) {
  const out = [];
  for (const name of readdirSync(dir)) {
    const full = join(dir, name);
    const st = statSync(full);
    if (st.isDirectory()) {
      out.push(...walk(full));
    } else if (/\.(ts|tsx)$/.test(name)) {
      out.push(full);
    }
  }
  return out;
}

const failures = [];

// 1. No non-GET fetch verbs outside client.ts.
const verbRe = new RegExp(`method\\s*:\\s*["'\`](${DENIED_VERBS.join("|")})["'\`]`, "i");
for (const file of walk(SRC)) {
  if (file === CLIENT || file === MANIFEST) continue;
  const text = readFileSync(file, "utf8");
  const lines = text.split("\n");
  lines.forEach((line, i) => {
    if (verbRe.test(line)) {
      failures.push(`${relative(ROOT, file)}:${i + 1}: non-GET fetch verb outside client.ts`);
    }
  });
}

// 2. Manifest cross-check: every manifest path prefix appears in client.ts.
const manifestText = readFileSync(MANIFEST, "utf8");
const clientText = readFileSync(CLIENT, "utf8");
const manifestPaths = [...manifestText.matchAll(/path:\s*"([^"]+)"/g)].map((m) => m[1]);
if (manifestPaths.length === 0) {
  failures.push("mutations.ts: empty mutation manifest");
}
for (const p of manifestPaths) {
  // Compare on the static segments around ":id" parameters.
  const segments = p.split("/:id");
  const allPresent = segments.every((seg) => seg === "" || clientText.includes(seg));
  if (!allPresent) {
    failures.push(`mutations.ts: manifest route ${p} has no matching call in client.ts`);
  }
}

// 3. client.ts must use only GET and POST.
const badClientVerb = clientText.match(/method\s*:\s*["'`](PUT|PATCH|DELETE)["'`]/i);
if (badClientVerb) {
  failures.push(`client.ts uses forbidden verb ${badClientVerb[1]}`);
}

if (failures.length > 0) {
  for (const f of failures) console.error(`no-write-guard: ${f}`);
  console.error(`no-write-guard: ${failures.length} violation(s).`);
  process.exit(1);
}
console.log(`no-write-guard: clean (${manifestPaths.length} registered mutation routes)`);

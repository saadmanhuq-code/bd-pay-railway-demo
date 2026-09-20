// Deterministic FNV-1a hex id helper for the ops-console mock store.
// Kept in its own module so unit tests can pin uniqueness without compiling
// the full store (which pulls API types via path aliases).

const SEED = "bdpay-ops-console-mock-seed-v1";

/** FNV-1a derived deterministic hex id suffix.
 *
 * Defect fixed BP-23 (ops Cases list vs detail type mismatch): the previous
 * expansion loop only ever read the first `length/4` characters of `src` —
 * all inside the constant SEED prefix — so EVERY `case_*` (and every other
 * same-prefix id) shared one digest. List rows looked like distinct types
 * (RTGS_MANUAL, COMPENSATION, …) but all linked to the same case_id, so
 * detail `findCase` always returned the first seed (COMPENSATION /
 * CompensationQueueItem). Same class of bug as portal S18-E20. Digest now
 * absorbs the full input first (true FNV-1a via Math.imul), then expands.
 */
export function detHex(input: string, length = 24): string {
  let h = 0x811c9dc5;
  const src = `${SEED}:${input}`;
  for (let i = 0; i < src.length; i++) {
    h ^= src.charCodeAt(i) & 0xff;
    h = Math.imul(h, 0x01000193) >>> 0;
  }
  let out = "";
  let round = 0;
  while (out.length < length) {
    h ^= round & 0xff;
    h = Math.imul(h, 0x01000193) >>> 0;
    out += (h & 0xffff).toString(16).padStart(4, "0");
    round++;
  }
  return out.slice(0, length);
}

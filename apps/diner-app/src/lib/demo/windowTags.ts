// Classify offer time windows into EatClub-style discovery chips.
// Pure helper — shared by the mock directory and the client demo catalog.

import type { OfferWindow } from "@/lib/offers/engine";
import type { WindowTag } from "@/lib/api/types";

function hmToMinutes(hm: string): number {
  const parts = hm.split(":");
  const h = Number(parts[0]);
  const m = Number(parts[1] ?? 0);
  if (!Number.isFinite(h) || !Number.isFinite(m)) return 0;
  return h * 60 + m;
}

/** Map a single local window onto zero-or-more discovery tags. */
export function tagsForWindow(w: Pick<OfferWindow, "start_local" | "end_local">): WindowTag[] {
  const start = hmToMinutes(w.start_local);
  const end = hmToMinutes(w.end_local);
  const tags: WindowTag[] = [];
  // Lunch: starts 11:00–14:00
  if (start >= 11 * 60 && start < 14 * 60) tags.push("lunch");
  // Early bird: afternoon off-peak start 14:00–16:00
  if (start >= 14 * 60 && start < 16 * 60) tags.push("early_bird");
  // Happy hour: 16:00–19:00
  if (start >= 16 * 60 && start < 19 * 60) tags.push("happy_hour");
  // Late: starts at/after 19:00, or runs past 22:00
  if (start >= 19 * 60 || end >= 22 * 60) tags.push("late");
  // All-day / very wide windows → surface lunch + late as "open windows"
  if (start <= 60 && end >= 23 * 60) {
    for (const tag of ["lunch", "early_bird", "happy_hour", "late"] as WindowTag[]) {
      if (!tags.includes(tag)) tags.push(tag);
    }
  }
  return tags;
}

export function tagsForWindows(windows: Array<Pick<OfferWindow, "start_local" | "end_local">>): WindowTag[] {
  const seen = new Set<WindowTag>();
  for (const w of windows) {
    for (const tag of tagsForWindow(w)) seen.add(tag);
  }
  return [...seen];
}

#!/usr/bin/env node
/**
 * Refresh the Dhaka OSM restaurant/cafe/fast_food cache for diner-app.
 *
 * Usage (from apps/diner-app):
 *   node scripts/fetch-osm-dhaka.mjs
 *
 * Tries several Overpass mirrors with a proper User-Agent. On full-bbox
 * failure, tiles smaller neighborhood bboxes and merges. Writes:
 *   src/lib/demo/osm-dhaka-restaurants.json
 *   public/data/osm-dhaka-restaurants.json
 *   src/lib/demo/osm-dhaka-restaurants.raw-meta.json
 *
 * Attribution: © OpenStreetMap contributors, ODbL
 * https://www.openstreetmap.org/copyright
 */

import { writeFileSync, mkdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = fileURLToPath(new URL("..", import.meta.url));
const UA =
  "bd-pay-diner-app/1.0 (github.com/saadmanhuq-code/bd-pay-railway-demo; osm-cache refresh)";

const ENDPOINTS = [
  "https://overpass-api.de/api/interpreter",
  "https://overpass.kumi.systems/api/interpreter",
  "https://overpass.openstreetmap.ru/api/interpreter",
];

const BBOX = [23.65, 90.3, 23.95, 90.55]; // s,w,n,e

const TILES = {
  Gulshan: [23.78, 90.405, 23.805, 90.43],
  Banani: [23.788, 90.398, 23.802, 90.412],
  Dhanmondi: [23.735, 90.365, 23.76, 90.39],
  Uttara: [23.85, 90.355, 23.895, 90.41],
  Mirpur: [23.795, 90.345, 23.845, 90.38],
  "Old Dhaka": [23.695, 90.39, 23.73, 90.425],
};

const NEIGHBORHOODS = [
  ["Banani", "বনানী", 23.788, 90.398, 23.802, 90.412],
  ["Baridhara", "বারিধারা", 23.8, 90.415, 23.815, 90.435],
  ["Gulshan", "গুলশান", 23.78, 90.405, 23.805, 90.43],
  ["Bashundhara", "বসুন্ধরা", 23.81, 90.418, 23.835, 90.45],
  ["Lalmatia", "লালমাটিয়া", 23.75, 90.36, 23.765, 90.375],
  ["Dhanmondi", "ধানমন্ডি", 23.735, 90.365, 23.76, 90.39],
  ["Mohammadpur", "মোহাম্মদপুর", 23.755, 90.35, 23.78, 90.375],
  ["Mirpur", "মিরপুর", 23.795, 90.345, 23.845, 90.38],
  ["Uttara", "উত্তরা", 23.85, 90.355, 23.895, 90.41],
  ["Old Dhaka", "পুরান ঢাকা", 23.695, 90.39, 23.73, 90.425],
  ["Wari", "ওয়ারী", 23.71, 90.41, 23.725, 90.425],
  ["Motijheel", "মতিঝিল", 23.725, 90.41, 23.745, 90.43],
  ["Farmgate", "ফার্মগেট", 23.75, 90.38, 23.765, 90.4],
  ["Tejgaon", "তেজগাঁও", 23.755, 90.39, 23.775, 90.41],
  ["Rampura", "রামপুরা", 23.755, 90.415, 23.775, 90.435],
  ["Badda", "বাড্ডা", 23.77, 90.415, 23.795, 90.445],
  ["Malibagh", "মালিবাগ", 23.745, 90.405, 23.76, 90.42],
  ["Shantinagar", "শান্তিনগর", 23.735, 90.405, 23.75, 90.42],
  ["Khilgaon", "খিলগাঁও", 23.745, 90.42, 23.765, 90.44],
];

const CUISINE_MAP = [
  [/biryani|biriyani|kacchi/i, "Biryani"],
  [/kebab|kabab|seekh|tikka/i, "Kabab"],
  [/burger|hamburger/i, "Burgers"],
  [/pizza/i, "Pizza"],
  [/thai/i, "Thai"],
  [/chinese|szechuan|sichuan/i, "Chinese"],
  [/cafe|coffee|tea/i, "Cafe"],
  [/bangladeshi|bengali|indian|curry/i, "Bangladeshi"],
];

const CUISINE_BN = {
  Bangladeshi: "বাংলাদেশি",
  Biryani: "বিরিয়ানি",
  Cafe: "ক্যাফে",
  Chinese: "চাইনিজ",
  Thai: "থাই",
  Burgers: "বার্গার",
  Pizza: "পিৎজা",
  Kabab: "কাবাব",
};

const AMENITY_DEFAULT = {
  cafe: ["Cafe"],
  fast_food: ["Burgers"],
  food_court: ["Bangladeshi"],
  restaurant: ["Bangladeshi"],
};

function queryForBbox([s, w, n, e]) {
  return `[out:json][timeout:90];(
  node["amenity"~"^(restaurant|cafe|fast_food|food_court)$"](${s},${w},${n},${e});
  way["amenity"~"^(restaurant|cafe|fast_food|food_court)$"](${s},${w},${n},${e});
);out center tags;`;
}

async function postOverpass(endpoint, query) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 120_000);
  try {
    const res = await fetch(endpoint, {
      method: "POST",
      headers: {
        "User-Agent": UA,
        "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
      },
      body: `data=${encodeURIComponent(query)}`,
      signal: ctrl.signal,
    });
    if (!res.ok) throw new Error(`${endpoint} HTTP ${res.status}`);
    return await res.json();
  } finally {
    clearTimeout(timer);
  }
}

async function fetchElements() {
  const q = queryForBbox(BBOX);
  for (const ep of ENDPOINTS) {
    try {
      console.log(`Trying ${ep} (full bbox)…`);
      const data = await postOverpass(ep, q);
      const els = data?.elements ?? [];
      if (els.length > 0) {
        console.log(`  OK — ${els.length} elements`);
        return { elements: els, meta: data, mode: "bbox" };
      }
      console.log("  empty result");
    } catch (err) {
      console.warn(`  fail: ${err?.message || err}`);
    }
  }

  console.log("Falling back to neighborhood tiles…");
  const merged = new Map();
  let lastMeta = null;
  for (const [name, box] of Object.entries(TILES)) {
    for (const ep of ENDPOINTS) {
      try {
        console.log(`  tile ${name} via ${ep}…`);
        const data = await postOverpass(ep, queryForBbox(box));
        lastMeta = data;
        for (const el of data?.elements ?? []) {
          merged.set(`${el.type}/${el.id}`, el);
        }
        console.log(`    got ${(data?.elements ?? []).length}`);
        break;
      } catch (err) {
        console.warn(`    fail: ${err?.message || err}`);
      }
    }
  }
  return { elements: [...merged.values()], meta: lastMeta || {}, mode: "tiles" };
}

function guessArea(lat, lng) {
  for (const [name, nameBn, s, w, n, e] of NEIGHBORHOODS) {
    if (lat >= s && lat <= n && lng >= w && lng <= e) return [name, nameBn];
  }
  return ["Dhaka", "ঢাকা"];
}

function mapTags(cuisineRaw, amenity, name) {
  const tags = [];
  const blob = `${cuisineRaw || ""} ${name || ""}`;
  for (const [rx, tag] of CUISINE_MAP) {
    if (rx.test(blob) && !tags.includes(tag)) tags.push(tag);
  }
  if (tags.length === 0) tags.push(...(AMENITY_DEFAULT[amenity] || ["Bangladeshi"]));
  return tags.slice(0, 4);
}

function cuisineLabel(tags) {
  if (!tags.length) return ["Restaurant", "রেস্তোরাঁ"];
  return [tags.join(" · "), tags.map((t) => CUISINE_BN[t] || t).join(" · ")];
}

function clean(elements) {
  const venues = [];
  const seen = new Set();
  const byAmenity = {};
  const byArea = {};
  let skippedNoName = 0;
  let skippedNoCoords = 0;

  for (const el of elements) {
    const tags = el.tags || {};
    const name = (tags.name || tags["name:en"] || "").trim();
    if (!name) {
      skippedNoName++;
      continue;
    }
    const amenity = tags.amenity;
    if (!["restaurant", "cafe", "fast_food", "food_court"].includes(amenity)) continue;
    let lat;
    let lng;
    if (el.type === "node") {
      lat = el.lat;
      lng = el.lon;
    } else {
      lat = el.center?.lat;
      lng = el.center?.lon;
    }
    if (lat == null || lng == null) {
      skippedNoCoords++;
      continue;
    }
    const id = `osm/${el.type}/${el.id}`;
    if (seen.has(id)) continue;
    seen.add(id);

    const nameBn = (tags["name:bn"] || name).trim();
    const suburb = (
      tags["addr:suburb"] ||
      tags["addr:neighbourhood"] ||
      tags["addr:neighborhood"] ||
      ""
    ).trim();
    let area;
    let areaBn;
    if (suburb) {
      area = suburb;
      areaBn = suburb;
      for (const [n, bn] of NEIGHBORHOODS) {
        if (n.toLowerCase() === suburb.toLowerCase()) {
          area = n;
          areaBn = bn;
          break;
        }
      }
    } else {
      [area, areaBn] = guessArea(Number(lat), Number(lng));
    }

    const cuisineRaw = tags.cuisine || "";
    const cuisineTags = mapTags(cuisineRaw, amenity, name);
    const [cuisine, cuisineBn] = cuisineLabel(cuisineTags);

    const venue = {
      id,
      amenity,
      name,
      name_bn: nameBn,
      lat: Math.round(Number(lat) * 1e6) / 1e6,
      lng: Math.round(Number(lng) * 1e6) / 1e6,
      cuisine_tags: cuisineTags,
      cuisine,
      cuisine_bn: cuisineBn,
      area,
      area_bn: areaBn,
      city: "Dhaka",
      city_bn: "ঢাকা",
    };
    const phone = tags.phone || tags["contact:phone"];
    const website = tags.website || tags["contact:website"];
    if (phone) venue.phone = phone;
    if (website) venue.website = website;
    if (tags.opening_hours) venue.opening_hours = tags.opening_hours;
    if (cuisineRaw) venue.cuisine_raw = cuisineRaw;

    venues.push(venue);
    byAmenity[amenity] = (byAmenity[amenity] || 0) + 1;
    byArea[area] = (byArea[area] || 0) + 1;
  }

  venues.sort((a, b) => a.area.localeCompare(b.area) || a.name.localeCompare(b.name));
  const byAreaSorted = Object.fromEntries(
    Object.entries(byArea).sort((a, b) => b[1] - a[1]),
  );

  return {
    attribution: "© OpenStreetMap contributors, ODbL",
    attribution_url: "https://www.openstreetmap.org/copyright",
    source: "Overpass API",
    bbox: BBOX,
    counts: {
      total: venues.length,
      by_amenity: byAmenity,
      by_area: byAreaSorted,
      skipped_no_name: skippedNoName,
      skipped_no_coords: skippedNoCoords,
      raw_elements: elements.length,
    },
    venues,
  };
}

async function main() {
  const { elements, meta, mode } = await fetchElements();
  if (elements.length === 0) {
    console.error("No OSM elements fetched — leaving existing cache untouched.");
    process.exit(1);
  }
  const cleaned = clean(elements);
  cleaned.fetch_mode = mode;

  const libPath = join(ROOT, "src/lib/demo/osm-dhaka-restaurants.json");
  const pubPath = join(ROOT, "public/data/osm-dhaka-restaurants.json");
  const metaPath = join(ROOT, "src/lib/demo/osm-dhaka-restaurants.raw-meta.json");
  mkdirSync(dirname(libPath), { recursive: true });
  mkdirSync(dirname(pubPath), { recursive: true });

  const text = JSON.stringify(cleaned);
  writeFileSync(libPath, text, "utf8");
  writeFileSync(pubPath, text, "utf8");
  writeFileSync(
    metaPath,
    JSON.stringify(
      {
        attribution: "© OpenStreetMap contributors, ODbL",
        overpass_generator: meta?.generator ?? null,
        osm3s: meta?.osm3s ?? null,
        element_count: elements.length,
        fetch_mode: mode,
        bbox_query: "amenity~restaurant|cafe|fast_food|food_court (23.65,90.30,23.95,90.55)",
        note: "Full raw Overpass JSON is not committed; re-run this script to refresh.",
      },
      null,
      2,
    ),
    "utf8",
  );

  console.log(`Wrote ${cleaned.counts.total} venues → ${libPath}`);
  console.log("by_amenity", cleaned.counts.by_amenity);
  console.log("top areas", Object.entries(cleaned.counts.by_area).slice(0, 10));
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});

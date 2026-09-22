// OpenStreetMap-backed public dining catalog for Dhaka.
// Data: src/lib/demo/osm-dhaka-restaurants.json (refresh via scripts/fetch-osm-dhaka.mjs)
// Attribution: © OpenStreetMap contributors, ODbL — https://www.openstreetmap.org/copyright
//
// Photos are picsum placeholders keyed by OSM id (OSM rarely has photos).
// Offer windows are synthetic demo terms so EatClub %/time chips still render.

import type {
  CuisineTag,
  DinerMerchant,
  EligibleOffer,
  WindowTag,
} from "@/lib/api/types";
import { tagsForWindows } from "@/lib/demo/windowTags";
import osmCache from "@/lib/demo/osm-dhaka-restaurants.json";

export const OSM_ATTRIBUTION = "© OpenStreetMap contributors";
export const OSM_ATTRIBUTION_FULL =
  (osmCache as { attribution?: string }).attribution ??
  "© OpenStreetMap contributors, ODbL";

interface OsmVenue {
  id: string;
  amenity: string;
  name: string;
  name_bn: string;
  lat: number;
  lng: number;
  cuisine_tags: CuisineTag[];
  cuisine: string;
  cuisine_bn: string;
  area: string;
  area_bn: string;
  city: string;
  city_bn: string;
  phone?: string;
  website?: string;
  opening_hours?: string;
  cuisine_raw?: string;
}

interface OsmCacheFile {
  attribution: string;
  counts: { total: number; by_amenity: Record<string, number>; by_area: Record<string, number> };
  venues: OsmVenue[];
}

const cache = osmCache as OsmCacheFile;

const VALID_UNTIL = "2026-12-31T18:00:00Z";

// OSM area names are sourced from free-form tags. Keep the source data intact,
// but normalize known neighborhood casing at the catalog boundary so filters,
// chips, cards, and detail pages all use the same display value.
const OSM_AREA_LABELS_EN: Record<string, string> = {
  korail: "Korail",
  "করাইল": "Korail",
};

const OSM_AREA_LABELS_BN: Record<string, string> = {
  korail: "করাইল",
  "করাইল": "করাইল",
};

function areaLabelEn(area: string): string {
  return OSM_AREA_LABELS_EN[area.toLowerCase()] ?? OSM_AREA_LABELS_EN[area] ?? area;
}

function areaLabelBn(area: string): string {
  return OSM_AREA_LABELS_BN[area.toLowerCase()] ?? OSM_AREA_LABELS_BN[area] ?? area;
}

const OFFER_TEMPLATES: Array<{
  kind: "PERCENT_OFF";
  title: string;
  title_bn: string;
  percent_bps: number;
  windows: EligibleOffer["windows"];
  min_spend_minor: number;
  max_discount_minor: number;
}> = [
  {
    kind: "PERCENT_OFF",
    title: "Lunch 15% off (demo terms)",
    title_bn: "লাঞ্চে ১৫% ছাড় (ডেমো শর্ত)",
    percent_bps: 1500,
    windows: [
      {
        days: ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"],
        start_local: "11:30",
        end_local: "15:00",
      },
    ],
    min_spend_minor: 30000,
    max_discount_minor: 20000,
  },
  {
    kind: "PERCENT_OFF",
    title: "Afternoon early-bird 20% off (demo terms)",
    title_bn: "বিকেলের আর্লি-বার্ড ২০% ছাড় (ডেমো শর্ত)",
    percent_bps: 2000,
    windows: [
      {
        days: ["MON", "TUE", "WED", "THU", "FRI"],
        start_local: "14:30",
        end_local: "17:30",
      },
    ],
    min_spend_minor: 40000,
    max_discount_minor: 25000,
  },
  {
    kind: "PERCENT_OFF",
    title: "Happy hour 25% off (demo terms)",
    title_bn: "হ্যাপি আওয়ার ২৫% ছাড় (ডেমো শর্ত)",
    percent_bps: 2500,
    windows: [{ days: ["MON", "TUE", "WED", "THU", "FRI", "SAT"], start_local: "16:00", end_local: "19:00" }],
    min_spend_minor: 50000,
    max_discount_minor: 30000,
  },
  {
    kind: "PERCENT_OFF",
    title: "Late night 20% off (demo terms)",
    title_bn: "লেট নাইট ২০% ছাড় (ডেমো শর্ত)",
    percent_bps: 2000,
    windows: [
      {
        days: ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"],
        start_local: "21:00",
        end_local: "23:30",
      },
    ],
    min_spend_minor: 35000,
    max_discount_minor: 25000,
  },
  {
    kind: "PERCENT_OFF",
    title: "Weekend 30% off (demo terms)",
    title_bn: "উইকেন্ড ৩০% ছাড় (ডেমো শর্ত)",
    percent_bps: 3000,
    windows: [{ days: ["SAT", "SUN"], start_local: "12:00", end_local: "22:00" }],
    min_spend_minor: 60000,
    max_discount_minor: 40000,
  },
];

function hashId(id: string): number {
  let h = 0;
  for (let i = 0; i < id.length; i++) h = (h * 31 + id.charCodeAt(i)) >>> 0;
  return h;
}

function picsum(seed: string, w = 640, h = 360): string {
  // Reuse a small deterministic pool. Unique-per-venue seeds stampede picsum
  // under browse load (dozens of redirects) and leave broken merchant thumbs.
  const pool = hashId(seed) % 24;
  return `https://picsum.photos/seed/bdpay-venue-${pool}/${w}/${h}`;
}

function offersForVenue(venueId: string): EligibleOffer[] {
  const h = hashId(venueId);
  const primary = OFFER_TEMPLATES[h % OFFER_TEMPLATES.length]!;
  const secondary = OFFER_TEMPLATES[(h + 2) % OFFER_TEMPLATES.length]!;
  const pick = primary === secondary ? [primary] : [primary, secondary];
  return pick.map((t, i) => ({
    offer_id: `osm_off_${venueId.replace(/\//g, "_")}_${i}`,
    kind: t.kind,
    title: t.title,
    title_bn: t.title_bn,
    percent_bps: t.percent_bps,
    windows: t.windows,
    min_spend_minor: t.min_spend_minor,
    max_discount_minor: t.max_discount_minor,
    allowed_methods: ["BANGLA_QR", "BKASH", "NAGAD"],
    valid_until: VALID_UNTIL,
  }));
}

function buildMerchant(v: OsmVenue): DinerMerchant {
  const offers = offersForVenue(v.id);
  const best = offers.reduce<number | null>(
    (acc, o) =>
      o.percent_bps !== null && (acc === null || o.percent_bps > acc) ? o.percent_bps : acc,
    null,
  );
  const seed = v.id.replace(/\//g, "-");
  return {
    merchant_id: v.id,
    display_name: v.name,
    display_name_bn: v.name_bn || v.name,
    area: areaLabelEn(v.area),
    area_bn: areaLabelBn(v.area_bn || v.area),
    cuisine: v.cuisine,
    cuisine_bn: v.cuisine_bn,
    live_offer_count: offers.length,
    best_percent_bps: best,
    window_tags: tagsForWindows(offers.flatMap((o) => o.windows)),
    city: v.city || "Dhaka",
    city_bn: v.city_bn || "ঢাকা",
    lat: v.lat,
    lng: v.lng,
    cuisine_tags: v.cuisine_tags,
    photo_urls: [picsum(`${seed}-1`), picsum(`${seed}-2`)],
  };
}

const OSM_VENUES: OsmVenue[] = (Array.isArray(cache.venues) ? cache.venues : []) as OsmVenue[];

export const OSM_MERCHANTS: DinerMerchant[] = OSM_VENUES.map(buildMerchant);

const OSM_BY_ID = new Map(OSM_MERCHANTS.map((m) => [m.merchant_id, m]));

export function osmVenueCount(): number {
  return OSM_MERCHANTS.length;
}

export function osmCounts(): OsmCacheFile["counts"] | null {
  return cache.counts ?? null;
}

export function isOsmMerchantId(merchantId: string): boolean {
  return merchantId.startsWith("osm/");
}

export function getOsmMerchant(merchantId: string): DinerMerchant | null {
  return OSM_BY_ID.get(merchantId) ?? null;
}

export function listOsmOffers(merchantId: string): EligibleOffer[] {
  if (!OSM_BY_ID.has(merchantId)) return [];
  return offersForVenue(merchantId);
}

export function listOsmMerchants(params: {
  area?: string;
  q?: string;
  windowTag?: WindowTag | "";
  city?: string;
  cuisine?: CuisineTag | "";
}): DinerMerchant[] {
  const area = params.area ?? "";
  const qRaw = params.q ?? "";
  const q = qRaw.trim().toLowerCase();
  const tag = params.windowTag ?? "";
  const city = params.city ?? "";
  const cuisine = params.cuisine ?? "";
  return OSM_MERCHANTS.filter((m) => {
    if (city && (m.city ?? "Dhaka") !== city) return false;
    if (area && m.area !== area) return false;
    if (tag && !m.window_tags.includes(tag)) return false;
    if (cuisine && !(m.cuisine_tags ?? []).includes(cuisine)) return false;
    if (q) {
      if (
        !m.display_name.toLowerCase().includes(q) &&
        !m.display_name_bn.includes(qRaw) &&
        !m.area.toLowerCase().includes(q) &&
        !m.area_bn.includes(qRaw) &&
        !m.cuisine.toLowerCase().includes(q) &&
        !m.cuisine_bn.includes(qRaw) &&
        !(m.cuisine_tags ?? []).some((c) => c.toLowerCase().includes(q))
      ) {
        return false;
      }
    }
    return true;
  });
}

export function osmAreasForCity(city: string): Array<[string, string]> {
  const seen = new Map<string, string>();
  for (const m of OSM_MERCHANTS) {
    if ((m.city ?? "Dhaka") !== city) continue;
    if (!seen.has(m.area)) seen.set(m.area, m.area_bn);
  }
  return [...seen.entries()].sort((a, b) => a[0].localeCompare(b[0]));
}

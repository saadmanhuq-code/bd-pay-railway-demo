// Discovery helper: try the configured API first; if the live gateway has no
// public merchant directory (empty/401/404/network), fall back to the local
// demo catalog so unauthenticated browse still works for demos.

import { eligibleOffers, listDinerMerchants } from "@/lib/api/client";
import type { CuisineTag, DinerMerchant, EligibleOffer, WindowTag } from "@/lib/api/types";
import {
  getDemoMerchant,
  isDemoMerchantId,
  listDemoMerchants,
  listDemoOffers,
} from "@/lib/demo/catalog";

export type DiscoverSource = "api" | "demo";

export interface MerchantDiscoverResult {
  merchants: DinerMerchant[];
  source: DiscoverSource;
}

export interface OfferDiscoverResult {
  merchant: DinerMerchant | null;
  offers: EligibleOffer[];
  source: DiscoverSource;
}

function withTags(m: DinerMerchant): DinerMerchant {
  return { ...m, window_tags: m.window_tags ?? [] };
}

export async function discoverMerchants(params: {
  area?: string;
  q?: string;
  windowTag?: WindowTag | "";
  city?: string;
  cuisine?: CuisineTag | "";
}): Promise<MerchantDiscoverResult> {
  const tag = params.windowTag ?? "";
  const cuisine = params.cuisine ?? "";
  const city = params.city ?? "";
  try {
    const page = await listDinerMerchants({ area: params.area, q: params.q });
    let rows = (page.data ?? []).map(withTags);
    if (tag) rows = rows.filter((m) => m.window_tags.includes(tag));
    if (city) rows = rows.filter((m) => (m.city ?? "Dhaka") === city);
    if (cuisine) rows = rows.filter((m) => (m.cuisine_tags ?? []).includes(cuisine));
    if (rows.length > 0) return { merchants: rows, source: "api" };
  } catch {
    // fall through to demo
  }
  return {
    merchants: listDemoMerchants({
      area: params.area,
      q: params.q,
      windowTag: tag,
      city,
      cuisine,
    }),
    source: "demo",
  };
}

export async function discoverMerchantOffers(merchantId: string): Promise<OfferDiscoverResult> {
  if (isDemoMerchantId(merchantId)) {
    return {
      merchant: getDemoMerchant(merchantId),
      offers: listDemoOffers(merchantId),
      source: "demo",
    };
  }
  try {
    const [page, eligible] = await Promise.all([
      listDinerMerchants({}),
      eligibleOffers({ merchant_id: merchantId }),
    ]);
    const merchant =
      (page.data ?? []).map(withTags).find((m) => m.merchant_id === merchantId) ?? null;
    const offers = eligible.data ?? [];
    if (merchant || offers.length > 0) {
      return { merchant, offers, source: "api" };
    }
  } catch {
    // fall through
  }
  const demoMerchant = getDemoMerchant(merchantId);
  if (demoMerchant) {
    return { merchant: demoMerchant, offers: listDemoOffers(merchantId), source: "demo" };
  }
  return { merchant: null, offers: [], source: "demo" };
}

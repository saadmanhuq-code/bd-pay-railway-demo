// Local demo dining catalog — used when the live gateway has no public
// merchant directory. Clearly labeled in the UI as a demo/simulator so the
// CEO can still walk the EatClub-style discovery flow.

import type { DinerMerchant, EligibleOffer, WindowTag } from "@/lib/api/types";
import { tagsForWindows } from "@/lib/demo/windowTags";

interface DemoOffer extends EligibleOffer {
  merchant_id: string;
}

const VALID_UNTIL = "2026-12-31T18:00:00Z";

const DEMO_OFFERS: DemoOffer[] = [
  {
    merchant_id: "demo_mrch_kacchi_bhai",
    offer_id: "demo_off_kacchi_weekday",
    kind: "PERCENT_OFF",
    title: "Weekday afternoon 25% off",
    title_bn: "সাপ্তাহিক দুপুরে ২৫% ছাড়",
    percent_bps: 2500,
    windows: [{ days: ["MON", "TUE", "WED", "THU"], start_local: "14:30", end_local: "18:00" }],
    min_spend_minor: 50000,
    max_discount_minor: 40000,
    allowed_methods: ["BANGLA_QR", "BKASH", "NAGAD"],
    valid_until: VALID_UNTIL,
  },
  {
    merchant_id: "demo_mrch_kacchi_bhai",
    offer_id: "demo_off_kacchi_weekend",
    kind: "PERCENT_OFF",
    title: "Weekend off-peak 20% off",
    title_bn: "সাপ্তাহিক ছুটির কম-ভিড়ে ২০% ছাড়",
    percent_bps: 2000,
    windows: [{ days: ["SAT", "SUN"], start_local: "15:00", end_local: "18:30" }],
    min_spend_minor: 50000,
    max_discount_minor: 30000,
    allowed_methods: ["BANGLA_QR", "BKASH", "NAGAD"],
    valid_until: VALID_UNTIL,
  },
  {
    merchant_id: "demo_mrch_sultans_dine",
    offer_id: "demo_off_sultan_weekend",
    kind: "PERCENT_OFF",
    title: "Weekend 30% off (all day)",
    title_bn: "ছুটির দিনে সারাদিন ৩০% ছাড়",
    percent_bps: 3000,
    windows: [{ days: ["SAT", "SUN"], start_local: "00:00", end_local: "23:59" }],
    min_spend_minor: 100000,
    max_discount_minor: 40000,
    allowed_methods: ["BANGLA_QR", "BKASH"],
    valid_until: VALID_UNTIL,
  },
  {
    merchant_id: "demo_mrch_star_kabab",
    offer_id: "demo_off_star_lunch",
    kind: "PERCENT_OFF",
    title: "Lunch 10% off",
    title_bn: "দুপুরের খাবারে ১০% ছাড়",
    percent_bps: 1000,
    windows: [
      {
        days: ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"],
        start_local: "11:30",
        end_local: "15:00",
      },
    ],
    min_spend_minor: 30000,
    max_discount_minor: 20000,
    allowed_methods: ["BANGLA_QR", "NAGAD"],
    valid_until: VALID_UNTIL,
  },
  {
    merchant_id: "demo_mrch_star_kabab",
    offer_id: "demo_off_star_dinner",
    kind: "PERCENT_OFF",
    title: "Dinner 15% off",
    title_bn: "রাতের খাবারে ১৫% ছাড়",
    percent_bps: 1500,
    windows: [
      {
        days: ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"],
        start_local: "19:00",
        end_local: "22:30",
      },
    ],
    min_spend_minor: 30000,
    max_discount_minor: 20000,
    allowed_methods: ["BANGLA_QR", "NAGAD"],
    valid_until: VALID_UNTIL,
  },
  {
    merchant_id: "demo_mrch_pizza_roma",
    offer_id: "demo_off_pizza_sat",
    kind: "PERCENT_OFF",
    title: "Saturday evening 50% off",
    title_bn: "শনিবার সন্ধ্যায় ৫০% ছাড়",
    percent_bps: 5000,
    windows: [{ days: ["SAT"], start_local: "16:00", end_local: "19:00" }],
    min_spend_minor: 80000,
    max_discount_minor: 60000,
    allowed_methods: ["BANGLA_QR"],
    valid_until: VALID_UNTIL,
  },
  {
    merchant_id: "demo_mrch_chillox",
    offer_id: "demo_off_chillox_late",
    kind: "PERCENT_OFF",
    title: "Late night burger 20% off",
    title_bn: "রাতের বার্গারে ২০% ছাড়",
    percent_bps: 2000,
    windows: [
      {
        days: ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"],
        start_local: "21:00",
        end_local: "23:30",
      },
    ],
    min_spend_minor: 40000,
    max_discount_minor: 25000,
    allowed_methods: ["BANGLA_QR", "BKASH", "NAGAD"],
    valid_until: VALID_UNTIL,
  },
  {
    merchant_id: "demo_mrch_haji_biriyani",
    offer_id: "demo_off_haji_lunch",
    kind: "PERCENT_OFF",
    title: "Old Dhaka lunch 35% off",
    title_bn: "পুরান ঢাকা দুপুরে ৩৫% ছাড়",
    percent_bps: 3500,
    windows: [
      {
        days: ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"],
        start_local: "12:00",
        end_local: "15:30",
      },
    ],
    min_spend_minor: 60000,
    max_discount_minor: 35000,
    allowed_methods: ["BANGLA_QR", "BKASH"],
    valid_until: VALID_UNTIL,
  },
];

interface DemoMerchantSeed {
  merchant_id: string;
  display_name: string;
  display_name_bn: string;
  area: string;
  area_bn: string;
  cuisine: string;
  cuisine_bn: string;
}

const DEMO_MERCHANT_SEEDS: DemoMerchantSeed[] = [
  {
    merchant_id: "demo_mrch_kacchi_bhai",
    display_name: "Kacchi Bhai",
    display_name_bn: "কাচ্চি ভাই",
    area: "Dhanmondi",
    area_bn: "ধানমন্ডি",
    cuisine: "Kacchi & Biryani",
    cuisine_bn: "কাচ্চি ও বিরিয়ানি",
  },
  {
    merchant_id: "demo_mrch_sultans_dine",
    display_name: "Sultan's Dine",
    display_name_bn: "সুলতান'স ডাইন",
    area: "Gulshan",
    area_bn: "গুলশান",
    cuisine: "Kacchi & Polao",
    cuisine_bn: "কাচ্চি ও পোলাও",
  },
  {
    merchant_id: "demo_mrch_star_kabab",
    display_name: "Star Kabab",
    display_name_bn: "স্টার কাবাব",
    area: "Banani",
    area_bn: "বনানী",
    cuisine: "Kabab & Curry",
    cuisine_bn: "কাবাব ও কারি",
  },
  {
    merchant_id: "demo_mrch_pizza_roma",
    display_name: "Pizza Roma",
    display_name_bn: "পিৎজা রোমা",
    area: "Uttara",
    area_bn: "উত্তরা",
    cuisine: "Pizza & Pasta",
    cuisine_bn: "পিৎজা ও পাস্তা",
  },
  {
    merchant_id: "demo_mrch_chillox",
    display_name: "Chillox",
    display_name_bn: "চিলক্স",
    area: "Mirpur",
    area_bn: "মিরপুর",
    cuisine: "Burgers",
    cuisine_bn: "বার্গার",
  },
  {
    merchant_id: "demo_mrch_haji_biriyani",
    display_name: "Haji Biriyani",
    display_name_bn: "হাজী বিরিয়ানি",
    area: "Old Dhaka",
    area_bn: "পুরান ঢাকা",
    cuisine: "Biriyani",
    cuisine_bn: "বিরিয়ানি",
  },
];

function buildMerchant(seed: DemoMerchantSeed): DinerMerchant {
  const live = DEMO_OFFERS.filter((o) => o.merchant_id === seed.merchant_id);
  const best = live.reduce<number | null>(
    (acc, o) =>
      o.percent_bps !== null && (acc === null || o.percent_bps > acc) ? o.percent_bps : acc,
    null,
  );
  return {
    ...seed,
    live_offer_count: live.length,
    best_percent_bps: best,
    window_tags: tagsForWindows(live.flatMap((o) => o.windows)),
  };
}

export const DEMO_MERCHANTS: DinerMerchant[] = DEMO_MERCHANT_SEEDS.map(buildMerchant);

export function isDemoMerchantId(merchantId: string): boolean {
  return merchantId.startsWith("demo_mrch_");
}

export function listDemoMerchants(params: {
  area?: string;
  q?: string;
  windowTag?: WindowTag | "";
}): DinerMerchant[] {
  const area = params.area ?? "";
  const qRaw = params.q ?? "";
  const q = qRaw.trim().toLowerCase();
  const tag = params.windowTag ?? "";
  return DEMO_MERCHANTS.filter((m) => {
    if (area && m.area !== area) return false;
    if (tag && !m.window_tags.includes(tag)) return false;
    if (q) {
      if (
        !m.display_name.toLowerCase().includes(q) &&
        !m.display_name_bn.includes(qRaw) &&
        !m.area.toLowerCase().includes(q) &&
        !m.area_bn.includes(qRaw) &&
        !m.cuisine.toLowerCase().includes(q) &&
        !m.cuisine_bn.includes(qRaw)
      ) {
        return false;
      }
    }
    return true;
  });
}

export function getDemoMerchant(merchantId: string): DinerMerchant | null {
  return DEMO_MERCHANTS.find((m) => m.merchant_id === merchantId) ?? null;
}

export function listDemoOffers(merchantId: string): EligibleOffer[] {
  return DEMO_OFFERS.filter((o) => o.merchant_id === merchantId).map((o) => {
    const { merchant_id: _mid, ...offer } = o;
    return offer;
  });
}

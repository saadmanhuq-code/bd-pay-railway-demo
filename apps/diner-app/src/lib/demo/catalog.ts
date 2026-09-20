// Local demo dining catalog — used when the live gateway has no public
// merchant directory. Clearly labeled in the UI as a demo/simulator so the
// CEO can still walk the EatClub-style discovery flow.

import type {
  CuisineTag,
  DinerMerchant,
  EligibleOffer,
  MenuSection,
  MerchantReview,
  WindowTag,
} from "@/lib/api/types";
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
  {
    merchant_id: "demo_mrch_teakwood",
    offer_id: "demo_off_teak_afternoon",
    kind: "PERCENT_OFF",
    title: "Afternoon cafe 15% off",
    title_bn: "বিকেলের ক্যাফেতে ১৫% ছাড়",
    percent_bps: 1500,
    windows: [
      {
        days: ["MON", "TUE", "WED", "THU", "FRI"],
        start_local: "14:00",
        end_local: "17:30",
      },
    ],
    min_spend_minor: 25000,
    max_discount_minor: 15000,
    allowed_methods: ["BANGLA_QR", "BKASH", "NAGAD"],
    valid_until: VALID_UNTIL,
  },
];

interface DemoMerchantSeed {
  merchant_id: string;
  display_name: string;
  display_name_bn: string;
  city: string;
  city_bn: string;
  area: string;
  area_bn: string;
  cuisine: string;
  cuisine_bn: string;
  cuisine_tags: CuisineTag[];
  lat: number;
  lng: number;
  photo_urls: string[];
  rating_avg: number;
  rating_count: number;
  menu_sections: MenuSection[];
  reviews: MerchantReview[];
}

function picsum(seed: string, w = 640, h = 360): string {
  // Deterministic placeholder photos — no API keys.
  return `https://picsum.photos/seed/${encodeURIComponent(seed)}/${w}/${h}`;
}

const DEMO_MERCHANT_SEEDS: DemoMerchantSeed[] = [
  {
    merchant_id: "demo_mrch_kacchi_bhai",
    display_name: "Kacchi Bhai",
    display_name_bn: "কাচ্চি ভাই",
    city: "Dhaka",
    city_bn: "ঢাকা",
    area: "Dhanmondi",
    area_bn: "ধানমন্ডি",
    cuisine: "Kacchi & Biryani",
    cuisine_bn: "কাচ্চি ও বিরিয়ানি",
    cuisine_tags: ["Biryani", "Bangladeshi"],
    lat: 23.7465,
    lng: 90.3742,
    photo_urls: [picsum("kacchi-bhai-1"), picsum("kacchi-bhai-2"), picsum("kacchi-bhai-3")],
    rating_avg: 4.5,
    rating_count: 128,
    menu_sections: [
      {
        title: "Biryani",
        title_bn: "বিরিয়ানি",
        items: [
          { name: "Kacchi biryani (full)", name_bn: "কাচ্চি বিরিয়ানি (ফুল)", price_minor: 45000 },
          { name: "Kacchi biryani (half)", name_bn: "কাচ্চি বিরিয়ানি (হাফ)", price_minor: 28000 },
          { name: "Chicken roast", name_bn: "চিকেন রোস্ট", price_minor: 22000 },
        ],
      },
      {
        title: "Sides",
        title_bn: "সাইড",
        items: [
          { name: "Borhani", name_bn: "বরহানি", price_minor: 6000 },
          { name: "Jali kabab", name_bn: "জালি কাবাব", price_minor: 12000 },
        ],
      },
    ],
    reviews: [
      {
        author: "Nusrat",
        author_bn: "নুসরাত",
        stars: 5,
        text: "Best off-peak kacchi deal — discount applied at pay, no voucher fuss.",
        text_bn: "সেরা কম-ভিড়ের কাচ্চি ডিল — পেমেন্টেই ছাড়, কোনো ভাউচার ঝামেলা নেই।",
      },
      {
        author: "Rahim",
        author_bn: "রহিম",
        stars: 4,
        text: "Busy at peak; afternoon window is worth it.",
        text_bn: "পিচে ব্যস্ত; বিকেলের উইন্ডো সত্যিই কাজে লাগে।",
      },
      {
        author: "Farhana",
        author_bn: "ফারহানা",
        stars: 5,
        text: "Demo review — sample copy for the dining wedge walkthrough.",
        text_bn: "ডেমো রিভিউ — ডাইনিং ওয়েজ ওয়াকথ্রু-এর নমুনা কপি।",
      },
    ],
  },
  {
    merchant_id: "demo_mrch_sultans_dine",
    display_name: "Sultan's Dine",
    display_name_bn: "সুলতান'স ডাইন",
    city: "Dhaka",
    city_bn: "ঢাকা",
    area: "Gulshan",
    area_bn: "গুলশান",
    cuisine: "Kacchi & Polao",
    cuisine_bn: "কাচ্চি ও পোলাও",
    cuisine_tags: ["Biryani", "Bangladeshi"],
    lat: 23.7925,
    lng: 90.4078,
    photo_urls: [picsum("sultans-dine-1"), picsum("sultans-dine-2")],
    rating_avg: 4.7,
    rating_count: 256,
    menu_sections: [
      {
        title: "Signature",
        title_bn: "সিগনেচার",
        items: [
          { name: "Sultan kacchi", name_bn: "সুলতান কাচ্চি", price_minor: 55000 },
          { name: "Morog polao", name_bn: "মোরগ পোলাও", price_minor: 38000 },
          { name: "Beef rezala", name_bn: "বিফ রেজালা", price_minor: 32000 },
        ],
      },
    ],
    reviews: [
      {
        author: "Imran",
        author_bn: "ইমরান",
        stars: 5,
        text: "Weekend 30% is a steal for Gulshan.",
        text_bn: "গুলশানের জন্য উইকেন্ড ৩০% সত্যিই দারুণ।",
      },
      {
        author: "Sadia",
        author_bn: "সাদিয়া",
        stars: 4,
        text: "Demo data — not a live rating feed.",
        text_bn: "ডেমো ডেটা — লাইভ রেটিং ফিড নয়।",
      },
    ],
  },
  {
    merchant_id: "demo_mrch_star_kabab",
    display_name: "Star Kabab",
    display_name_bn: "স্টার কাবাব",
    city: "Dhaka",
    city_bn: "ঢাকা",
    area: "Banani",
    area_bn: "বনানী",
    cuisine: "Kabab & Curry",
    cuisine_bn: "কাবাব ও কারি",
    cuisine_tags: ["Kabab", "Bangladeshi"],
    lat: 23.7937,
    lng: 90.4066,
    photo_urls: [picsum("star-kabab-1"), picsum("star-kabab-2")],
    rating_avg: 4.2,
    rating_count: 89,
    menu_sections: [
      {
        title: "Kabab",
        title_bn: "কাবাব",
        items: [
          { name: "Beef sheekh", name_bn: "বিফ শিক", price_minor: 18000 },
          { name: "Chicken tikka", name_bn: "চিকেন টিক্কা", price_minor: 16000 },
          { name: "Mixed grill platter", name_bn: "মিক্সড গ্রিল প্লেটার", price_minor: 45000 },
        ],
      },
    ],
    reviews: [
      {
        author: "Karim",
        author_bn: "করিম",
        stars: 4,
        text: "Solid lunch window discount.",
        text_bn: "লাঞ্চ উইন্ডোর ছাড় ভালো।",
      },
      {
        author: "Laila",
        author_bn: "লাইলা",
        stars: 5,
        text: "Sample review for demo catalog.",
        text_bn: "ডেমো ক্যাটালগের নমুনা রিভিউ।",
      },
    ],
  },
  {
    merchant_id: "demo_mrch_pizza_roma",
    display_name: "Pizza Roma",
    display_name_bn: "পিৎজা রোমা",
    city: "Dhaka",
    city_bn: "ঢাকা",
    area: "Uttara",
    area_bn: "উত্তরা",
    cuisine: "Pizza & Pasta",
    cuisine_bn: "পিৎজা ও পাস্তা",
    cuisine_tags: ["Pizza"],
    lat: 23.8759,
    lng: 90.3795,
    photo_urls: [picsum("pizza-roma-1"), picsum("pizza-roma-2")],
    rating_avg: 4.0,
    rating_count: 64,
    menu_sections: [
      {
        title: "Pizza",
        title_bn: "পিৎজা",
        items: [
          { name: "Margherita", name_bn: "মার্গেরিটা", price_minor: 45000 },
          { name: "Pepperoni", name_bn: "পেপারোনি", price_minor: 52000 },
          { name: "BBQ chicken", name_bn: "বিবিকিউ চিকেন", price_minor: 58000 },
        ],
      },
    ],
    reviews: [
      {
        author: "Tanvir",
        author_bn: "তানভির",
        stars: 4,
        text: "Saturday 50% fills the room — demo note.",
        text_bn: "শনিবার ৫০% রুম ভরে দেয় — ডেমো নোট।",
      },
      {
        author: "Maya",
        author_bn: "মায়া",
        stars: 3,
        text: "Decent crust; sample review.",
        text_bn: "ক্রাস্ট ঠিক আছে; নমুনা রিভিউ।",
      },
    ],
  },
  {
    merchant_id: "demo_mrch_chillox",
    display_name: "Chillox",
    display_name_bn: "চিলক্স",
    city: "Dhaka",
    city_bn: "ঢাকা",
    area: "Mirpur",
    area_bn: "মিরপুর",
    cuisine: "Burgers",
    cuisine_bn: "বার্গার",
    cuisine_tags: ["Burgers"],
    lat: 23.8223,
    lng: 90.3654,
    photo_urls: [picsum("chillox-1"), picsum("chillox-2")],
    rating_avg: 4.3,
    rating_count: 201,
    menu_sections: [
      {
        title: "Burgers",
        title_bn: "বার্গার",
        items: [
          { name: "Classic beef", name_bn: "ক্লাসিক বিফ", price_minor: 28000 },
          { name: "Chicken smash", name_bn: "চিকেন স্ম্যাশ", price_minor: 26000 },
          { name: "Loaded fries", name_bn: "লোডেড ফ্রাইজ", price_minor: 15000 },
        ],
      },
    ],
    reviews: [
      {
        author: "Ashik",
        author_bn: "আশিক",
        stars: 5,
        text: "Late-night window is perfect after campus.",
        text_bn: "ক্যাম্পাসের পর লেট-নাইট উইন্ডো পারফেক্ট।",
      },
      {
        author: "Rima",
        author_bn: "রিমা",
        stars: 4,
        text: "Demo review blurb.",
        text_bn: "ডেমো রিভিউ ব্লার্ব।",
      },
    ],
  },
  {
    merchant_id: "demo_mrch_haji_biriyani",
    display_name: "Haji Biriyani",
    display_name_bn: "হাজী বিরিয়ানি",
    city: "Dhaka",
    city_bn: "ঢাকা",
    area: "Old Dhaka",
    area_bn: "পুরান ঢাকা",
    cuisine: "Biriyani",
    cuisine_bn: "বিরিয়ানি",
    cuisine_tags: ["Biryani", "Bangladeshi"],
    lat: 23.7104,
    lng: 90.4074,
    photo_urls: [picsum("haji-biriyani-1"), picsum("haji-biriyani-2")],
    rating_avg: 4.6,
    rating_count: 412,
    menu_sections: [
      {
        title: "Classic",
        title_bn: "ক্লাসিক",
        items: [
          { name: "Haji biryani plate", name_bn: "হাজী বিরিয়ানি প্লেট", price_minor: 35000 },
          { name: "Extra meat", name_bn: "অতিরিক্ত মাংস", price_minor: 12000 },
        ],
      },
    ],
    reviews: [
      {
        author: "Shakil",
        author_bn: "শাকিল",
        stars: 5,
        text: "Old Dhaka classic — demo photos only.",
        text_bn: "পুরান ঢাকার ক্লাসিক — শুধু ডেমো ছবি।",
      },
      {
        author: "Nabila",
        author_bn: "নাবিলা",
        stars: 5,
        text: "35% lunch deal is the one to catch.",
        text_bn: "৩৫% লাঞ্চ ডিলটাই ধরতে হবে।",
      },
      {
        author: "Omar",
        author_bn: "ওমর",
        stars: 4,
        text: "Sample third review for the card.",
        text_bn: "কার্ডের জন্য তৃতীয় নমুনা রিভিউ।",
      },
    ],
  },
  {
    merchant_id: "demo_mrch_teakwood",
    display_name: "Teakwood Cafe",
    display_name_bn: "টিকউড ক্যাফে",
    city: "Dhaka",
    city_bn: "ঢাকা",
    area: "Bashundhara",
    area_bn: "বসুন্ধরা",
    cuisine: "Cafe & Thai",
    cuisine_bn: "ক্যাফে ও থাই",
    cuisine_tags: ["Cafe", "Thai", "Chinese"],
    lat: 23.8151,
    lng: 90.4255,
    photo_urls: [picsum("teakwood-1"), picsum("teakwood-2")],
    rating_avg: 4.1,
    rating_count: 73,
    menu_sections: [
      {
        title: "Cafe",
        title_bn: "ক্যাফে",
        items: [
          { name: "Flat white", name_bn: "ফ্ল্যাট হোয়াইট", price_minor: 28000 },
          { name: "Club sandwich", name_bn: "ক্লাব স্যান্ডউইচ", price_minor: 32000 },
        ],
      },
      {
        title: "Thai / Chinese",
        title_bn: "থাই / চাইনিজ",
        items: [
          { name: "Pad Thai", name_bn: "প্যাড থাই", price_minor: 42000 },
          { name: "Chicken chili dry", name_bn: "চিকেন চিলি ড্রাই", price_minor: 38000 },
        ],
      },
    ],
    reviews: [
      {
        author: "Priya",
        author_bn: "প্রিয়া",
        stars: 4,
        text: "Nice cafe vibes; demo menu only.",
        text_bn: "ক্যাফে ভাইব ভালো; শুধু ডেমো মেনু।",
      },
      {
        author: "Hasan",
        author_bn: "হাসান",
        stars: 4,
        text: "Afternoon discount is quiet and good.",
        text_bn: "বিকেলের ছাড় শান্ত ও ভালো।",
      },
    ],
  },
];

/** Cities shown in the picker. Chittagong / Sylhet are stubs (no venues yet). */
export const DEMO_CITIES: Array<{ city: string; city_bn: string }> = [
  { city: "Dhaka", city_bn: "ঢাকা" },
  { city: "Chittagong", city_bn: "চট্টগ্রাম" },
  { city: "Sylhet", city_bn: "সিলেট" },
];

export const CUISINE_FILTERS: CuisineTag[] = [
  "Bangladeshi",
  "Biryani",
  "Cafe",
  "Chinese",
  "Thai",
  "Burgers",
  "Pizza",
  "Kabab",
];

function buildMerchant(seed: DemoMerchantSeed): DinerMerchant {
  const live = DEMO_OFFERS.filter((o) => o.merchant_id === seed.merchant_id);
  const best = live.reduce<number | null>(
    (acc, o) =>
      o.percent_bps !== null && (acc === null || o.percent_bps > acc) ? o.percent_bps : acc,
    null,
  );
  return {
    merchant_id: seed.merchant_id,
    display_name: seed.display_name,
    display_name_bn: seed.display_name_bn,
    area: seed.area,
    area_bn: seed.area_bn,
    cuisine: seed.cuisine,
    cuisine_bn: seed.cuisine_bn,
    live_offer_count: live.length,
    best_percent_bps: best,
    window_tags: tagsForWindows(live.flatMap((o) => o.windows)),
    city: seed.city,
    city_bn: seed.city_bn,
    lat: seed.lat,
    lng: seed.lng,
    cuisine_tags: seed.cuisine_tags,
    photo_urls: seed.photo_urls,
    rating_avg: seed.rating_avg,
    rating_count: seed.rating_count,
    menu_sections: seed.menu_sections,
    reviews: seed.reviews,
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
  city?: string;
  cuisine?: CuisineTag | "";
}): DinerMerchant[] {
  const area = params.area ?? "";
  const qRaw = params.q ?? "";
  const q = qRaw.trim().toLowerCase();
  const tag = params.windowTag ?? "";
  const city = params.city ?? "";
  const cuisine = params.cuisine ?? "";
  return DEMO_MERCHANTS.filter((m) => {
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

export function getDemoMerchant(merchantId: string): DinerMerchant | null {
  return DEMO_MERCHANTS.find((m) => m.merchant_id === merchantId) ?? null;
}

export function listDemoOffers(merchantId: string): EligibleOffer[] {
  return DEMO_OFFERS.filter((o) => o.merchant_id === merchantId).map((o) => {
    const { merchant_id: _mid, ...offer } = o;
    return offer;
  });
}

export function areasForCity(city: string): Array<[string, string]> {
  const seen = new Map<string, string>();
  for (const m of DEMO_MERCHANTS) {
    if ((m.city ?? "Dhaka") !== city) continue;
    if (!seen.has(m.area)) seen.set(m.area, m.area_bn);
  }
  return [...seen.entries()];
}

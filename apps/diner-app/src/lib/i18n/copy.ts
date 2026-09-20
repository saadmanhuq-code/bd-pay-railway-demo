// Bilingual copy objects — Bengali-FIRST (spec/18 §language requirements;
// protein-chain-bd pattern, same shape as the ops-console / developer-portal
// corpora). The diner-app renders bn as the primary language; en is the
// secondary. Gate: scripts/check-bn-corpus.mjs (G5 mojibake harness) runs at
// prebuild — key symmetry, Bengali-block presence, no placeholders, and the
// 11 spec/18 ReasonCode strings byte-identical to the kernel table
// (bdpay/kernel/offers/copy_bn.py).

export type Lang = "bn" | "en";

export interface Bi {
  en: string;
  bn: string;
}

// ---------------------------------------------------------------------------
// spec/18 ReasonCode copy — byte-identical to the kernel REASON_COPY table
// (bdpay/kernel/offers/copy_bn.py, pinned by tests/platform/test_bn_encoding.py).
// Consumer-displayable, PII-free, static strings only.
// ---------------------------------------------------------------------------

export const REASON_COPY = {
  OFFER_NOT_ACTIVE: {
    en: "This offer is not currently active",
    bn: "অফারটি এখন সক্রিয় নেই",
  },
  OUTSIDE_WINDOW: {
    en: "This offer does not apply at this time",
    bn: "এই সময়ে অফারটি প্রযোজ্য নয়",
  },
  BEFORE_VALIDITY: {
    en: "This offer has not started yet",
    bn: "অফারের মেয়াদ এখনও শুরু হয়নি",
  },
  AFTER_VALIDITY: {
    en: "This offer has expired",
    bn: "অফারের মেয়াদ শেষ হয়ে গেছে",
  },
  MIN_SPEND_NOT_MET: {
    en: "The minimum spend for this offer was not met",
    bn: "ন্যূনতম কেনাকাটার পরিমাণ পূরণ হয়নি",
  },
  METHOD_NOT_ALLOWED: {
    en: "This offer does not apply to this payment method",
    bn: "এই পেমেন্ট পদ্ধতিতে অফারটি প্রযোজ্য নয়",
  },
  CAP_DAY_EXHAUSTED: {
    en: "Today's limit for this offer has been reached",
    bn: "আজকের জন্য অফারের সীমা শেষ হয়ে গেছে",
  },
  CAP_TOTAL_EXHAUSTED: {
    en: "The total limit for this offer has been reached",
    bn: "অফারের মোট সীমা শেষ হয়ে গেছে",
  },
  CAP_CUSTOMER_EXHAUSTED: {
    en: "You have already used this offer",
    bn: "আপনি এই অফারটি ইতিমধ্যে ব্যবহার করেছেন",
  },
  MERCHANT_NOT_ACTIVE: {
    en: "The merchant is not currently active",
    bn: "মার্চেন্ট এই মুহূর্তে সক্রিয় নেই",
  },
  BOGO_LINE_ITEMS_MISSING: {
    en: "BOGO offers require item details",
    bn: "BOGO অফারের জন্য পণ্যের বিবরণ প্রয়োজন",
  },
} as const;

export type ReasonCopyKey = keyof typeof REASON_COPY;

// ---------------------------------------------------------------------------
// Refusal codes surfaced by the reservation path (error envelope `code`
// values, spec/18 §API surface + errata S18-E14). Bilingual, PII-free.
// ---------------------------------------------------------------------------

export const REFUSAL_COPY = {
  offer_requires_login: {
    en: "Sign in to redeem offers. Guests cannot redeem (decision D1).",
    bn: "অফার ব্যবহার করতে সাইন ইন করুন। অতিথি হিসেবে অফার ব্যবহার করা যায় না।",
  },
  offer_not_eligible: {
    en: "This offer cannot be applied to this payment.",
    bn: "এই পেমেন্টে অফারটি প্রযোজ্য নয়।",
  },
  offer_cap_exhausted_customer: {
    en: "You have already used this offer the maximum number of times.",
    bn: "আপনি এই অফারটি সর্বোচ্চবার ব্যবহার করে ফেলেছেন।",
  },
  offer_cap_exhausted_day: {
    en: "Today's quota for this offer is finished. Try again tomorrow.",
    bn: "আজকের জন্য এই অফারের কোটা শেষ। আগামীকাল আবার চেষ্টা করুন।",
  },
  offer_cap_exhausted_total: {
    en: "This offer is fully redeemed.",
    bn: "এই অফারটি সম্পূর্ণ ব্যবহৃত হয়ে গেছে।",
  },
  bogo_not_yet_enabled: {
    en: "Buy-one-get-one offers are not available yet.",
    bn: "একটি কিনলে একটি ফ্রি অফার এখনও চালু হয়নি।",
  },
  method_not_allowed_static_qr: {
    en: "Offer payments need a dynamic QR. Ask the restaurant for a fresh QR.",
    bn: "অফার পেমেন্টের জন্য ডাইনামিক কিউআর প্রয়োজন। রেস্তোরাঁ থেকে নতুন কিউআর চেয়ে নিন।",
  },
  gross_amount_required: {
    en: "Enter the bill amount before reserving the offer.",
    bn: "অফার সংরক্ষণের আগে বিলের পরিমাণ লিখুন।",
  },
  discount_equals_gross: {
    en: "The discount cannot cover the whole bill — at least 1 paisa must be paid.",
    bn: "ছাড় পুরো বিলের সমান হতে পারে না — অন্তত ১ পয়সা পরিশোধ করতে হবে।",
  },
} as const;

export type RefusalCopyKey = keyof typeof REFUSAL_COPY;

// ---------------------------------------------------------------------------
// UI copy
// ---------------------------------------------------------------------------

export const COPY = {
  appName: { en: "BD-PAY Diner", bn: "বিডি-পে ডাইনার" },
  tagline: {
    en: "Restaurant offers, applied at payment — no vouchers, no codes.",
    bn: "রেস্তোরাঁর অফার, পেমেন্টের সময়েই প্রযোজ্য — কোনো ভাউচার বা কোড লাগে না।",
  },
  nav_browse: { en: "Offers", bn: "অফার" },
  nav_history: { en: "My redemptions", bn: "আমার ব্যবহার" },
  language: { en: "Language", bn: "ভাষা" },
  sign_out: { en: "Sign out", bn: "সাইন আউট" },
  loading: { en: "Loading…", bn: "লোড হচ্ছে…" },
  error_generic: {
    en: "Something went wrong. Please try again.",
    bn: "কিছু একটা সমস্যা হয়েছে। আবার চেষ্টা করুন।",
  },

  // -- login (D1) -----------------------------------------------------------
  login_title: { en: "Sign in to dine", bn: "খেতে যাওয়ার আগে সাইন ইন করুন" },
  login_d1_notice: {
    en: "Browse deals freely. Sign in only when you are ready to redeem — that keeps per-person limits fair.",
    bn: "অফারগুলো মুক্তভাবে দেখুন। রিডিম করতে চাইলেই সাইন ইন করুন — এতে জনপ্রতি সীমা ন্যায্য থাকে।",
  },
  login_phone: { en: "Mobile number", bn: "মোবাইল নম্বর" },
  login_phone_hint: {
    en: "Bangladeshi mobile (01XXXXXXXXX). Bengali digits work too.",
    bn: "বাংলাদেশি মোবাইল (০১XXXXXXXXX)। বাংলা সংখ্যাও লেখা যাবে।",
  },
  login_phone_invalid: {
    en: "Enter a valid Bangladeshi mobile number (e.g. 01712345678).",
    bn: "সঠিক বাংলাদেশি মোবাইল নম্বর দিন (যেমন ০১৭১২৩৪৫৬৭৮)।",
  },
  login_send_otp: { en: "Send code", bn: "কোড পাঠান" },
  login_otp: { en: "One-time code", bn: "ওয়ান-টাইম কোড" },
  login_otp_sent: {
    en: "A 6-digit code was sent by SMS.",
    bn: "এসএমএসে ৬-সংখ্যার কোড পাঠানো হয়েছে।",
  },
  login_demo_otp: { en: "Demo code", bn: "ডেমো কোড" },
  login_otp_invalid: {
    en: "That code did not match. Check the SMS and try again.",
    bn: "কোডটি মেলেনি। এসএমএস দেখে আবার চেষ্টা করুন।",
  },
  login_verify: { en: "Verify & sign in", bn: "যাচাই করে সাইন ইন করুন" },
  login_change_phone: { en: "Change number", bn: "নম্বর পরিবর্তন করুন" },

  // -- browse ----------------------------------------------------------------
  browse_title: { en: "Restaurants near you", bn: "আপনার কাছের রেস্তোরাঁ" },
  browse_area_all: { en: "All areas", bn: "সব এলাকা" },
  browse_search: { en: "Search restaurants", bn: "রেস্তোরাঁ খুঁজুন" },
  browse_offers_live: { en: "offers live now", bn: "টি অফার এখন চালু" },
  browse_no_offers_now: { en: "no offers right now", bn: "এখন কোনো অফার নেই" },
  browse_up_to: { en: "up to", bn: "সর্বোচ্চ" },
  browse_discount_off: { en: "off", bn: "ছাড়" },
  browse_empty: {
    en: "No restaurants match. Try another area.",
    bn: "কোনো রেস্তোরাঁ মেলেনি। অন্য এলাকা দেখুন।",
  },
  browse_note: {
    en: "Offers are set by each restaurant for their off-peak hours. The discount is applied in the payment itself.",
    bn: "প্রতিটি রেস্তোরাঁ তাদের কম-ভিড়ের সময়ের জন্য অফার ঠিক করে। ছাড়টি পেমেন্টের মধ্যেই প্রয়োগ হয়।",
  },

  // -- merchant / offers -------------------------------------------------------
  offers_title: { en: "Offers at this restaurant", bn: "এই রেস্তোরাঁর অফার" },
  offers_none: {
    en: "No offers are live at this restaurant right now. Offer hours are set by the restaurant.",
    bn: "এই রেস্তোরাঁয় এখন কোনো অফার চালু নেই। অফারের সময় রেস্তোরাঁ নির্ধারণ করে।",
  },
  offer_min_spend: { en: "Min spend", bn: "ন্যূনতম বিল" },
  offer_max_discount: { en: "Max discount", bn: "সর্বোচ্চ ছাড়" },
  offer_valid_until: { en: "Valid until", bn: "মেয়াদ" },
  offer_windows: { en: "Offer hours", bn: "অফারের সময়" },
  offer_methods: { en: "Pay with", bn: "পেমেন্ট মাধ্যম" },
  offer_select: { en: "Use this offer", bn: "এই অফারটি ব্যবহার করুন" },
  offer_example_label: {
    en: "Estimated bill (for discount preview)",
    bn: "আনুমানিক বিল (ছাড়ের হিসাব দেখতে)",
  },

  // -- redemption flow ---------------------------------------------------------
  redeem_title: { en: "Reserve & pay", bn: "সংরক্ষণ করে পেমেন্ট করুন" },
  redeem_gross_label: { en: "Bill amount (BDT)", bn: "বিলের পরিমাণ (টাকা)" },
  redeem_gross_hint: {
    en: "The full bill before discount. Bengali digits and paisa (e.g. ১২৫০.৫০) are fine.",
    bn: "ছাড়ের আগের পুরো বিল। বাংলা সংখ্যা ও পয়সা (যেমন ১২৫০.৫০) লেখা যাবে।",
  },
  redeem_amount_invalid: {
    en: "Enter a valid amount, e.g. 1250 or ১২৫০.৫০.",
    bn: "সঠিক পরিমাণ লিখুন, যেমন 1250 বা ১২৫০.৫০।",
  },
  redeem_gross: { en: "Bill (gross)", bn: "মোট বিল" },
  redeem_discount: { en: "Discount", bn: "ছাড়" },
  redeem_net: { en: "You pay (net)", bn: "আপনি দেবেন (নেট)" },
  redeem_reserve: { en: "Reserve offer & get QR", bn: "অফার সংরক্ষণ করে কিউআর নিন" },
  redeem_reserving: { en: "Reserving…", bn: "সংরক্ষণ হচ্ছে…" },
  redeem_hold_notice: {
    en: "Your discount is locked the moment the reservation succeeds — the price never changes mid-payment. If you do not pay within 30 minutes, the reservation is released.",
    bn: "সংরক্ষণ সফল হওয়ামাত্র আপনার ছাড় নিশ্চিত হয়ে যায় — পেমেন্টের মাঝপথে দাম কখনো বদলায় না। ৩০ মিনিটের মধ্যে পেমেন্ট না করলে সংরক্ষণটি ছেড়ে দেওয়া হয়।",
  },
  redeem_reasons_title: {
    en: "Why this offer cannot apply now",
    bn: "অফারটি এখন কেন প্রযোজ্য নয়",
  },

  // -- QR handoff ---------------------------------------------------------------
  qr_title: { en: "Pay with this QR", bn: "এই কিউআর দিয়ে পেমেন্ট করুন" },
  qr_note: {
    en: "Show this dynamic Bangla QR at the counter or scan it from your wallet app. It already carries the discounted amount.",
    bn: "কাউন্টারে এই ডাইনামিক বাংলা কিউআর দেখান বা আপনার ওয়ালেট অ্যাপ থেকে স্ক্যান করুন। এতে ছাড়সহ চূড়ান্ত পরিমাণ ধরা আছে।",
  },
  qr_expires: { en: "QR expires", bn: "কিউআর-এর মেয়াদ" },
  qr_payment_intent: { en: "Payment reference", bn: "পেমেন্ট রেফারেন্স" },
  qr_confirm_demo: {
    en: "I have paid — confirm (simulated rail)",
    bn: "পেমেন্ট করেছি — নিশ্চিত করুন (সিমুলেটেড রেল)",
  },
  qr_confirming: { en: "Confirming…", bn: "নিশ্চিত হচ্ছে…" },

  // -- receipt -------------------------------------------------------------------
  receipt_title: { en: "Payment receipt", bn: "পেমেন্ট রসিদ" },
  receipt_applied: {
    en: "Offer applied. The discount is shown on your receipt — always.",
    bn: "অফার প্রযোজ্য হয়েছে। ছাড়টি সবসময় আপনার রসিদে দেখানো হয়।",
  },
  receipt_merchant: { en: "Restaurant", bn: "রেস্তোরাঁ" },
  receipt_view_history: { en: "View my redemptions", bn: "আমার ব্যবহার দেখুন" },

  // -- sandbox demo one-tap pay --------------------------------------------------
  demo_pay_title: { en: "Sandbox demo pay", bn: "স্যান্ডবক্স ডেমো পেমেন্ট" },
  demo_pay_note: {
    en: "One-tap simulator payment. This appears in My redemptions as SUCCEEDED.",
    bn: "এক-ট্যাপ সিমুলেটর পেমেন্ট। এটি আমার ব্যবহার-এ SUCCEEDED হিসেবে দেখাবে।",
  },
  demo_pay_amount_label: { en: "Amount (BDT)", bn: "পরিমাণ (টাকা)" },
  demo_pay_busy: { en: "Paying…", bn: "পেমেন্ট হচ্ছে…" },
  demo_pay_button: { en: "Pay now (simulated)", bn: "এখন পেমেন্ট করুন (সিমুলেটেড)" },
  demo_pay_success: {
    en: "Payment succeeded. View it in My redemptions.",
    bn: "পেমেন্ট সফল হয়েছে। আমার ব্যবহার-এ দেখুন।",
  },

  // -- history ---------------------------------------------------------------------
  history_title: { en: "My redemptions", bn: "আমার অফার ব্যবহারের ইতিহাস" },
  history_empty: {
    en: "No redemptions yet. Reserve an offer and pay to see it here.",
    bn: "এখনও কোনো অফার ব্যবহার হয়নি। অফার সংরক্ষণ করে পেমেন্ট করলে এখানে দেখা যাবে।",
  },
  history_when: { en: "When", bn: "কখন" },
  history_state: { en: "Status", bn: "অবস্থা" },
  state_RESERVED: { en: "Reserved — awaiting payment", bn: "সংরক্ষিত — পেমেন্টের অপেক্ষায়" },
  state_APPLIED: { en: "Applied — discount realized", bn: "প্রযোজ্য — ছাড় কার্যকর হয়েছে" },
  state_RELEASED: {
    en: "Released — not paid in time, slot returned",
    bn: "ছেড়ে দেওয়া হয়েছে — সময়মতো পেমেন্ট হয়নি, স্লট ফেরত গেছে",
  },
  state_REVERSED: {
    en: "Reversed — payment refunded",
    bn: "প্রত্যাহার — পেমেন্ট ফেরত হয়েছে",
  },
  state_SETTLED: { en: "Settled with restaurant", bn: "রেস্তোরাঁর সাথে নিষ্পত্তি হয়েছে" },

  // -- days (Asia/Dhaka windows) ------------------------------------------------------
  day_MON: { en: "Mon", bn: "সোম" },
  day_TUE: { en: "Tue", bn: "মঙ্গল" },
  day_WED: { en: "Wed", bn: "বুধ" },
  day_THU: { en: "Thu", bn: "বৃহস্পতি" },
  day_FRI: { en: "Fri", bn: "শুক্র" },
  day_SAT: { en: "Sat", bn: "শনি" },
  day_SUN: { en: "Sun", bn: "রবি" },

  // -- methods -------------------------------------------------------------------------
  method_BANGLA_QR: { en: "Bangla QR", bn: "বাংলা কিউআর" },
  method_BKASH: { en: "bKash", bn: "বিকাশ" },
  method_NAGAD: { en: "Nagad", bn: "নগদ" },
  // -- public discovery (EatClub-style) ---------------------------------------
  hero_eyebrow: { en: "Dining deals on BD-PAY", bn: "বিডি-পে-তে ডাইনিং ডিল" },
  hero_title: {
    en: "Up to 50% off at restaurants near you",
    bn: "আপনার কাছের রেস্তোরাঁয় সর্বোচ্চ ৫০% ছাড়",
  },
  hero_subtitle: {
    en: "Merchant-set off-peak deals. Discount applied at payment — no vouchers, no codes.",
    bn: "রেস্তোরাঁর নির্ধারিত কম-ভিড়ের ডিল। ছাড় পেমেন্টের সময়ই প্রয়োগ — কোনো ভাউচার বা কোড নেই।",
  },
  window_lunch: { en: "Lunch", bn: "লাঞ্চ" },
  window_early_bird: { en: "Early bird", bn: "আর্লি বার্ড" },
  window_happy_hour: { en: "Happy hour", bn: "হ্যাপি আওয়ার" },
  window_late: { en: "Late", bn: "লেট" },
  window_all: { en: "Any time", bn: "যেকোনো সময়" },
  demo_catalog_banner: {
    en: "Demo catalog (simulator) — sample Dhaka restaurants for discovery. Not live bank data.",
    bn: "ডেমো ক্যাটালগ (সিমুলেটর) — ডিসকভারির জন্য নমুনা ঢাকা রেস্তোরাঁ। লাইভ ব্যাংক ডেটা নয়।",
  },
  sign_in: { en: "Sign in", bn: "সাইন ইন" },
  sign_in_to_redeem: {
    en: "Sign in to redeem",
    bn: "রিডিম করতে সাইন ইন করুন",
  },
  sign_in_to_redeem_hint: {
    en: "You can browse every deal without an account. Sign in only at payment.",
    bn: "অ্যাকাউন্ট ছাড়াই সব ডিল দেখা যায়। শুধু পেমেন্টের সময় সাইন ইন করুন।",
  },
  browse_results: { en: "restaurants", bn: "টি রেস্তোরাঁ" },

} as const;

export type CopyKey = keyof typeof COPY;

export function pick(bi: Bi, lang: Lang): string {
  return lang === "bn" ? bi.bn : bi.en;
}

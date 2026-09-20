// Bilingual copy objects (spec/15 §i18n). Safety-critical strings are
// rendered via DualLabel — BOTH languages at once, never toggled (API key
// revoke, webhook delete, bulk disbursement release).

export type Lang = "en" | "bn";

export interface Bi {
  en: string;
  bn: string;
}

export const COPY = {
  appName: { en: "BD-PAY Developer Portal", bn: "বিডি-পে ডেভেলপার পোর্টাল" },
  nav_dashboard: { en: "Dashboard", bn: "ড্যাশবোর্ড" },
  nav_onboarding: { en: "Onboarding", bn: "অনবোর্ডিং" },
  nav_api_keys: { en: "API keys", bn: "এপিআই কী" },
  nav_webhooks: { en: "Webhooks", bn: "ওয়েবহুক" },
  nav_docs: { en: "API docs", bn: "এপিআই ডকুমেন্টেশন" },
  nav_sandbox: { en: "Sandbox", bn: "স্যান্ডবক্স" },
  nav_qr: { en: "Bangla QR", bn: "বাংলা কিউআর" },
  nav_disputes: { en: "Disputes", bn: "বিরোধ" },
  nav_disbursements: { en: "Disbursements", bn: "বিতরণ" },
  nav_reports: { en: "Reports", bn: "রিপোর্ট" },
  nav_notifications: { en: "Notifications", bn: "নোটিফিকেশন" },
  nav_settings: { en: "Settings", bn: "সেটিংস" },
  nav_payment_links: { en: "Payment links", bn: "পেমেন্ট লিংক" },
  nav_status: { en: "Platform status", bn: "প্ল্যাটফর্ম স্ট্যাটাস" },
  login_title: { en: "Merchant sign-in", bn: "মার্চেন্ট সাইন-ইন" },
  login_email: { en: "Email", bn: "ইমেইল" },
  login_password: { en: "Password", bn: "পাসওয়ার্ড" },
  login_totp: { en: "TOTP code", bn: "টিওটিপি কোড" },
  login_persona: { en: "Sign in as", bn: "যে ভূমিকায় সাইন ইন করবেন" },
  login_submit: { en: "Sign in", bn: "সাইন ইন করুন" },
  login_lockout: {
    en: "3 consecutive TOTP failures lock the account for 30 minutes (BB ICT §6.2.6).",
    bn: "পরপর ৩ বার টিওটিপি ব্যর্থ হলে অ্যাকাউন্ট ৩০ মিনিটের জন্য লক হয়ে যাবে (বিবি আইসিটি §৬.২.৬)।",
  },
  logout: { en: "Sign out", bn: "সাইন আউট" },
  language: { en: "Language", bn: "ভাষা" },
  persona: { en: "Persona", bn: "ভূমিকা" },
  env_live: { en: "Live", bn: "লাইভ" },
  env_sandbox: { en: "Sandbox", bn: "স্যান্ডবক্স" },
  data_age: { en: "data age", bn: "তথ্যের বয়স" },
  refresh: { en: "Refresh", bn: "রিফ্রেশ" },
  loading: { en: "Loading…", bn: "লোড হচ্ছে…" },
  confirm: { en: "Confirm", bn: "নিশ্চিত করুন" },
  cancel: { en: "Cancel", bn: "বাতিল" },
  reason: { en: "Reason", bn: "কারণ" },
  copy: { en: "Copy", bn: "কপি করুন" },
  copied: { en: "Copied", bn: "কপি হয়েছে" },
  download: { en: "Download", bn: "ডাউনলোড" },
  totp_prompt: {
    en: "Enter your 6-digit TOTP code to confirm (step-up authentication).",
    bn: "নিশ্চিত করতে আপনার ৬-সংখ্যার টিওটিপি কোড লিখুন (ধাপ-আপ যাচাই)।",
  },
  error_generic: { en: "Request failed", bn: "অনুরোধ ব্যর্থ হয়েছে" },
  portal_footer: {
    en: "Merchant mutations require TOTP step-up and an Idempotency-Key. Server-side values are authoritative; this portal renders, it never recomputes.",
    bn: "মার্চেন্ট পরিবর্তনে টিওটিপি যাচাই ও আইডেমপোটেন্সি-কী প্রয়োজন। সার্ভারের মানই চূড়ান্ত; এই পোর্টাল কেবল প্রদর্শন করে, পুনরায় হিসাব করে না।",
  },

  // Signup / onboarding
  signup_title: { en: "Create a merchant account", bn: "মার্চেন্ট অ্যাকাউন্ট খুলুন" },
  signup_submit: { en: "Submit application", bn: "আবেদন জমা দিন" },
  onboarding_title: { en: "Onboarding status", bn: "অনবোর্ডিং অবস্থা" },
  kyb_checklist: { en: "KYB document checklist", bn: "কেওয়াইবি নথির তালিকা" },
  upload_document: { en: "Upload document", bn: "নথি আপলোড করুন" },
  bn_numeral_note: {
    en: "Bengali numerals (০-৯) are accepted and normalized to 0-9 client-side; the server re-normalizes and is authoritative.",
    bn: "বাংলা সংখ্যা (০-৯) গ্রহণযোগ্য এবং ক্লায়েন্ট-পাশে ০-৯→0-9 রূপান্তরিত হয়; সার্ভার পুনরায় রূপান্তর করে এবং সার্ভারের মানই চূড়ান্ত।",
  },
  activation_two_eyes: {
    en: "Activation is pending compliance review: a second compliance officer must approve before the account goes live (two-eyes).",
    bn: "সক্রিয়করণ কমপ্লায়েন্স পর্যালোচনার অপেক্ষায়: অ্যাকাউন্ট চালুর আগে দ্বিতীয় কমপ্লায়েন্স কর্মকর্তার অনুমোদন প্রয়োজন (দুই-চোখ)।",
  },

  // API keys
  create_key: { en: "Create API key", bn: "এপিআই কী তৈরি করুন" },
  rotate_key: { en: "Rotate", bn: "রোটেট করুন" },
  revoke_key: { en: "Revoke", bn: "প্রত্যাহার করুন" },
  revoke_key_confirm: {
    en: "REVOKE is immediate and irreversible. Every request signed with this key will fail from now on. There is no recovery — create a new key instead.",
    bn: "প্রত্যাহার তাৎক্ষণিক ও অপরিবর্তনীয়। এই কী দিয়ে স্বাক্ষরিত প্রতিটি অনুরোধ এখন থেকে ব্যর্থ হবে। পুনরুদ্ধারের উপায় নেই — পরিবর্তে নতুন কী তৈরি করুন।",
  },
  rotate_key_confirm: {
    en: "Rotation issues a new secret; the old secret keeps working for a 24-hour grace window, then expires.",
    bn: "রোটেশনে নতুন সিক্রেট ইস্যু হয়; পুরনো সিক্রেট ২৪ ঘণ্টার গ্রেস সময় পর্যন্ত কাজ করবে, তারপর মেয়াদোত্তীর্ণ হবে।",
  },
  secret_once: {
    en: "This secret is shown ONCE. Copy it now — it cannot be retrieved again.",
    bn: "এই সিক্রেট শুধু একবারই দেখানো হয়। এখনই কপি করুন — পরে আর পাওয়া যাবে না।",
  },

  // Webhooks
  create_endpoint: { en: "Add endpoint", bn: "এন্ডপয়েন্ট যোগ করুন" },
  delete_endpoint: { en: "Delete", bn: "মুছে ফেলুন" },
  delete_endpoint_confirm: {
    en: "Deleting this endpoint stops ALL event deliveries to it immediately. In-flight retries are dropped.",
    bn: "এই এন্ডপয়েন্ট মুছে দিলে এতে সব ইভেন্ট পাঠানো তাৎক্ষণিক বন্ধ হবে। চলমান পুনঃচেষ্টাগুলোও বাতিল হবে।",
  },
  test_fire: { en: "Send test event", bn: "টেস্ট ইভেন্ট পাঠান" },
  test_fire_confirm: {
    en: "A signed test event will be delivered to this endpoint. Verify the X-BDPay-Signature header against your signing secret.",
    bn: "এই এন্ডপয়েন্টে একটি স্বাক্ষরিত টেস্ট ইভেন্ট পাঠানো হবে। আপনার সাইনিং সিক্রেট দিয়ে X-BDPay-Signature হেডার যাচাই করুন।",
  },
  replay_delivery: { en: "Replay", bn: "পুনঃপ্রেরণ" },
  replay_confirm: {
    en: "Replay re-sends the same event with a new attempt number. Your handler must be idempotent on event id.",
    bn: "পুনঃপ্রেরণে একই ইভেন্ট নতুন চেষ্টার নম্বরসহ আবার পাঠানো হয়। ইভেন্ট আইডির উপর আপনার হ্যান্ডলার আইডেমপোটেন্ট হতে হবে।",
  },

  // QR
  register_qr: { en: "Issue static QR", bn: "স্ট্যাটিক কিউআর ইস্যু করুন" },
  revoke_qr: { en: "Revoke QR", bn: "কিউআর প্রত্যাহার করুন" },
  revoke_qr_confirm: {
    en: "Revoking a QR invalidates the printed code at the store. Customers scanning it will see a decline.",
    bn: "কিউআর প্রত্যাহার করলে দোকানে ছাপানো কোডটি অকার্যকর হবে। গ্রাহকরা স্ক্যান করলে প্রত্যাখ্যান দেখবেন।",
  },
  suspend_qr: { en: "Suspend QR", bn: "কিউআর স্থগিত করুন" },
  suspend_qr_confirm: {
    en: "Suspending pauses payments on this QR. Customers scanning it will see a decline until it is reactivated.",
    bn: "স্থগিত করলে এই কিউআর-এ পেমেন্ট বন্ধ থাকবে। পুনরায় সক্রিয় না করা পর্যন্ত গ্রাহকরা স্ক্যান করলে প্রত্যাখ্যান দেখবেন।",
  },
  reactivate_qr: { en: "Reactivate QR", bn: "কিউআর পুনরায় সক্রিয় করুন" },
  reactivate_qr_confirm: {
    en: "Reactivating resumes payments on this suspended QR immediately.",
    bn: "পুনরায় সক্রিয় করলে এই স্থগিত কিউআর-এ পেমেন্ট সাথে সাথে চালু হবে।",
  },
  qr_static_limit: {
    en: "Static QR transactions are capped at BDT 20,200 per scan (Bangla QR rules).",
    bn: "স্ট্যাটিক কিউআর লেনদেন প্রতি স্ক্যানে সর্বোচ্চ ২০,২০০ টাকা (বাংলা কিউআর নিয়ম)।",
  },
  qr_payload_once: {
    en: "The EMVCo payload string is shown once at issuance. Download print-ready assets any time from the render links.",
    bn: "ইএমভিকো পেলোড স্ট্রিংটি ইস্যুর সময় একবারই দেখানো হয়। প্রিন্ট-উপযোগী ফাইল যেকোনো সময় রেন্ডার লিংক থেকে নামান।",
  },

  // Disputes
  submit_evidence: { en: "Submit evidence", bn: "প্রমাণ জমা দিন" },
  submit_evidence_confirm: {
    en: "Evidence is hashed (SHA-256) and timestamped. Submit before the deadline — late evidence is not considered.",
    bn: "প্রমাণ SHA-256 হ্যাশ ও সময়সহ সংরক্ষিত হয়। সময়সীমার আগে জমা দিন — দেরিতে জমা প্রমাণ বিবেচিত হয় না।",
  },
  evidence_deadline: { en: "Evidence deadline", bn: "প্রমাণ জমার সময়সীমা" },

  // Disbursements
  upload_csv: { en: "Upload CSV", bn: "সিএসভি আপলোড করুন" },
  submit_batch: { en: "Submit batch", bn: "ব্যাচ জমা দিন" },
  submit_batch_confirm: {
    en: "Submitting creates a batch awaiting checker approval. The maker cannot approve their own batch.",
    bn: "জমা দিলে চেকারের অনুমোদনের অপেক্ষায় একটি ব্যাচ তৈরি হবে। মেকার নিজের ব্যাচ অনুমোদন করতে পারবেন না।",
  },
  approve_batch: { en: "Approve & release", bn: "অনুমোদন ও ছাড় করুন" },
  approve_batch_confirm: {
    en: "RELEASE moves real money to every row in this batch. You are the checker — verify totals before approving. Batches over BDT 10,00,000 also require platform finance co-sign.",
    bn: "ছাড় করলে এই ব্যাচের প্রতিটি সারিতে প্রকৃত অর্থ স্থানান্তর হবে। আপনি চেকার — অনুমোদনের আগে মোট অঙ্ক যাচাই করুন। ১০,০০,০০০ টাকার বেশি ব্যাচে প্ল্যাটফর্ম ফাইন্যান্সের সহ-স্বাক্ষরও লাগবে।",
  },
  reject_batch: { en: "Reject batch", bn: "ব্যাচ প্রত্যাখ্যান করুন" },
  reject_batch_confirm: {
    en: "Rejecting cancels the batch permanently. The maker must upload a corrected file as a new batch.",
    bn: "প্রত্যাখ্যান করলে ব্যাচ স্থায়ীভাবে বাতিল হবে। মেকারকে সংশোধিত ফাইল নতুন ব্যাচ হিসেবে আপলোড করতে হবে।",
  },
  checker_neq_maker: {
    en: "Maker-checker: the checker must be a different member than the maker.",
    bn: "মেকার-চেকার: চেকার অবশ্যই মেকার থেকে ভিন্ন সদস্য হতে হবে।",
  },
  finance_cosign_notice: {
    en: "Batches over BDT 10,00,000 (10 lakh) require an additional platform finance co-sign before release.",
    bn: "১০,০০,০০০ টাকার (১০ লাখ) বেশি ব্যাচ ছাড়ের আগে প্ল্যাটফর্ম ফাইন্যান্সের অতিরিক্ত সহ-স্বাক্ষর প্রয়োজন।",
  },

  // Public status page (spec/16 §E)
  status_title: { en: "Platform status", bn: "প্ল্যাটফর্ম স্ট্যাটাস" },
  status_connectors: { en: "Connector health", bn: "কানেক্টর স্বাস্থ্য" },
  status_mode_note: {
    en: "Modes are shown verbatim. No live payment rail is connected in this deployment.",
    bn: "মোডগুলো হুবহু দেখানো হয়। এই ডিপ্লয়মেন্টে কোনো লাইভ পেমেন্ট রেল সংযুক্ত নেই।",
  },
  mode_simulator: {
    en: "Simulated rail (pre-license sandbox)",
    bn: "সিমুলেটেড রেল (লাইসেন্স-পূর্ব স্যান্ডবক্স)",
  },
  mode_sandbox_rail: {
    en: "Rail-provided test environment",
    bn: "রেল-প্রদত্ত পরীক্ষা পরিবেশ",
  },
  status_certification: { en: "Adapter certification", bn: "অ্যাডাপ্টার সার্টিফিকেশন" },
  status_cert_note: {
    en: "Certification verdicts and report hashes are published read-only. Report files are not public.",
    bn: "সার্টিফিকেশনের ফলাফল ও রিপোর্ট হ্যাশ শুধু-পড়ার জন্য প্রকাশিত। রিপোর্ট ফাইল সর্বসাধারণের জন্য নয়।",
  },
  status_source_note: {
    en: "This page renders exclusively from the three public endpoints: /v1/public/status, /v1/public/certification-matrix and /v1/public/badges — no second data path.",
    bn: "এই পৃষ্ঠা কেবল তিনটি পাবলিক এন্ডপয়েন্ট থেকে প্রদর্শিত হয়: /v1/public/status, /v1/public/certification-matrix ও /v1/public/badges — অন্য কোনো ডেটা পথ নেই।",
  },

  // Payment links (spec/16 §C)
  create_link: { en: "Create payment link", bn: "পেমেন্ট লিংক তৈরি করুন" },
  create_link_confirm: {
    en: "A shareable hosted checkout URL will be created. The description must not contain personal data — requests containing PII are rejected, not redacted.",
    bn: "একটি শেয়ারযোগ্য হোস্টেড চেকআউট ইউআরএল তৈরি হবে। বিবরণে ব্যক্তিগত তথ্য থাকা যাবে না — পিআইআই থাকলে অনুরোধ প্রত্যাখ্যাত হয়, মুছে দেওয়া হয় না।",
  },
  link_amount_fixed: { en: "Fixed amount", bn: "নির্দিষ্ট পরিমাণ" },
  link_amount_bounded: { en: "Payer-entered amount (bounds)", bn: "পরিশোধকারী-প্রদত্ত পরিমাণ (সীমাসহ)" },
  link_single_use: { en: "Single use", bn: "একবার ব্যবহারযোগ্য" },
  link_expiry_hours: { en: "Expiry (hours)", bn: "মেয়াদ (ঘণ্টা)" },
  link_description: { en: "Description", bn: "বিবরণ" },
  link_public_url: { en: "Public URL", bn: "পাবলিক ইউআরএল" },
  cancel_link: { en: "Cancel link", bn: "লিংক বাতিল করুন" },
  cancel_link_confirm: {
    en: "Cancelling is permanent. The hosted checkout page will show a cancelled notice and no further payment can be made through this link.",
    bn: "বাতিল করা স্থায়ী। হোস্টেড চেকআউট পৃষ্ঠায় বাতিলের বার্তা দেখানো হবে এবং এই লিংক দিয়ে আর কোনো পেমেন্ট করা যাবে না।",
  },

  // Hosted checkout (public, spec/16 §C)
  checkout_title: { en: "Complete your payment", bn: "আপনার পেমেন্ট সম্পন্ন করুন" },
  checkout_amount: { en: "Amount", bn: "পরিমাণ" },
  checkout_enter_amount: {
    en: "Enter amount (BDT — Bengali numerals accepted)",
    bn: "পরিমাণ লিখুন (টাকা — বাংলা সংখ্যা গ্রহণযোগ্য)",
  },
  checkout_bounds: { en: "Allowed range", bn: "অনুমোদিত সীমা" },
  checkout_method: { en: "Payment method", bn: "পেমেন্টের মাধ্যম" },
  method_bangla_qr: { en: "Bangla QR", bn: "বাংলা কিউআর" },
  method_card: { en: "Card", bn: "কার্ড" },
  method_mfs: { en: "Mobile wallet (MFS)", bn: "মোবাইল ওয়ালেট (এমএফএস)" },
  checkout_continue: { en: "Continue to payment", bn: "পেমেন্টে এগিয়ে যান" },
  checkout_qr_title: { en: "Scan to pay (Bangla QR)", bn: "স্ক্যান করে পরিশোধ করুন (বাংলা কিউআর)" },
  checkout_qr_unavailable: {
    en: "QR payment is not available for this checkout right now. Please choose another method.",
    bn: "এই চেকআউটে কিউআর পেমেন্ট এখন উপলব্ধ নয়। অনুগ্রহ করে অন্য একটি পদ্ধতি বেছে নিন।",
  },
  checkout_qr_note: {
    en: "Scan this dynamic QR with your banking or MFS app. The code is bound to this payment only.",
    bn: "আপনার ব্যাংকিং বা এমএফএস অ্যাপ দিয়ে এই ডায়নামিক কিউআর স্ক্যান করুন। কোডটি কেবল এই পেমেন্টের জন্য নির্ধারিত।",
  },
  checkout_redirect_note: {
    en: "You will be redirected to complete this payment.",
    bn: "পেমেন্ট সম্পন্ন করতে আপনাকে অন্য পৃষ্ঠায় নেওয়া হবে।",
  },
  amount_out_of_bounds: { en: "Amount is outside the allowed range", bn: "পরিমাণ অনুমোদিত সীমার বাইরে" },
  link_paid_title: { en: "This payment is complete", bn: "এই পেমেন্ট সম্পন্ন হয়েছে" },
  link_paid_body: {
    en: "This payment link has already been paid. No further payment is due.",
    bn: "এই পেমেন্ট লিংকে ইতিমধ্যে পরিশোধ করা হয়েছে। আর কোনো পরিশোধের প্রয়োজন নেই।",
  },
  link_expired_title: { en: "This link has expired", bn: "এই লিংকের মেয়াদ শেষ হয়েছে" },
  link_expired_body: {
    en: "This payment link has expired. Please contact the merchant for a new link.",
    bn: "এই পেমেন্ট লিংকের মেয়াদ শেষ হয়ে গেছে। নতুন লিংকের জন্য অনুগ্রহ করে বিক্রেতার সাথে যোগাযোগ করুন।",
  },
  link_cancelled_title: { en: "This link was cancelled", bn: "এই লিংকটি বাতিল করা হয়েছে" },
  link_cancelled_body: {
    en: "The merchant cancelled this payment link. Contact the merchant if you believe this is a mistake.",
    bn: "বিক্রেতা এই পেমেন্ট লিংকটি বাতিল করেছেন। ভুল মনে হলে বিক্রেতার সাথে যোগাযোগ করুন।",
  },
  link_not_found_title: { en: "Link not found", bn: "লিংক পাওয়া যায়নি" },
  link_not_found_body: {
    en: "This payment link does not exist or is no longer available.",
    bn: "এই পেমেন্ট লিংকটি নেই অথবা আর পাওয়া যাচ্ছে না।",
  },

  // Sandbox signup (public, spec/16 §F)
  sbx_signup_title: { en: "Get a sandbox API key", bn: "স্যান্ডবক্স এপিআই কী নিন" },
  sbx_signup_intro: {
    en: "Sign up with your email to receive a test API key (bdpk_test_). The public sandbox runs only against simulated rails — no real money can move.",
    bn: "আপনার ইমেইল দিয়ে সাইন আপ করে একটি টেস্ট এপিআই কী (bdpk_test_) নিন। পাবলিক স্যান্ডবক্স কেবল সিমুলেটেড রেলের সাথে চলে — কোনো প্রকৃত অর্থ স্থানান্তর হয় না।",
  },
  sbx_email: { en: "Email", bn: "ইমেইল" },
  sbx_display_name: { en: "Display name", bn: "প্রদর্শিত নাম" },
  sbx_send_otp: { en: "Send verification code", bn: "যাচাই কোড পাঠান" },
  sbx_otp_sent: {
    en: "We emailed you a 6-digit code. Enter it below to verify your address.",
    bn: "আপনার ইমেইলে ৬-সংখ্যার একটি কোড পাঠানো হয়েছে। ঠিকানা যাচাই করতে নিচে কোডটি লিখুন।",
  },
  sbx_demo_otp: { en: "Demo code", bn: "ডেমো কোড" },
  sbx_otp_label: { en: "Verification code", bn: "যাচাই কোড" },
  sbx_verify: { en: "Verify", bn: "যাচাই করুন" },
  sbx_success_title: { en: "Your sandbox key is ready", bn: "আপনার স্যান্ডবক্স কী প্রস্তুত" },
  sbx_base_url: { en: "Sandbox base URL", bn: "স্যান্ডবক্স বেস ইউআরএল" },
  sbx_docs_link: { en: "API documentation", bn: "এপিআই ডকুমেন্টেশন" },
  sbx_revoked: {
    en: "This signup was revoked after too many failed attempts. Start again with a new signup.",
    bn: "অনেকবার ভুল চেষ্টার পর এই সাইনআপ প্রত্যাহার করা হয়েছে। নতুন সাইনআপ দিয়ে আবার শুরু করুন।",
  },
  sbx_expired: {
    en: "This verification code has expired. Start again to receive a new code.",
    bn: "এই যাচাই কোডের মেয়াদ শেষ হয়েছে। নতুন কোড পেতে আবার শুরু করুন।",
  },
  sbx_from_sandbox_link: {
    en: "Need a key? Self-serve sandbox signup",
    bn: "কী দরকার? স্ব-সেবা স্যান্ডবক্স সাইনআপ",
  },

  // Dining-wedge offers (spec/18 merchant surface)
  nav_offers: { en: "Dining offers", bn: "ডাইনিং অফার" },
  create_offer: { en: "Create offer", bn: "অফার তৈরি করুন" },
  create_offer_confirm: {
    en: "The offer is created as a DRAFT and becomes redeemable only after you activate it. You set every economic parameter; at least one redemption cap is mandatory (the circuit breaker is not optional).",
    bn: "অফারটি খসড়া (DRAFT) হিসেবে তৈরি হবে এবং আপনি সক্রিয় করার পরই ব্যবহারযোগ্য হবে। প্রতিটি আর্থিক শর্ত আপনি নির্ধারণ করেন; অন্তত একটি রিডেম্পশন সীমা বাধ্যতামূলক (সার্কিট ব্রেকার ঐচ্ছিক নয়)।",
  },
  edit_offer: { en: "Edit offer", bn: "অফার সম্পাদনা করুন" },
  edit_offer_confirm: {
    en: "Economic changes create a new immutable version; reservations already in flight keep the version they reserved against. Title text updates in place. Raising the total cap reactivates an EXHAUSTED offer.",
    bn: "আর্থিক পরিবর্তনে একটি নতুন অপরিবর্তনীয় সংস্করণ তৈরি হয়; চলমান রিজার্ভেশনগুলো তাদের সংরক্ষিত সংস্করণেই থাকে। শিরোনাম সরাসরি হালনাগাদ হয়। মোট সীমা বাড়ালে নিঃশেষিত (EXHAUSTED) অফার পুনরায় সক্রিয় হয়।",
  },
  activate_offer: { en: "Activate", bn: "সক্রিয় করুন" },
  activate_offer_confirm: {
    en: "Activating makes this offer discoverable and redeemable by signed-in diners within its time windows. Caps are enforced fail-closed: redemptions stop the moment a cap is reached.",
    bn: "সক্রিয় করলে সাইন-ইন করা ডাইনাররা নির্ধারিত সময়সীমার মধ্যে অফারটি দেখতে ও ব্যবহার করতে পারবেন। সীমা ফেল-ক্লোজড ভাবে প্রযোজ্য: সীমা পূরণ হওয়া মাত্র রিডেম্পশন বন্ধ হয়ে যায়।",
  },
  pause_offer: { en: "Pause", bn: "বিরতি দিন" },
  pause_offer_confirm: {
    en: "Pausing refuses new redemptions immediately. Already-reserved redemptions are honored. You can resume the offer at any time before it expires.",
    bn: "বিরতি দিলে নতুন রিডেম্পশন সাথে সাথে বন্ধ হবে। ইতিমধ্যে সংরক্ষিত রিডেম্পশনগুলো বহাল থাকবে। মেয়াদ শেষের আগে যেকোনো সময় পুনরায় চালু করতে পারবেন।",
  },
  resume_offer: { en: "Resume", bn: "পুনরায় চালু করুন" },
  resume_offer_confirm: {
    en: "Resuming re-opens this paused offer to new redemptions within its time windows.",
    bn: "পুনরায় চালু করলে বিরতিতে থাকা অফারটি নির্ধারিত সময়সীমার মধ্যে নতুন রিডেম্পশনের জন্য খুলে যাবে।",
  },
  archive_offer: { en: "Archive", bn: "আর্কাইভ করুন" },
  archive_offer_confirm: {
    en: "Archiving is permanent and irreversible: the offer is retired and can never be reactivated. Records are retained for 12 years (regulatory retention). Archiving is refused while reservations are outstanding.",
    bn: "আর্কাইভ স্থায়ী ও অপরিবর্তনীয়: অফারটি অবসরে যাবে এবং আর কখনও সক্রিয় করা যাবে না। রেকর্ড ১২ বছর সংরক্ষিত থাকবে (নিয়ন্ত্রক সংরক্ষণ)। রিজার্ভেশন বকেয়া থাকলে আর্কাইভ প্রত্যাখ্যাত হয়।",
  },
  offer_kind: { en: "Offer type", bn: "অফারের ধরন" },
  offer_percent: { en: "Discount (%)", bn: "ছাড় (%)" },
  offer_d2_note: {
    en: "Pilot is PERCENT_OFF-only: discounts of 5%-50%. BOGO is schema-reserved and not yet enabled (principal decision D2).",
    bn: "পাইলটে কেবল শতাংশ-ছাড় (PERCENT_OFF): ৫%-৫০%। BOGO স্কিমায় সংরক্ষিত, এখনও চালু হয়নি (প্রিন্সিপাল সিদ্ধান্ত D2)।",
  },
  offer_title_en: { en: "Title (English)", bn: "শিরোনাম (ইংরেজি)" },
  offer_title_bn: { en: "Title (Bengali)", bn: "শিরোনাম (বাংলা)" },
  offer_windows: { en: "Time windows", bn: "সময়সীমা" },
  offer_window_add: { en: "Add window", bn: "সময়সীমা যোগ করুন" },
  offer_window_remove: { en: "Remove", bn: "মুছুন" },
  offer_window_tz_note: {
    en: "Windows are Asia/Dhaka local time (no DST). Start is inclusive, end is exclusive. At most 7 rows; overlapping windows on the same day are rejected.",
    bn: "সময়সীমা ঢাকা (Asia/Dhaka) স্থানীয় সময়ে (ডিএসটি নেই)। শুরু অন্তর্ভুক্ত, শেষ বহির্ভূত। সর্বোচ্চ ৭টি সারি; একই দিনে ওভারল্যাপ করা সময়সীমা প্রত্যাখ্যাত হয়।",
  },
  offer_valid_from: { en: "Valid from (UTC)", bn: "কার্যকর শুরু (ইউটিসি)" },
  offer_valid_until: { en: "Valid until (UTC)", bn: "কার্যকর শেষ (ইউটিসি)" },
  offer_validity_note: {
    en: "Validity range is capped at 366 days.",
    bn: "কার্যকর মেয়াদ সর্বোচ্চ ৩৬৬ দিন।",
  },
  offer_min_spend: { en: "Minimum spend (BDT)", bn: "ন্যূনতম কেনাকাটা (টাকা)" },
  offer_max_discount: { en: "Maximum discount (BDT)", bn: "সর্বোচ্চ ছাড় (টাকা)" },
  offer_caps: { en: "Redemption caps", bn: "রিডেম্পশন সীমা" },
  offer_cap_per_day: { en: "Per day", bn: "প্রতিদিন" },
  offer_cap_total: { en: "Total", bn: "মোট" },
  offer_cap_per_customer: { en: "Per customer", bn: "প্রতি গ্রাহক" },
  offer_cap_required_note: {
    en: "At least one of the per-day / total caps is required — uncapped offers do not exist.",
    bn: "প্রতিদিন বা মোট সীমার অন্তত একটি বাধ্যতামূলক — সীমাহীন অফার বলে কিছু নেই।",
  },
  offer_cap_recredit: {
    en: "Re-credit cap slot on full refund",
    bn: "পূর্ণ ফেরতে সীমার স্লট পুনরায় যোগ করুন",
  },
  offer_methods: { en: "Allowed payment methods", bn: "অনুমোদিত পেমেন্ট পদ্ধতি" },
  offer_dynamic_qr_note: {
    en: "Offer redemption requires DYNAMIC-amount Bangla QR (the net is computed per transaction). Static-QR-only acceptance cannot run offers.",
    bn: "অফার রিডেম্পশনে ডায়নামিক-পরিমাণ বাংলা কিউআর প্রয়োজন (নিট অঙ্ক প্রতি লেনদেনে হিসাব হয়)। শুধু স্ট্যাটিক কিউআর গ্রহণকারীরা অফার চালাতে পারবেন না।",
  },
  offer_login_note: {
    en: "Diners must be signed in to redeem (decision D1) — per-customer caps are always enforceable. Guests cannot redeem.",
    bn: "রিডিম করতে ডাইনারদের সাইন-ইন থাকতে হবে (সিদ্ধান্ত D1) — প্রতি-গ্রাহক সীমা সর্বদা প্রযোজ্য। অতিথিরা রিডিম করতে পারবেন না।",
  },
  offer_state: { en: "State", bn: "অবস্থা" },
  offer_version: { en: "Version", bn: "সংস্করণ" },
  offer_analytics: { en: "Redemption analytics", bn: "রিডেম্পশন বিশ্লেষণ" },
  offer_redeemed_today: { en: "Redeemed today", bn: "আজ রিডিম হয়েছে" },
  offer_redeemed_total: { en: "Redeemed total", bn: "মোট রিডিম হয়েছে" },
  offer_reserved_now: { en: "Reserved now", bn: "এখন সংরক্ষিত" },
  offer_cap_monitor: { en: "Cap monitoring", bn: "সীমা পর্যবেক্ষণ" },
  offer_cap_warning: {
    en: "Above 80% of the cap — the offer pauses automatically (fail-closed) the moment the cap is reached.",
    bn: "সীমার ৮০%-এর উপরে — সীমা পূরণ হওয়া মাত্র অফারটি স্বয়ংক্রিয়ভাবে বন্ধ হয় (ফেল-ক্লোজড)।",
  },
  offer_counts_only_note: {
    en: "Analytics show counts only — never diner identities (data-protection posture). Server values are authoritative; this page renders, it never recomputes.",
    bn: "বিশ্লেষণে কেবল সংখ্যা দেখানো হয় — কখনও ডাইনারের পরিচয় নয় (ডেটা-সুরক্ষা নীতি)। সার্ভারের মানই চূড়ান্ত; এই পৃষ্ঠা কেবল প্রদর্শন করে, পুনরায় হিসাব করে না।",
  },
  offer_receipt_note: {
    en: "Receipts always show gross, discount and net amounts (consumer-protection requirement). The rail and the ledger only ever move the net.",
    bn: "রসিদে সর্বদা মোট, ছাড় ও নিট অঙ্ক দেখানো হয় (ভোক্তা-সুরক্ষা বাধ্যবাধকতা)। রেল ও লেজারে কেবল নিট অঙ্কই চলাচল করে।",
  },
  offer_none: { en: "No offers yet", bn: "এখনও কোনো অফার নেই" },

  // Reports
  reports_sha_note: {
    en: "Downloads are byte-identical on re-download. The X-BDPay-Content-Sha256 response header carries the SHA-256 of the exact bytes served; verify it against the value shown here.",
    bn: "পুনরায় ডাউনলোডে ফাইল বাইট-অভিন্ন থাকে। X-BDPay-Content-Sha256 হেডারে পরিবেশিত বাইটের SHA-256 থাকে; এখানে দেখানো মানের সাথে যাচাই করুন।",
  },
} as const;

export type CopyKey = keyof typeof COPY;

export function pick(bi: Bi, lang: Lang): string {
  return lang === "bn" ? bi.bn : bi.en;
}

// Bilingual copy objects (spec/15 §i18n — protein-chain-bd pattern).
// Safety-critical strings are rendered via DualLabel (both languages at once).

export type Lang = "en" | "bn";

export interface Bi {
  en: string;
  bn: string;
}

export const COPY = {
  appName: { en: "BD-PAY Ops Console", bn: "বিডি-পে অপস কনসোল" },
  nav_dashboard: { en: "Dashboard", bn: "ড্যাশবোর্ড" },
  nav_approvals: { en: "Approvals", bn: "অনুমোদন" },
  nav_cases: { en: "Cases", bn: "কেস" },
  nav_disputes: { en: "Disputes", bn: "বিরোধ" },
  nav_compensation: { en: "Compensation", bn: "ক্ষতিপূরণ" },
  nav_recon: { en: "Reconciliation", bn: "সমন্বয়" },
  nav_ledger: { en: "Ledger", bn: "খতিয়ান" },
  nav_connectors: { en: "Connectors", bn: "সংযোগকারী" },
  nav_operators: { en: "Operators", bn: "অপারেটর" },
  nav_camlco: { en: "CAMLCO", bn: "ক্যামেলকো" },
  nav_exports: { en: "Exports", bn: "এক্সপোর্ট" },
  login_title: { en: "Operator sign-in", bn: "অপারেটর সাইন-ইন" },
  login_email: { en: "Email", bn: "ইমেইল" },
  login_password: { en: "Password", bn: "পাসওয়ার্ড" },
  login_totp: { en: "TOTP code", bn: "টিওটিপি কোড" },
  login_submit: { en: "Sign in", bn: "সাইন ইন করুন" },
  login_lockout: {
    en: "3 consecutive TOTP failures lock the account for 30 minutes (BB ICT §6.2.6).",
    bn: "পরপর ৩ বার টিওটিপি ব্যর্থ হলে অ্যাকাউন্ট ৩০ মিনিটের জন্য লক হয়ে যাবে (বিবি আইসিটি §৬.২.৬)।",
  },
  logout: { en: "Sign out", bn: "সাইন আউট" },
  language: { en: "Language", bn: "ভাষা" },
  role: { en: "Role", bn: "ভূমিকা" },
  audit_notice: {
    en: "Every action on this console is audited with operator ID and timestamp (BB ICT §9.2). Mutations require TOTP step-up and, where catalogued, a second pair of eyes.",
    bn: "এই কনসোলের প্রতিটি কাজ অপারেটর আইডি ও সময়সহ নিরীক্ষিত হয় (বিবি আইসিটি §৯.২)। প্রতিটি পরিবর্তনে টিওটিপি যাচাই এবং তালিকাভুক্ত ক্ষেত্রে দ্বিতীয় অনুমোদনকারী প্রয়োজন।",
  },
  data_age: { en: "data age", bn: "তথ্যের বয়স" },
  refresh: { en: "Refresh", bn: "রিফ্রেশ" },
  loading: { en: "Loading…", bn: "লোড হচ্ছে…" },
  filter: { en: "Filter", bn: "ফিল্টার" },
  all: { en: "All", bn: "সব" },
  approve: { en: "Approve", bn: "অনুমোদন করুন" },
  reject: { en: "Reject", bn: "প্রত্যাখ্যান করুন" },
  withdraw: { en: "Withdraw", bn: "প্রত্যাহার করুন" },
  confirm: { en: "Confirm", bn: "নিশ্চিত করুন" },
  cancel: { en: "Cancel", bn: "বাতিল" },
  reason: { en: "Reason", bn: "কারণ" },
  approve_confirm: {
    en: "You are the second approver. This action will execute immediately on approval.",
    bn: "আপনি দ্বিতীয় অনুমোদনকারী। অনুমোদনের সাথে সাথে এই কাজটি কার্যকর হবে।",
  },
  reject_confirm: {
    en: "Reject this request. The underlying action will never execute.",
    bn: "এই অনুরোধটি প্রত্যাখ্যান করুন। মূল কাজটি আর কখনো কার্যকর হবে না।",
  },
  withdraw_confirm: {
    en: "Withdraw your own request before decision.",
    bn: "সিদ্ধান্তের আগে আপনার নিজের অনুরোধ প্রত্যাহার করুন।",
  },
  self_initiated_notice: {
    en: "You initiated this request — initiator and approver must differ. You may only withdraw.",
    bn: "এই অনুরোধটি আপনি শুরু করেছেন — সূচনাকারী ও অনুমোদনকারী ভিন্ন ব্যক্তি হতে হবে। আপনি শুধু প্রত্যাহার করতে পারবেন।",
  },
  totp_prompt: {
    en: "Enter your 6-digit TOTP code to confirm (step-up authentication).",
    bn: "নিশ্চিত করতে আপনার ৬-সংখ্যার টিওটিপি কোড লিখুন (ধাপ-আপ যাচাই)।",
  },
  freeze: { en: "Freeze", bn: "হিমায়িত করুন" },
  freeze_confirm: {
    en: "FREEZE applies immediately on initiation (ATA 2009). An admin pair must confirm within 24 hours.",
    bn: "হিমায়িতকরণ শুরু করার সাথে সাথেই কার্যকর হয় (সন্ত্রাসবিরোধী আইন ২০০৯)। ২৪ ঘণ্টার মধ্যে একজন অ্যাডমিন জোড়া নিশ্চিত করবেন।",
  },
  assign: { en: "Assign to me", bn: "আমাকে বরাদ্দ করুন" },
  comment: { en: "Comment", bn: "মন্তব্য" },
  add_comment: { en: "Add comment", bn: "মন্তব্য যোগ করুন" },
  attach_evidence: { en: "Attach evidence", bn: "প্রমাণ সংযুক্ত করুন" },
  resolve: { en: "Resolve", bn: "নিষ্পত্তি করুন" },
  resolve_confirm: {
    en: "Resolve this case. Money-touching resolutions require two-eyes approval.",
    bn: "এই কেসটি নিষ্পত্তি করুন। অর্থ-সংশ্লিষ্ট নিষ্পত্তিতে দুই-চোখ অনুমোদন প্রয়োজন।",
  },
  request_evidence: { en: "Request evidence", bn: "প্রমাণ চান" },
  mark_under_review: { en: "Mark under review", bn: "পর্যালোচনাধীন চিহ্নিত করুন" },
  resolve_merchant: { en: "Resolve for merchant", bn: "মার্চেন্টের পক্ষে নিষ্পত্তি" },
  resolve_customer: { en: "Resolve for customer", bn: "গ্রাহকের পক্ষে নিষ্পত্তি" },
  file_chargeback: { en: "File chargeback", bn: "চার্জব্যাক দাখিল করুন" },
  two_eyes_notice: {
    en: "Two-eyes: this outcome moves money and creates an ApprovalRequest for a second approver.",
    bn: "দুই-চোখ: এই ফলাফলে অর্থ স্থানান্তর হয় এবং দ্বিতীয় অনুমোদনকারীর জন্য একটি অনুমোদন-অনুরোধ তৈরি হয়।",
  },
  dispute_resolve_confirm: {
    en: "Dispute resolution may move money. A second approver must confirm before the refund executes.",
    bn: "বিরোধ নিষ্পত্তিতে অর্থ স্থানান্তর হতে পারে। রিফান্ড কার্যকর হওয়ার আগে দ্বিতীয় অনুমোদনকারীকে নিশ্চিত করতে হবে।",
  },
  comp_resolution: { en: "Compensation resolution", bn: "ক্ষতিপূরণ নিষ্পত্তি" },
  comp_resolve_confirm: {
    en: "Compensation resolution ALWAYS requires finance two-eyes. Money may have moved without a ledger entry.",
    bn: "ক্ষতিপূরণ নিষ্পত্তিতে সর্বদা ফাইন্যান্স দুই-চোখ অনুমোদন প্রয়োজন। খতিয়ানে এন্ট্রি ছাড়াই অর্থ স্থানান্তরিত হয়ে থাকতে পারে।",
  },
  first_touch: { en: "First touch (start investigating)", bn: "প্রথম স্পর্শ (তদন্ত শুরু করুন)" },
  mode_change: { en: "Request mode change", bn: "মোড পরিবর্তনের অনুরোধ" },
  mode_change_prod_notice: {
    en: "Promotion to PRODUCTION requires two-eyes: an admin must approve the ApprovalRequest.",
    bn: "প্রোডাকশনে উন্নীত করতে দুই-চোখ প্রয়োজন: একজন অ্যাডমিনকে অনুমোদন-অনুরোধটি অনুমোদন করতে হবে।",
  },
  reset_circuit: { en: "Reset circuit breaker", bn: "সার্কিট ব্রেকার রিসেট করুন" },
  reset_circuit_confirm: {
    en: "Force the breaker OPEN → HALF_OPEN (manual probe). This action is audited.",
    bn: "ব্রেকারকে জোরপূর্বক OPEN → HALF_OPEN করুন (ম্যানুয়াল প্রোব)। এই কাজটি নিরীক্ষিত হয়।",
  },
  disable_operator: { en: "Disable operator", bn: "অপারেটর নিষ্ক্রিয় করুন" },
  disable_operator_confirm: {
    en: "Disable is immediate: all sessions terminate and edge revocations are inserted.",
    bn: "নিষ্ক্রিয়করণ তাৎক্ষণিক: সব সেশন বন্ধ হবে এবং এজ প্রত্যাহার রেকর্ড যুক্ত হবে।",
  },
  sla_breached: { en: "SLA breached", bn: "এসএলএ লঙ্ঘিত" },
  payload_hash: { en: "Payload hash (what the approver approves)", bn: "পেলোড হ্যাশ (অনুমোদনকারী যা অনুমোদন করেন)" },
  conflict_409: {
    en: "Conflict: another operator decided this request first (one live request per subject).",
    bn: "দ্বন্দ্ব: অন্য একজন অপারেটর আগে এই অনুরোধের সিদ্ধান্ত নিয়েছেন (প্রতি বিষয়ে একটিই সক্রিয় অনুরোধ)।",
  },
  chain_verified: { en: "Hash chain VERIFIED", bn: "হ্যাশ চেইন যাচাইকৃত" },
  chain_failed: { en: "Hash chain verification FAILED", bn: "হ্যাশ চেইন যাচাই ব্যর্থ" },
  exports_sha_note: {
    en: "Downloads are byte-identical on re-download. The X-BDPay-Content-Sha256 response header carries the SHA-256 of the exact bytes served; verify it against the manifest value shown here.",
    bn: "পুনরায় ডাউনলোডে ফাইল বাইট-অভিন্ন থাকে। X-BDPay-Content-Sha256 হেডারে পরিবেশিত বাইটের SHA-256 থাকে; এখানে দেখানো ম্যানিফেস্ট মানের সাথে যাচাই করুন।",
  },
  mine_initiated: { en: "Initiated by me", bn: "আমার শুরু করা" },
  mine_awaiting: { en: "Awaiting me", bn: "আমার অপেক্ষায়" },
  runbook_title: { en: "Binding runbook checklist", bn: "বাধ্যতামূলক রানবুক চেকলিস্ট" },
  runbook_1: { en: "1. query_status the rail with the original connector_ref (truth attempt)", bn: "১. মূল connector_ref দিয়ে রেইলে query_status চালান (সত্য যাচাই)" },
  runbook_2: { en: "2. Check the next recon file for the rail movement", bn: "২. পরবর্তী সমন্বয় ফাইলে রেইল লেনদেন পরীক্ষা করুন" },
  runbook_3: { en: "3. Rail shows reversed ⇒ RAIL_REVERSED_CONFIRMED", bn: "৩. রেইল রিভার্স দেখালে ⇒ RAIL_REVERSED_CONFIRMED" },
  runbook_4: { en: "4. Rail shows charged ⇒ rail-side manual refund ⇒ MANUAL_RAIL_REFUND", bn: "৪. রেইল চার্জ দেখালে ⇒ রেইল-পাশে ম্যানুয়াল রিফান্ড ⇒ MANUAL_RAIL_REFUND" },
  runbook_5: { en: "5. Amount differs/ambiguous ⇒ LEDGER_ADJUSTMENT to recorded truth", bn: "৫. পরিমাণ ভিন্ন/অস্পষ্ট হলে ⇒ রেকর্ডকৃত সত্যে LEDGER_ADJUSTMENT" },
  runbook_6: { en: "6. Unrecoverable ⇒ WRITE_OFF", bn: "৬. অপুনরুদ্ধারযোগ্য হলে ⇒ WRITE_OFF" },
  error_generic: { en: "Request failed", bn: "অনুরোধ ব্যর্থ হয়েছে" },
} as const;

export type CopyKey = keyof typeof COPY;

export function pick(bi: Bi, lang: Lang): string {
  return lang === "bn" ? bi.bn : bi.en;
}

export const RUNBOOK_KEYS: CopyKey[] = [
  "runbook_1",
  "runbook_2",
  "runbook_3",
  "runbook_4",
  "runbook_5",
  "runbook_6",
];

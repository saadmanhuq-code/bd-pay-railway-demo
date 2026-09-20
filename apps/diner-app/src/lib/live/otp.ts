// F08 — phone-bound diner OTP challenges for the live BFF.
//
// The live adapter used to return its ONE configured OTP in the challenge
// response body (`debug_otp_code`) and, on any verification against that shared
// code, issue a session for a single configured demo customer. Two different
// phones therefore mapped to one customer, and the second phone never needed a
// challenge at all (review F08 / evidence E34, E36).
//
// This module is the replacement: a challenge is minted per phone, its code is
// never returned to the caller, it carries an expiry, an attempt limit and
// single-use replay protection, and it resolves to a customer id DERIVED from
// the phone so distinct phones are distinct customers.
//
// Kept free of next/server so it is unit-testable without the Next runtime.
//
// Bounded-demonstrator note: challenges live in this server process. A
// multi-instance deployment needs a shared store (Redis / the gateway), and
// production additionally needs a real delivery channel — which is exactly why
// `challengeRefusalInProduction()` fails closed rather than pretending.

import { isMockEnabled } from "@bdpay/edge/env-flag";

export const OTP_TTL_SECONDS = 300;
export const OTP_MAX_ATTEMPTS = 5;
export const OTP_CODE_LENGTH = 6;

const textEncoder = new TextEncoder();

const BN_DIGITS = "০১২৩৪৫৬৭৮৯";

/** Converts Bangla digits to ASCII; leaves every other character untouched. */
export function normalizeDigits(input: string): string {
  let out = "";
  for (const ch of input) {
    const idx = BN_DIGITS.indexOf(ch);
    out += idx >= 0 ? String(idx) : ch;
  }
  return out;
}

/**
 * Finding 341 (diner-phone-aliases-split-identity-and-attempt-budget): the
 * route accepts four spellings of the same physical number — 017…, +88017…,
 * 88017…, and 17… — and used to store challenges / derive customer ids from
 * whichever spelling the caller happened to send, so one phone minted
 * distinct customer ids and independent five-attempt OTP budgets depending on
 * formatting alone.
 *
 * This collapses every accepted spelling to ONE canonical form — the
 * national 11-digit form `01XXXXXXXXX` — before it reaches challenge
 * storage, `customerIdForPhone`, or `maskPhone`. Callers MUST validate the
 * input against the accepted-format check (e.g. `BD_MOBILE_RE`) before
 * calling this: it strips whatever recognized prefix is present and does not
 * itself re-validate the remaining digits.
 */
export function canonicalizePhone(input: string): string {
  const digits = normalizeDigits(input).replace(/[\s-]/g, "");
  let national = digits;
  if (national.startsWith("+880")) national = national.slice(4);
  else if (national.startsWith("880")) national = national.slice(3);
  else if (national.startsWith("0")) national = national.slice(1);
  return `0${national}`;
}

export interface OtpChallenge {
  challengeId: string;
  phone: string;
  codeHash: string;
  expiresAt: number;
  attempts: number;
  consumed: boolean;
}

export type ChallengeRefusal =
  | { code: "otp_fixture_secret_refused"; message: string }
  | { code: "otp_delivery_unconfigured"; message: string };

// One challenge per phone: requesting a new code invalidates the previous one.
const challenges = new Map<string, OtpChallenge>();

export function isProductionMode(): boolean {
  return (
    process.env.NODE_ENV === "production" && !isMockEnabled(process.env.BDPAY_ENABLE_MOCK)
  );
}

/**
 * Why a production challenge must be refused, or null when it may proceed.
 *
 * A build-time fixture OTP is a shared static secret: anyone who knows it can
 * sign in as any phone. It is a development affordance and is refused outright
 * in production. With the fixture gone this BFF has no way to DELIVER a code
 * (there is no SMS connector here), so production fails closed rather than
 * minting a code nobody can receive.
 */
export function challengeRefusalInProduction(): ChallengeRefusal | null {
  if (!isProductionMode()) return null;
  if ((process.env.BDPAY_LIVE_DINER_OTP || "").trim() !== "") {
    return {
      code: "otp_fixture_secret_refused",
      message:
        "BDPAY_LIVE_DINER_OTP is a shared fixture secret and is refused in production.",
    };
  }
  return {
    code: "otp_delivery_unconfigured",
    message: "No OTP delivery channel is configured for production diner sign-in.",
  };
}

// 250 = 25 * 10: bytes 0..249 map onto the ten digits evenly, and 250..255 are
// rejected. `byte % 10` over the full 0..255 range would skew towards 0-5.
const DIGIT_REJECTION_CEILING = 250;

function randomCode(): string {
  const buffer = new Uint8Array(OTP_CODE_LENGTH * 2);
  let code = "";
  while (code.length < OTP_CODE_LENGTH) {
    crypto.getRandomValues(buffer);
    for (const byte of buffer) {
      if (byte >= DIGIT_REJECTION_CEILING) continue;
      code += String(byte % 10);
      if (code.length === OTP_CODE_LENGTH) break;
    }
  }
  return code;
}

function bytesToHex(bytes: Uint8Array): string {
  let out = "";
  for (const byte of bytes) out += byte.toString(16).padStart(2, "0");
  return out;
}

async function sha256Hex(value: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", textEncoder.encode(value));
  return bytesToHex(new Uint8Array(digest));
}

function constantTimeEqual(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i += 1) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

function customerSalt(): string {
  return (
    process.env.BDPAY_SESSION_SECRET?.trim() ||
    process.env.BDPAY_MOCK_SESSION_SECRET?.trim() ||
    "bdpay-diner-app-dev-session-secret-v1"
  );
}

/**
 * The customer this phone signs in as. Derived, so two phones are two
 * customers and one phone is the SAME customer across sign-ins; the phone
 * number itself never becomes the identifier.
 */
export async function customerIdForPhone(phone: string): Promise<string> {
  const digest = await sha256Hex(`bdpay-diner-customer:${customerSalt()}:${phone}`);
  return `cust_${digest.slice(0, 24)}`;
}

export function maskPhone(phone: string): string {
  if (phone.length <= 5) return "*".repeat(phone.length);
  return `${phone.slice(0, 3)}${"*".repeat(phone.length - 5)}${phone.slice(-2)}`;
}

export interface IssuedChallenge {
  challengeId: string;
  expiresAt: string;
  /** Present ONLY outside production, for the developer console. Never in a response body. */
  deliverableCode: string;
}

/**
 * Mint a challenge bound to `phone`. The code is returned to the CALLER OF THIS
 * FUNCTION for out-of-band delivery only — the route must never put it in a
 * response body.
 */
export async function issueChallenge(
  phone: string,
  now: number = Date.now(),
): Promise<IssuedChallenge> {
  const code = isProductionMode()
    ? randomCode()
    : (process.env.BDPAY_LIVE_DINER_OTP || "").trim() || randomCode();
  const challengeId = `chl_${bytesToHex(crypto.getRandomValues(new Uint8Array(12)))}`;
  const expiresAt = now + OTP_TTL_SECONDS * 1000;
  // The attempt budget is carried across a re-request within the same TTL:
  // otherwise an attacker resets it to zero simply by asking for a new code,
  // and the limit bounds nothing.
  const previous = challenges.get(phone);
  const spentAttempts =
    previous !== undefined && !previous.consumed && previous.expiresAt > now
      ? previous.attempts
      : 0;
  challenges.set(phone, {
    challengeId,
    phone,
    codeHash: await sha256Hex(`${challengeId}:${code}`),
    expiresAt,
    attempts: spentAttempts,
    consumed: false,
  });
  return {
    challengeId,
    expiresAt: new Date(expiresAt).toISOString(),
    deliverableCode: code,
  };
}

export type VerifyResult =
  | { ok: true; customerId: string }
  | { ok: false; code: "otp_challenge_required" | "otp_expired" | "otp_attempts_exhausted" | "otp_invalid" };

/**
 * Verify `code` against the challenge bound to `phone`.
 *
 * Fails closed on every miss: no challenge for this phone, an expired one, one
 * whose attempt budget is spent, or one already consumed (replay).
 *
 * Finding 355 (single-flight): this used to increment `attempts`, `await` the
 * hash comparison, and only THEN delete the challenge — so two concurrent
 * verifies with the same valid code both ran their (synchronous) pre-checks
 * before either deleted anything, both hashed correctly, and both minted a
 * session. The challenge is now removed from the map SYNCHRONOUSLY, before
 * the only `await` in this function, so a second call racing in here (same
 * tick, before the first call's hash resolves) finds nothing to verify and
 * gets a deterministic `otp_challenge_required` refusal. A wrong code
 * restores the challenge — with the spent attempt counted — so the budget
 * still counts down and a subsequent correct code can still be tried; the
 * restore is skipped if a newer challenge has since been issued for this
 * phone, so a re-request always wins over a stale in-flight verify.
 */
export async function verifyChallenge(
  phone: string,
  code: string,
  now: number = Date.now(),
): Promise<VerifyResult> {
  const challenge = challenges.get(phone);
  if (challenge === undefined || challenge.consumed) {
    return { ok: false, code: "otp_challenge_required" };
  }
  if (challenge.expiresAt <= now) {
    challenges.delete(phone);
    return { ok: false, code: "otp_expired" };
  }
  if (challenge.attempts >= OTP_MAX_ATTEMPTS) {
    // Kept (not deleted) until the TTL elapses so the spent budget is carried
    // into any re-requested challenge for this phone.
    return { ok: false, code: "otp_attempts_exhausted" };
  }
  // Mark in flight: gone from the map before the only await below, so a
  // concurrent call cannot also observe (and pass) this same challenge.
  challenges.delete(phone);
  const spentAttempts = challenge.attempts + 1;
  const candidate = await sha256Hex(`${challenge.challengeId}:${code}`);
  if (!constantTimeEqual(candidate, challenge.codeHash)) {
    // Wrong code: restore the challenge — with the attempt counted — unless a
    // fresher challenge for this phone has since replaced it.
    if (!challenges.has(phone)) {
      challenges.set(phone, { ...challenge, attempts: spentAttempts });
    }
    return { ok: false, code: "otp_invalid" };
  }
  // Single use: a replayed code cannot mint a second session — already
  // removed above, so no `delete` is needed on the success path.
  return { ok: true, customerId: await customerIdForPhone(phone) };
}

/** Test seam: drop every stored challenge. */
export function resetChallenges(): void {
  challenges.clear();
}

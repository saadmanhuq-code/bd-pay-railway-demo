import { createHmac } from "node:crypto";

import type { ErrorType, Persona } from "./types";

export const LIVE_MERCHANT_LOGIN_UNCONFIGURED_CODE = "merchant_login_unconfigured";
export const LIVE_MERCHANT_LOGIN_UNCONFIGURED_MESSAGE =
  "Live merchant login is not configured. Set BDPAY_LIVE_MERCHANT_EMAIL and BDPAY_LIVE_MERCHANT_TOTP_SECRET_B32; any 6-digit code is not a credential.";

export const LIVE_MERCHANT_TOTP_INVALID_CODE = "totp_invalid";
export const LIVE_MERCHANT_TOTP_INVALID_MESSAGE = "TOTP verification failed.";

const BASE32_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";
const STEP_SECONDS = 30;
const WINDOW_STEPS = 1;

export type LiveMerchantLoginDecision =
  | { ok: true; persona: Persona; email: string }
  | {
      ok: false;
      status: 400 | 401;
      type: ErrorType;
      code: string;
      message: string;
    };

const PERSONAS: readonly Persona[] = ["owner", "developer", "finance_maker", "finance_checker"];

function isPersona(value: string): value is Persona {
  return (PERSONAS as readonly string[]).includes(value);
}

function decodeBase32(value: string): Buffer | null {
  const normalized = value.replace(/[\s=]/g, "").toUpperCase();
  if (normalized === "") return null;
  let bits = 0;
  let bitCount = 0;
  const bytes: number[] = [];
  for (const ch of normalized) {
    const idx = BASE32_ALPHABET.indexOf(ch);
    if (idx < 0) return null;
    bits = (bits << 5) | idx;
    bitCount += 5;
    if (bitCount >= 8) {
      bytes.push((bits >> (bitCount - 8)) & 0xff);
      bitCount -= 8;
    }
  }
  return Buffer.from(bytes);
}

function counterBuffer(counter: number): Buffer {
  const buf = Buffer.alloc(8);
  let value = BigInt(counter);
  for (let i = 7; i >= 0; i -= 1) {
    buf[i] = Number(value & 0xffn);
    value >>= 8n;
  }
  return buf;
}

function constantTimeEqual(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i += 1) {
    diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  }
  return diff === 0;
}

function hotp(secret: Buffer, counter: number): string {
  const digest = createHmac("sha1", secret).update(counterBuffer(counter)).digest();
  const offset = digest.readUInt8(digest.length - 1) & 0x0f;
  const binary =
    ((digest.readUInt8(offset) & 0x7f) << 24) |
    ((digest.readUInt8(offset + 1) & 0xff) << 16) |
    ((digest.readUInt8(offset + 2) & 0xff) << 8) |
    (digest.readUInt8(offset + 3) & 0xff);
  return String(binary % 1_000_000).padStart(6, "0");
}

export function totpAt(secretB32: string, atMs: number): string | null {
  const secret = decodeBase32(secretB32);
  if (secret === null || secret.length === 0) return null;
  const counter = Math.floor(atMs / 1000 / STEP_SECONDS);
  return hotp(secret, counter);
}

function verifyTotp(secretB32: string, code: string, atMs: number): boolean {
  const secret = decodeBase32(secretB32);
  if (secret === null || secret.length === 0) return false;
  const counter = Math.floor(atMs / 1000 / STEP_SECONDS);
  let matched = false;
  for (let offset = -WINDOW_STEPS; offset <= WINDOW_STEPS; offset += 1) {
    const step = counter + offset;
    if (step < 0) continue;
    if (constantTimeEqual(code, hotp(secret, step))) matched = true;
  }
  return matched;
}

function envValue(env: Record<string, string | undefined>, name: string): string {
  return (env[name] ?? "").trim();
}

export function verifyLiveMerchantLogin(input: {
  email: string;
  totpCode: string;
  persona: string;
  atMs?: number;
  env?: Record<string, string | undefined>;
}): LiveMerchantLoginDecision {
  const env = input.env ?? {};
  const email = input.email.trim().toLowerCase();
  const totpCode = input.totpCode.trim();
  if (!email.includes("@")) {
    return {
      ok: false,
      status: 400,
      type: "invalid_request",
      code: "email_required",
      message: "A valid email is required.",
    };
  }
  if (!isPersona(input.persona)) {
    return {
      ok: false,
      status: 400,
      type: "invalid_request",
      code: "persona_invalid",
      message: "Choose a valid merchant persona.",
    };
  }
  if (!/^\d{6}$/.test(totpCode)) {
    return {
      ok: false,
      status: 401,
      type: "authentication",
      code: LIVE_MERCHANT_TOTP_INVALID_CODE,
      message: LIVE_MERCHANT_TOTP_INVALID_MESSAGE,
    };
  }

  const expectedEmail = envValue(env, "BDPAY_LIVE_MERCHANT_EMAIL").toLowerCase();
  const secretB32 = envValue(env, "BDPAY_LIVE_MERCHANT_TOTP_SECRET_B32");
  if (expectedEmail === "" || secretB32 === "") {
    return {
      ok: false,
      status: 401,
      type: "authentication",
      code: LIVE_MERCHANT_LOGIN_UNCONFIGURED_CODE,
      message: LIVE_MERCHANT_LOGIN_UNCONFIGURED_MESSAGE,
    };
  }

  const emailOk = constantTimeEqual(email, expectedEmail);
  const totpOk = verifyTotp(secretB32, totpCode, input.atMs ?? Date.now());
  if (!emailOk || !totpOk) {
    return {
      ok: false,
      status: 401,
      type: "authentication",
      code: LIVE_MERCHANT_TOTP_INVALID_CODE,
      message: LIVE_MERCHANT_TOTP_INVALID_MESSAGE,
    };
  }

  return { ok: true, persona: input.persona, email };
}

import { createHmac } from "node:crypto";

export type MockTotpResult = "ok" | "invalid" | "secret_missing" | "secret_invalid";

const DEV_TOTP_SECRET_B32 = "JBSWY3DPEHPK3PXP";
const BASE32_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";
const STEP_SECONDS = 30;
const WINDOW_STEPS = 1;

function configuredSecret(): string | null {
  const configured = process.env.BDPAY_MOCK_TOTP_SECRET_B32?.trim();
  if (configured) return configured;
  if (process.env.NODE_ENV === "production") return null;
  return DEV_TOTP_SECRET_B32;
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

function constantTimeEqual(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i += 1) {
    diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  }
  return diff === 0;
}

export function verifyMockTotp(code: string, atMs = Date.now()): MockTotpResult {
  const normalized = code.trim();
  if (!/^\d{6}$/.test(normalized)) return "invalid";
  const secretRaw = configuredSecret();
  if (secretRaw === null) return "secret_missing";
  const secret = decodeBase32(secretRaw);
  if (secret === null || secret.length === 0) return "secret_invalid";
  const counter = Math.floor(atMs / 1000 / STEP_SECONDS);
  for (let offset = -WINDOW_STEPS; offset <= WINDOW_STEPS; offset += 1) {
    if (constantTimeEqual(normalized, hotp(secret, counter + offset))) return "ok";
  }
  return "invalid";
}

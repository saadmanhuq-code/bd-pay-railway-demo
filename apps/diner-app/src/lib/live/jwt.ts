// Minimal HS256 JWT signer for the live adapter -> gateway boundary.
// The gateway expects a customer JWT (sub=cust_*, scope, kyc_tier, jti, exp).
// Only the live adapter uses this; no browser code imports this module.

const textEncoder = new TextEncoder();

function base64Url(bytes: Uint8Array): string {
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
}

function encodeText(value: string): string {
  return base64Url(textEncoder.encode(value));
}

export interface DinerJwtClaims {
  sub: string;
  scope: string[];
  kyc_tier: string;
  jti: string;
  iat: number;
  exp: number;
}

export async function signDinerJwt(claims: DinerJwtClaims, secret: string): Promise<string> {
  const header = { alg: "HS256", typ: "JWT" };
  const headerB64 = encodeText(JSON.stringify(header));
  const payloadB64 = encodeText(JSON.stringify(claims));
  const signingInput = `${headerB64}.${payloadB64}`;
  const key = await crypto.subtle.importKey(
    "raw",
    textEncoder.encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const signature = await crypto.subtle.sign("HMAC", key, textEncoder.encode(signingInput));
  return `${signingInput}.${base64Url(new Uint8Array(signature))}`;
}

export function liveJwtSecret(): string | null {
  const configured = process.env.BDPAY_LIVE_DINER_JWT_SECRET?.trim();
  if (configured) return configured;
  if (process.env.NODE_ENV === "production") return null;
  // Dev-only fallback; matches the default gateway test secret so a local
  // diner-app against a local gateway works out of the box.
  return "unit-test-jwt-secret";
}

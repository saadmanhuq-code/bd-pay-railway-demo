// POST /v1/public/payment-links/{public_code}/intents — public, rate-limited;
// server-generated idempotency from public_code + payer inputs (spec/16 §C).
// No session / TOTP / Idempotency-Key headers required on this surface.

import { NextResponse, type NextRequest } from "next/server";
import type { CheckoutMethod } from "@/lib/api/launchTypes";
import { createPublicIntentMock } from "@/lib/mock/launch";
import { json, mockDisabled, readBody, str, strOrNull } from "@/lib/mock/launchHttp";

export const dynamic = "force-dynamic";

export async function POST(req: NextRequest, ctx: { params: Promise<{ code: string }> }): Promise<NextResponse> {
  const disabled = mockDisabled();
  if (disabled) return disabled;
  const body = await readBody(req);
  const methodRaw = str(body, "method");
  const method: CheckoutMethod = methodRaw === "CARD" ? "CARD" : methodRaw === "MFS" ? "MFS" : "BANGLA_QR";
  const { code } = await ctx.params;
  return json(createPublicIntentMock(code, strOrNull(body, "amount_minor"), method));
}

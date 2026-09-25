// GET /v1/public/payment-links/{public_code}/intents/{payment_intent_id} —
// public (no session), mock simulator only: the hosted checkout's sandbox
// authorisation page reads the pending intent it is about to approve.

import { NextResponse, type NextRequest } from "next/server";
import { getPublicIntentMock } from "@/lib/mock/launch";
import { json, mockDisabled } from "@/lib/mock/launchHttp";

export const dynamic = "force-dynamic";

export async function GET(
  _req: NextRequest,
  ctx: { params: Promise<{ code: string; pi: string }> },
): Promise<NextResponse> {
  const disabled = mockDisabled();
  if (disabled) return disabled;
  const { code, pi } = await ctx.params;
  return json(getPublicIntentMock(code, pi));
}

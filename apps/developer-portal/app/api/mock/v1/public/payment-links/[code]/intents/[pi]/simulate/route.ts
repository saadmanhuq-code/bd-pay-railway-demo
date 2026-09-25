// POST /v1/public/payment-links/{public_code}/intents/{payment_intent_id}/simulate
// — public, mock simulator only. Stands in for the acquirer / MFS
// authorisation step of the hosted checkout: {"outcome": "succeed" | "fail"}.

import { NextResponse, type NextRequest } from "next/server";
import { simulatePublicIntentMock } from "@/lib/mock/launch";
import { json, mockDisabled, readBody, str } from "@/lib/mock/launchHttp";

export const dynamic = "force-dynamic";

export async function POST(
  req: NextRequest,
  ctx: { params: Promise<{ code: string; pi: string }> },
): Promise<NextResponse> {
  const disabled = mockDisabled();
  if (disabled) return disabled;
  const body = await readBody(req);
  const outcome = str(body, "outcome") === "fail" ? "fail" : "succeed";
  const { code, pi } = await ctx.params;
  return json(simulatePublicIntentMock(code, pi, outcome));
}

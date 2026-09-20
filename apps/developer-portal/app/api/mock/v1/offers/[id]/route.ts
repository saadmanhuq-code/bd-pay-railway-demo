// GET /v1/offers/{offer_id} — single offer INCLUDING the live counter readout
// { redeemed_today, redeemed_total, reserved_now } (spec/18 §API surface).
// Counts only, never customer identities (PDPO posture).

import { NextResponse, type NextRequest } from "next/server";
import { getOfferMock } from "@/lib/mock/offers";
import { json, mockDisabled, requireSession } from "@/lib/mock/launchHttp";

export const dynamic = "force-dynamic";

export async function GET(req: NextRequest, ctx: { params: Promise<{ id: string }> }): Promise<NextResponse> {
  const denied = mockDisabled() ?? (await requireSession(req));
  if (denied) return denied;
  const { id } = await ctx.params;
  return json(getOfferMock(id));
}

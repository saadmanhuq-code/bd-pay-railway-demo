// GET /v1/payment-links/{plink_id} — merchant fetch; includes
// payment_intent_id once paid (spec/16 §C).

import { NextResponse, type NextRequest } from "next/server";
import { findPaymentLinkMock } from "@/lib/mock/launch";
import { json, mockDisabled, requireSession } from "@/lib/mock/launchHttp";
import { mockError } from "@/lib/mock/store";

export const dynamic = "force-dynamic";

export async function GET(req: NextRequest, ctx: { params: Promise<{ id: string }> }): Promise<NextResponse> {
  const denied = mockDisabled() ?? (await requireSession(req));
  if (denied) return denied;
  const { id } = await ctx.params;
  const found = findPaymentLinkMock(id);
  if (!found) return json(mockError("not_found", "payment_link_not_found", "No such payment link."));
  return NextResponse.json(found, { status: 200 });
}

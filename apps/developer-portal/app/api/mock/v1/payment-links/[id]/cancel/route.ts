// POST /v1/payment-links/{plink_id}/cancel — ACTIVE → CANCELLED (spec/16 §C;
// merchant key, payment:write, Idempotency-Key).

import { type NextRequest, NextResponse } from "next/server";
import { cancelPaymentLinkMock } from "@/lib/mock/launch";
import { json, mockDisabled, requireMutationHeaders, requireSession } from "@/lib/mock/launchHttp";

export const dynamic = "force-dynamic";

export async function POST(req: NextRequest, ctx: { params: Promise<{ id: string }> }): Promise<NextResponse> {
  const denied = mockDisabled() ?? (await requireSession(req)) ?? requireMutationHeaders(req);
  if (denied) return denied;
  const { id } = await ctx.params;
  return json(cancelPaymentLinkMock(id));
}

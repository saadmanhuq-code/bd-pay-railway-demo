// GET /v1/public/payment-links/{public_code} — PII-free public view consumed
// by the hosted checkout page (spec/16 §C). Non-ACTIVE links still return the
// view (the page renders a terminal screen, never an error envelope).

import { NextResponse, type NextRequest } from "next/server";
import { publicLinkViewMock } from "@/lib/mock/launch";
import { json, mockDisabled } from "@/lib/mock/launchHttp";
import { mockError } from "@/lib/mock/store";

export const dynamic = "force-dynamic";

export async function GET(_req: NextRequest, ctx: { params: Promise<{ code: string }> }): Promise<NextResponse> {
  const disabled = mockDisabled();
  if (disabled) return disabled;
  const { code } = await ctx.params;
  const view = publicLinkViewMock(code);
  if (view === null) {
    return json(mockError("not_found", "payment_link_not_found", "No such payment link."));
  }
  return NextResponse.json(view, { status: 200 });
}

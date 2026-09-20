// POST /v1/offers/{offer_id}/{activate|pause|resume|archive} — sub-resource
// POSTs driving the Offer FSM (spec/18 §API surface; refusal-first, idempotent
// replays, invalid transition → 409 conflict/invalid_state_transition), plus
// POST /v1/offers/{offer_id}/edit (errata S18-E17): economic fields are
// edit-by-versioning, title fields mutate in place.

import { NextResponse, type NextRequest } from "next/server";
import type { EditOfferInput } from "@/lib/api/offerTypes";
import { editOfferMock, offerLifecycleMock, type OfferLifecycleAction } from "@/lib/mock/offers";
import { json, mockDisabled, mockErrorResponse, readBody, requireMutationHeaders, requireSession } from "@/lib/mock/launchHttp";

export const dynamic = "force-dynamic";

const LIFECYCLE_ACTIONS = new Set(["activate", "pause", "resume", "archive"]);

export async function POST(
  req: NextRequest,
  ctx: { params: Promise<{ id: string; action: string }> },
): Promise<NextResponse> {
  const denied = mockDisabled() ?? (await requireSession(req)) ?? requireMutationHeaders(req);
  if (denied) return denied;
  const { id, action } = await ctx.params;
  if (LIFECYCLE_ACTIONS.has(action)) {
    return json(offerLifecycleMock(id, action as OfferLifecycleAction));
  }
  if (action === "edit") {
    const body = await readBody(req);
    return json(editOfferMock(id, body as unknown as EditOfferInput));
  }
  return mockErrorResponse("not_found", "unknown_route", "Unknown mock route.");
}

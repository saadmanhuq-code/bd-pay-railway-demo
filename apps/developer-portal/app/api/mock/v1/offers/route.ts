// /v1/offers — merchant list (GET, offers:read) + create (POST, offers:write;
// Idempotency-Key required — it is the off_<sha> preimage's created_idem_key)
// per spec/18 §API surface. Mirrors bdpay/gateway/routes_offers.py +
// bdpay/kernel/offers/service.py shapes byte-for-byte in envelope structure.
// NOTE: no env query parameter on this surface (errata S18-E19).

import { NextResponse, type NextRequest } from "next/server";
import type { CreateOfferInput } from "@/lib/api/offerTypes";
import { createOfferMock, listOffersMock } from "@/lib/mock/offers";
import { json, mockDisabled, readBody, requireMutationHeaders, requireSession } from "@/lib/mock/launchHttp";

export const dynamic = "force-dynamic";

export async function GET(req: NextRequest): Promise<NextResponse> {
  const denied = mockDisabled() ?? (await requireSession(req));
  if (denied) return denied;
  return json(listOffersMock(req.nextUrl.searchParams.get("state")));
}

export async function POST(req: NextRequest): Promise<NextResponse> {
  const denied = mockDisabled() ?? (await requireSession(req)) ?? requireMutationHeaders(req);
  if (denied) return denied;
  const body = await readBody(req);
  const idempotencyKey = req.headers.get("idempotency-key") ?? "";
  return json(createOfferMock(body as unknown as CreateOfferInput, idempotencyKey));
}

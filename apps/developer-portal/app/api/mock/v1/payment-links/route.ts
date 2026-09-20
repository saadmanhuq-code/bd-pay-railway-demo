// /v1/payment-links — merchant list (GET, payment:read) + create (POST,
// payment:write; Idempotency-Key required) per spec/16 §C.

import { NextResponse, type NextRequest } from "next/server";
import type { Env, Paginated } from "@/lib/api/types";
import type { PaymentLink } from "@/lib/api/launchTypes";
import { createPaymentLinkMock, listPaymentLinksMock } from "@/lib/mock/launch";
import { json, mockDisabled, readBody, requireMutationHeaders, requireSession, str, strOrNull } from "@/lib/mock/launchHttp";

export const dynamic = "force-dynamic";

export async function GET(req: NextRequest): Promise<NextResponse> {
  const denied = mockDisabled() ?? (await requireSession(req));
  if (denied) return denied;
  const env: Env = req.nextUrl.searchParams.get("env") === "live" ? "live" : "sandbox";
  const body: Paginated<PaymentLink> = { data: listPaymentLinksMock(env), next_cursor: null };
  return NextResponse.json(body, { status: 200 });
}

export async function POST(req: NextRequest): Promise<NextResponse> {
  const denied = mockDisabled() ?? (await requireSession(req)) ?? requireMutationHeaders(req);
  if (denied) return denied;
  const body = await readBody(req);
  const env: Env = str(body, "env") === "live" ? "live" : "sandbox";
  const expiryRaw = body["expiry_hours"];
  return json(
    createPaymentLinkMock({
      env,
      description: str(body, "description"),
      amountMinor: strOrNull(body, "amount_minor"),
      amountMinMinor: strOrNull(body, "amount_min_minor"),
      amountMaxMinor: strOrNull(body, "amount_max_minor"),
      singleUse: body["single_use"] === true,
      expiryHours: typeof expiryRaw === "number" ? expiryRaw : null,
    }),
  );
}

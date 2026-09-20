// POST /v1/sandbox/signups/{sbxs_id}/verify — email OTP verification
// (spec/16 §F). The mock OTP is generated per signup; 5 attempts max then
// REVOKED; "999999" demonstrates the expired path deterministically.

import { NextResponse, type NextRequest } from "next/server";
import { verifySandboxSignupMock } from "@/lib/mock/launch";
import { json, mockDisabled, readBody, str } from "@/lib/mock/launchHttp";

export const dynamic = "force-dynamic";

export async function POST(req: NextRequest, ctx: { params: Promise<{ id: string }> }): Promise<NextResponse> {
  const disabled = mockDisabled();
  if (disabled) return disabled;
  const body = await readBody(req);
  const { id } = await ctx.params;
  return json(verifySandboxSignupMock(id, str(body, "otp")));
}

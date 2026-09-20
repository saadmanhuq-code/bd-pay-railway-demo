// POST /v1/sandbox/signups — public self-serve sandbox signup (spec/16 §F).
// Pre-auth surface: no session, no TOTP step-up.

import { NextResponse, type NextRequest } from "next/server";
import { createSandboxSignupMock } from "@/lib/mock/launch";
import { json, mockDisabled, readBody, str } from "@/lib/mock/launchHttp";

export const dynamic = "force-dynamic";

export async function POST(req: NextRequest): Promise<NextResponse> {
  const disabled = mockDisabled();
  if (disabled) return disabled;
  const body = await readBody(req);
  return json(createSandboxSignupMock(str(body, "email"), str(body, "display_name")));
}

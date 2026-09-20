// GET /v1/public/status — public connector-health feed (spec/16 §E).
// Unauthenticated, read-only; real backend caches 60s.

import { NextResponse } from "next/server";
import { publicStatusMock } from "@/lib/mock/launch";
import { mockDisabled } from "@/lib/mock/guard";

export const dynamic = "force-dynamic";

export async function GET(): Promise<NextResponse> {
  const disabled = mockDisabled();
  if (disabled) return disabled;
  return NextResponse.json(publicStatusMock(), {
    status: 200,
    headers: { "Cache-Control": "public, max-age=60" },
  });
}

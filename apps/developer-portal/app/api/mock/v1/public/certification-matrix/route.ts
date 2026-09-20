// GET /v1/public/certification-matrix — public per-connector certification
// verdicts + report hashes (spec/16 §E). Hashes are public; files are not.

import { NextResponse } from "next/server";
import { certificationMatrixMock } from "@/lib/mock/launch";
import { mockDisabled } from "@/lib/mock/guard";

export const dynamic = "force-dynamic";

export async function GET(): Promise<NextResponse> {
  const disabled = mockDisabled();
  if (disabled) return disabled;
  return NextResponse.json(certificationMatrixMock(), {
    status: 200,
    headers: { "Cache-Control": "public, max-age=60" },
  });
}

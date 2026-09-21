// GET /v1/sandbox/demo/fullflow — deterministic sandbox payment walkthrough
// used by Platform status (and the live-gateway shim). Without this route the
// catch-all returns "Unknown mock route" and Promise.all on /status blanks
// the whole page (connectors + certification stuck on Loading).

import { NextResponse } from "next/server";
import { sandboxDemoFullflowMock } from "@/lib/mock/launch";
import { mockDisabled } from "@/lib/mock/guard";

export const dynamic = "force-dynamic";

export async function GET(): Promise<NextResponse> {
  const disabled = mockDisabled();
  if (disabled) return disabled;
  return NextResponse.json(sandboxDemoFullflowMock(), {
    status: 200,
    headers: { "Cache-Control": "private, max-age=30" },
  });
}

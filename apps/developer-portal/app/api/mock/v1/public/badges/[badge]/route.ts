// GET /v1/public/badges/{connector_id}.svg — certification badge (spec/16 §E).
// Deterministic SVG; Cache-Control max-age=300; 404 for unknown connector_id.

import { NextResponse, type NextRequest } from "next/server";
import { badgeSvgMock } from "@/lib/mock/launch";
import { json, mockDisabled } from "@/lib/mock/launchHttp";
import { mockError } from "@/lib/mock/store";

export const dynamic = "force-dynamic";

export async function GET(_req: NextRequest, ctx: { params: Promise<{ badge: string }> }): Promise<NextResponse> {
  const disabled = mockDisabled();
  if (disabled) return disabled;
  const { badge } = await ctx.params;
  const connectorId = badge.replace(/\.svg$/, "");
  const svg = badgeSvgMock(connectorId);
  if (svg === null) {
    return json(mockError("not_found", "connector_not_found", "No such connector."));
  }
  return new NextResponse(svg, {
    status: 200,
    headers: {
      "Content-Type": "image/svg+xml",
      "Cache-Control": "public, max-age=300",
    },
  });
}

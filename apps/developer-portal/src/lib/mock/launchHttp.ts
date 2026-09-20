// Shared HTTP plumbing for the spec/16 mock routes (app/api/mock/v1/...).
// Mirrors the conventions of app/api/mock/v1/[...path]/route.ts: merchant routes
// require the session cookie; merchant mutations require the X-BDPay-TOTP
// step-up header and an Idempotency-Key. Public routes require neither.

import { NextResponse, type NextRequest } from "next/server";
import type { ErrorType } from "@/lib/api/types";
import { mockError, type MockResult } from "./store";
import { readPortalSessionPersona } from "./session";
import { verifyMockTotp, type MockTotpResult } from "@bdpay/edge/mock/totp";

// SEC-01: re-exported so the spec/16 v1 mock routes can pull the production
// guard from the same module they already import their HTTP helpers from.
export { mockDisabled } from "./guard";

export function json<T>(result: MockResult<T>): NextResponse {
  return NextResponse.json(result.body, { status: result.status });
}

/** Conventions §4 error envelope as a NextResponse (route-level refusals). */
export function mockErrorResponse(type: ErrorType, code: string, message: string): NextResponse {
  return json(mockError(type, code, message));
}

function totpFailure(result: Exclude<MockTotpResult, "ok">): NextResponse {
  if (result === "secret_missing") {
    return json(mockError("internal", "mock_totp_secret_missing", "Mock TOTP secret is not configured."));
  }
  if (result === "secret_invalid") {
    return json(mockError("internal", "mock_totp_secret_invalid", "Mock TOTP secret is invalid."));
  }
  return json(mockError("authentication", "totp_step_up_required", "Mutation requires a valid TOTP step-up code."));
}

/** Returns an error response when no signed merchant session is present, else null. */
export async function requireSession(req: NextRequest): Promise<NextResponse | null> {
  const persona = await readPortalSessionPersona(req);
  if (persona === null) {
    return json(mockError("authentication", "session_required", "Sign in to use the developer portal."));
  }
  return null;
}

/** TOTP step-up + Idempotency-Key checks for merchant mutations. */
export function requireMutationHeaders(req: NextRequest): NextResponse | null {
  const totp = verifyMockTotp(req.headers.get("x-bdpay-totp") ?? "");
  if (totp !== "ok") return totpFailure(totp);
  if (!req.headers.get("idempotency-key")) {
    return json(mockError("invalid_request", "idempotency_key_required", "Mutations require an Idempotency-Key header."));
  }
  return null;
}

export async function readBody(req: NextRequest): Promise<Record<string, unknown>> {
  try {
    return (await req.json()) as Record<string, unknown>;
  } catch {
    return {};
  }
}

export function str(body: Record<string, unknown>, key: string): string {
  return typeof body[key] === "string" ? (body[key] as string) : "";
}

export function strOrNull(body: Record<string, unknown>, key: string): string | null {
  return typeof body[key] === "string" ? (body[key] as string) : null;
}

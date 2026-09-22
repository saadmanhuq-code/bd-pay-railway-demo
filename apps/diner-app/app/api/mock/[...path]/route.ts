// Single dispatcher for the in-app deterministic mock server (the
// developer-portal/ops-console pattern). All paths mirror the spec/18 +
// spec/13 consumer API surface under /api/mock/v1/...
//
// Auth (D1, errata S18-E18): BD mobile + server-verified mock OTP; session via
// an HMAC-signed httpOnly cookie standing in for the platform customer JWT.
// Mutations require an Idempotency-Key header — except the auth POSTs (errata
// E31 exemption: token-issuing endpoints are idempotency-exempt).

import { NextRequest, NextResponse } from "next/server";
import type { DinerSession, Paginated, PaymentIntentView } from "@/lib/api/types";
import {
  confirmIntentMock,
  createOfferIntentMock,
  eligibleOffersMock,
  getDemoCustomer,
  getIntentMock,
  issueDynamicQrMock,
  listMerchantsMock,
  listMyIntentsMock,
  mockError,
  otpChallenge,
  verifyOtpChallenge,
  type MockResult,
} from "@/lib/mock/store";
import { mockDisabled } from "@/lib/mock/guard";
import {
  DINER_SESSION_COOKIE,
  clearDinerSessionCookieOptions,
  dinerSessionCookieOptions,
  hasValidDinerSession,
  issueDinerSession,
} from "@/lib/mock/session";

export const dynamic = "force-dynamic";

const SESSION_COOKIE = DINER_SESSION_COOKIE;
const BD_MOBILE_RE = /^(?:\+?880|0)?1[3-9]\d{8}$/;
const BN_DIGITS = "০১২৩৪৫৬৭৮৯";

function normalizeDigits(input: string): string {
  let out = "";
  for (const ch of input) {
    const idx = BN_DIGITS.indexOf(ch);
    out += idx >= 0 ? String(idx) : ch;
  }
  return out;
}

function json<T>(result: MockResult<T>): NextResponse {
  return NextResponse.json(result.body, { status: result.status });
}

function page<T>(rows: T[]): NextResponse {
  const body: Paginated<T> = { data: rows, next_cursor: null };
  return NextResponse.json(body, { status: 200 });
}

async function hasSession(req: NextRequest): Promise<boolean> {
  return hasValidDinerSession(req);
}

function unauthenticated(): NextResponse {
  return json(mockError("authentication", "session_required", "Sign in to use the diner app."));
}

interface Ctx {
  params: Promise<{ path: string[] }>;
}

// ---------------------------------------------------------------------------
// GET
// ---------------------------------------------------------------------------

export async function GET(req: NextRequest, ctx: Ctx): Promise<NextResponse> {
  const disabled = mockDisabled();
  if (disabled) return disabled;
  const { path: p } = await ctx.params;
  const q = req.nextUrl.searchParams;
  if (p[0] !== "v1") return json(mockError("not_found", "unknown_route", "Unknown mock route."));

  if (p[1] === "auth" && p[2] === "diner" && p[3] === "session") {
    if (!(await hasSession(req))) return unauthenticated();
    const issued = Date.now();
    const body: DinerSession = {
      customer: getDemoCustomer(),
      issued_at: new Date(issued).toISOString().replace(".000Z", "Z"),
      // Match cookie TTL (12h) — do not advertise the frozen June SNAPSHOT expiry.
      absolute_expires_at: new Date(issued + 12 * 3_600_000).toISOString().replace(".000Z", "Z"),
    };
    return NextResponse.json(body, { status: 200 });
  }

  // Public discovery (EatClub-style): merchant directory + eligible offers
  // are browseable without a session. Reservation/pay remain session-gated.
  if (p[1] === "diner" && p[2] === "merchants" && p.length === 3) {
    return page(listMerchantsMock({ area: q.get("area") ?? "", q: q.get("q") ?? "" }));
  }

  if (p[1] === "offers" && p[2] === "eligible" && p.length === 3) {
    const merchantId = q.get("merchant_id") ?? "";
    if (merchantId === "") {
      return json(
        mockError("invalid_request", "validation_failed", "merchant_id query parameter is required."),
      );
    }
    const exampleRaw = normalizeDigits(q.get("example_amount_minor") ?? "").trim();
    let example: number | null = null;
    if (exampleRaw !== "") {
      if (!/^\d+$/.test(exampleRaw)) {
        return json(
          mockError("invalid_request", "validation_failed", "example_amount_minor must be integer paisa."),
        );
      }
      example = Number(exampleRaw);
    }
    return json(
      eligibleOffersMock({
        merchant_id: merchantId,
        method: q.get("method") ?? "",
        example_amount_minor: example,
        at: q.get("at") ?? "",
      }),
    );
  }

  // Authenticated diner surface (history, intents, QR) requires a session.
  if (!(await hasSession(req))) return unauthenticated();

  if (p[1] === "payment-intents") {
    if (p.length === 2) {
      return page<PaymentIntentView>(listMyIntentsMock());
    }
    if (p.length === 3) {
      return json(getIntentMock(p[2] ?? ""));
    }
  }

  return json(mockError("not_found", "unknown_route", "Unknown mock route."));
}

// ---------------------------------------------------------------------------
// POST
// ---------------------------------------------------------------------------

export async function POST(req: NextRequest, ctx: Ctx): Promise<NextResponse> {
  const disabled = mockDisabled();
  if (disabled) return disabled;
  const { path: p } = await ctx.params;
  if (p[0] !== "v1") return json(mockError("not_found", "unknown_route", "Unknown mock route."));

  let body: Record<string, unknown> = {};
  try {
    body = (await req.json()) as Record<string, unknown>;
  } catch {
    body = {};
  }
  const str = (key: string): string => (typeof body[key] === "string" ? (body[key] as string) : "");

  // --- auth (idempotency-exempt POSTs, E31) --------------------------------
  if (p[1] === "auth" && p[2] === "diner") {
    if (p[3] === "otp" && p[4] === "request") {
      const phone = normalizeDigits(str("phone")).replace(/[\s-]/g, "");
      if (!BD_MOBILE_RE.test(phone)) {
        return json(
          mockError(
            "invalid_request",
            "phone_invalid",
            "A Bangladeshi mobile number (01XXXXXXXXX) is required.",
          ),
        );
      }
      return json(otpChallenge(phone));
    }
    if (p[3] === "otp" && p[4] === "verify") {
      const phone = normalizeDigits(str("phone")).replace(/[\s-]/g, "");
      if (!BD_MOBILE_RE.test(phone)) {
        return json(
          mockError("invalid_request", "phone_invalid", "A Bangladeshi mobile number is required."),
        );
      }
      const verified = verifyOtpChallenge(phone, normalizeDigits(str("otp_code")));
      if (verified.status !== 200) return json(verified);
      const customer = getDemoCustomer();
      const token = await issueDinerSession(customer.customer_id);
      if (token === null) {
        return json(
          mockError(
            "internal",
            "mock_session_secret_missing",
            "Mock session signing secret is not configured.",
          ),
        );
      }
      const res = NextResponse.json({ customer }, { status: 200 });
      res.cookies.set(SESSION_COOKIE, token, dinerSessionCookieOptions());
      return res;
    }
    if (p[3] === "logout") {
      const res = NextResponse.json({ ok: true }, { status: 200 });
      res.cookies.set(SESSION_COOKIE, "", clearDinerSessionCookieOptions());
      return res;
    }
    return json(mockError("not_found", "unknown_route", "Unknown mock route."));
  }

  // Mutations require an Idempotency-Key (conventions §4).
  const idem = req.headers.get("idempotency-key") ?? "";
  if (idem === "") {
    return json(
      mockError("invalid_request", "idempotency_key_required", "Mutations require an Idempotency-Key header."),
    );
  }

  if (p[1] === "payment-intents" && p.length === 2) {
    // D1 is enforced INSIDE the reservation path: a guest with offer_id gets
    // 401 offer_requires_login (not the generic session refusal).
    const offerId = typeof body["offer_id"] === "string" ? (body["offer_id"] as string) : null;
    return json(
      createOfferIntentMock({
        authenticated: await hasSession(req),
        idempotencyKey: idem,
        merchant_id: str("merchant_id"),
        offer_id: offerId,
        gross_amount_minor: body["gross_amount_minor"],
        method: str("method"),
      }),
    );
  }

  if (!(await hasSession(req))) return unauthenticated();

  if (p[1] === "payment-intents" && p.length === 4 && p[3] === "confirm") {
    return json(confirmIntentMock(p[2] ?? ""));
  }

  if (p[1] === "qr-codes" && p[2] === "dynamic" && p.length === 3) {
    return json(issueDynamicQrMock(str("payment_intent_id")));
  }

  return json(mockError("not_found", "unknown_route", "Unknown mock route."));
}

import { createHmac } from "node:crypto";
import { NextRequest, NextResponse } from "next/server";
import { copyHeaders, demoApiKey, gatewayUrl } from "@bdpay/edge/live-proxy-core";
import { error, proxyGateway } from "@bdpay/edge/live-proxy";
import type { Operator, Session } from "@/lib/api/types";
import {
  OPS_SESSION_COOKIE,
  OPS_SESSION_TTL_SECONDS,
  clearOpsSessionCookieOptions,
  hasValidOpsSession,
  issueOpsSession,
  opsSessionCookieOptions,
} from "@/lib/mock/session";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

const LIVE_OPERATOR_TOKEN_COOKIE = "bdpay_live_ops_token";

interface Ctx {
  params: Promise<{ path: string[] }>;
}

function base32Decode(value: string): Buffer | null {
  const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";
  const normalized = value.toUpperCase().replace(/=+$/g, "").replace(/\s+/g, "");
  let bits = 0;
  let bitCount = 0;
  const out: number[] = [];
  for (const ch of normalized) {
    const index = alphabet.indexOf(ch);
    if (index < 0) return null;
    bits = (bits << 5) | index;
    bitCount += 5;
    if (bitCount >= 8) {
      out.push((bits >>> (bitCount - 8)) & 0xff);
      bitCount -= 8;
    }
  }
  return Buffer.from(out);
}

function totpCode(secret: string, at = Date.now()): string | null {
  const key = base32Decode(secret);
  if (key === null) return null;
  const counter = Math.floor(at / 1000 / 30);
  const msg = Buffer.alloc(8);
  msg.writeBigUInt64BE(BigInt(counter));
  const digest = createHmac("sha1", key).update(msg).digest();
  const last = digest.at(-1);
  if (last === undefined) return null;
  const offset = last & 0x0f;
  const code = (digest.readUInt32BE(offset) & 0x7fffffff) % 1_000_000;
  return code.toString().padStart(6, "0");
}

function constantTimeEqual(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i += 1) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

function validOpsShim(email: string, password: string, totp: string): boolean {
  const expectedEmail = process.env.BDPAY_LIVE_OPS_EMAIL || "";
  const expectedPassword = process.env.BDPAY_LIVE_OPS_PASSWORD || "";
  const secret = process.env.BDPAY_LIVE_OPS_TOTP_SECRET || "";
  if (!expectedEmail || !expectedPassword || !secret) return false;
  if (email !== expectedEmail || password !== expectedPassword) return false;
  const now = Date.now();
  return [-30_000, 0, 30_000].some((offset) => {
    const expected = totpCode(secret, now + offset);
    return expected !== null && constantTimeEqual(expected, totp);
  });
}

function demoOperator(operatorId: string, roles: string[] = ["PAYMENT_OPS"]): Operator {
  const now = new Date().toISOString();
  return {
    operator_id: operatorId.startsWith("oper_") ? operatorId : "oper_live_demo",
    email: operatorId.includes("@") ? operatorId : "you@bdpay.example",
    display_name: "BDPay Demo Operator",
    roles: roles.includes("READ_ONLY") ? ["auditor"] : ["operator"],
    acl_denies: [],
    credential_state: "ACTIVE",
    failed_attempts: 0,
    locked_until: null,
    password_set_at: now,
    created_at: now,
    updated_at: now,
    schema_version: 1,
  };
}

function sessionBody(operatorId = "you@bdpay.example", expiresAt?: string, roles?: string[]): Session {
  return {
    operator: demoOperator(operatorId, roles),
    issued_at: new Date().toISOString(),
    absolute_expires_at: expiresAt || new Date(Date.now() + OPS_SESSION_TTL_SECONDS * 1000).toISOString(),
  };
}

export async function GET(req: NextRequest, ctx: Ctx): Promise<NextResponse> {
  const { path: p } = await ctx.params;
  if (p[0] !== "v1") return error(404, "unknown_route", "Unknown live route.");

  if (p[1] === "auth" && p[2] === "operator" && p[3] === "session") {
    if (!(await hasValidOpsSession(req))) return error(401, "session_required", "Sign in to use the ops console.");
    return NextResponse.json(sessionBody(), { status: 200 });
  }

  if (!(await hasValidOpsSession(req))) {
    return error(401, "session_required", "Sign in to use the ops console.");
  }
  const token = req.cookies.get(LIVE_OPERATOR_TOKEN_COOKIE)?.value || demoApiKey();
  if (!token) return error(500, "live_demo_api_key_missing", "Live demo API key is not configured.");
  return proxyGateway(req, p, { token });
}

export async function POST(req: NextRequest, ctx: Ctx): Promise<NextResponse> {
  const { path: p } = await ctx.params;
  if (p[0] !== "v1") return error(404, "unknown_route", "Unknown live route.");

  if (p[1] === "auth" && p[2] === "operator" && p[3] === "logout") {
    const res = NextResponse.json({ ok: true }, { status: 200 });
    res.cookies.set(OPS_SESSION_COOKIE, "", clearOpsSessionCookieOptions());
    res.cookies.set(LIVE_OPERATOR_TOKEN_COOKIE, "", {
      httpOnly: true,
      sameSite: "lax",
      path: "/",
      maxAge: 0,
      secure: process.env.NODE_ENV === "production",
    });
    return res;
  }

  if (p[1] !== "auth" || p[2] !== "operator" || p[3] !== "login") {
    return error(404, "unknown_route", "Unknown live route.");
  }

  let body: Record<string, unknown> = {};
  try {
    body = (await req.json()) as Record<string, unknown>;
  } catch {
    body = {};
  }
  const email = typeof body.email === "string" ? body.email : "";
  const password = typeof body.password === "string" ? body.password : "";
  const totpCode = typeof body.totp_code === "string" ? body.totp_code : "";
  const url = gatewayUrl(["v1", "auth", "operator", "totp", "verify"], "");
  if (url === null) return error(500, "live_gateway_base_missing", "Live gateway base URL is not configured.");

  const upstream = await fetch(url, {
    method: "POST",
    cache: "no-store",
    headers: {
      "Content-Type": "application/json",
      Accept: "application/json",
      Authorization: `Bearer ${password}`,
    },
    body: JSON.stringify({ operator_id: email, totp_code: totpCode }),
  });
  const payload = (await upstream.json().catch(() => null)) as
    | { session_token?: string; operator_id?: string; roles?: string[]; expires_at?: string }
    | null;
  if (!upstream.ok || !payload?.session_token) {
    if (validOpsShim(email, password, totpCode)) {
      const token = await issueOpsSession("oper_live_demo");
      if (token === null) {
        return error(500, "session_secret_missing", "Session signing secret is not configured.");
      }
      const res = NextResponse.json({ operator: demoOperator(email, ["PAYMENT_OPS"]) }, { status: 200 });
      res.cookies.set(OPS_SESSION_COOKIE, token, opsSessionCookieOptions());
      res.cookies.set(LIVE_OPERATOR_TOKEN_COOKIE, "", {
        httpOnly: true,
        sameSite: "lax",
        path: "/",
        maxAge: 0,
        secure: process.env.NODE_ENV === "production",
      });
      return res;
    }
    return NextResponse.json(payload ?? { error: { code: "operator_login_failed" } }, {
      status: upstream.status,
      headers: copyHeaders(upstream),
    });
  }

  const token = await issueOpsSession("oper_live_demo");
  if (token === null) {
    return error(500, "session_secret_missing", "Session signing secret is not configured.");
  }
  const res = NextResponse.json(
    { operator: demoOperator(payload.operator_id || email, payload.roles || []) },
    { status: 200 },
  );
  res.cookies.set(OPS_SESSION_COOKIE, token, opsSessionCookieOptions());
  res.cookies.set(LIVE_OPERATOR_TOKEN_COOKIE, payload.session_token, {
    httpOnly: true,
    sameSite: "lax",
    path: "/",
    maxAge: OPS_SESSION_TTL_SECONDS,
    secure: process.env.NODE_ENV === "production",
  });
  return res;
}

"use client";

// Diner login (decision D1): offer redemption REQUIRES an authenticated
// diner — guests cannot redeem, and per-customer caps stay enforceable.
// BD mobile + OTP, Bengali-digit tolerant (errata S18-E18: the dev-mock
// session stands in for the platform customer JWT at activation).

import React, { useState } from "react";
import { useRouter } from "next/navigation";
import { ApiError, requestOtp, verifyOtp, USING_MOCK_API } from "@/lib/api/client";
import { normalizeBdMobile } from "@/lib/format";
import { useLang } from "@/lib/i18n/LangContext";
import { DualLabel } from "@/lib/i18n/DualLabel";
import { Shell } from "@/components/Shell";


function safeNext(raw: string | null): string {
  if (!raw) return "/";
  // Only allow same-origin relative paths (no protocol-relative / open redirects).
  if (!raw.startsWith("/") || raw.startsWith("//")) return "/";
  return raw;
}

export default function LoginPage() {
  const { t } = useLang();
  const router = useRouter();

  const [phoneRaw, setPhoneRaw] = useState("");
  const [step, setStep] = useState<"phone" | "otp">("phone");
  const [otp, setOtp] = useState("");
  const [debugOtpCode, setDebugOtpCode] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const phone = normalizeBdMobile(phoneRaw);

  async function onSendOtp() {
    if (phone === null) {
      setError(t("login_phone_invalid"));
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const challenge = await requestOtp(phone);
      setDebugOtpCode(challenge.debug_otp_code ?? null);
      setStep("otp");
    } catch (err) {
      setError(err instanceof ApiError ? t("login_phone_invalid") : t("error_generic"));
    } finally {
      setBusy(false);
    }
  }

  async function onVerify() {
    if (phone === null) return;
    setBusy(true);
    setError(null);
    try {
      await verifyOtp(phone, otp.trim());
      setDebugOtpCode(null);
      router.replace(safeNext(new URLSearchParams(window.location.search).get("next")));
    } catch (err) {
      if (err instanceof ApiError && err.code === "otp_invalid") {
        setError(t("login_otp_invalid"));
      } else {
        setError(t("error_generic"));
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <Shell session={null}>
      <section className="panel">
        <h1>{t("login_title")}</h1>
        <p className="subtle">{t("tagline")}</p>
        <p className="notice">
          <DualLabel k="login_d1_notice" />
        </p>
        {USING_MOCK_API || process.env.NODE_ENV !== "production" ? (
          <p className="notice" role="note">
            Mock demo (not live SMS): use a BD mobile (e.g. 01712345678); after Send
            code, enter the on-screen debug OTP shown below.
          </p>
        ) : null}

        {step === "phone" ? (
          <>
            <label className="field">
              <span>{t("login_phone")}</span>
              <input
                value={phoneRaw}
                onChange={(e) => setPhoneRaw(e.target.value)}
                inputMode="tel"
                autoComplete="tel"
              />
            </label>
            <p className="hint">{t("login_phone_hint")}</p>
            {error ? <p className="error-text">{error}</p> : null}
            <button
              className="btn btn-primary btn-block"
              disabled={busy || phoneRaw.trim() === ""}
              onClick={() => void onSendOtp()}
            >
              {t("login_send_otp")}
            </button>
          </>
        ) : (
          <>
            <p className="notice">{t("login_otp_sent")}</p>
            {debugOtpCode ? (
              <p className="hint">
                {t("login_demo_otp")}: <span className="mono">{debugOtpCode}</span>
              </p>
            ) : null}
            <label className="field">
              <span>{t("login_otp")}</span>
              <input
                value={otp}
                onChange={(e) => setOtp(e.target.value)}
                inputMode="numeric"
                maxLength={6}
                autoComplete="one-time-code"
              />
            </label>
            {error ? <p className="error-text">{error}</p> : null}
            <button
              className="btn btn-primary btn-block"
              disabled={busy || otp.trim().length !== 6}
              onClick={() => void onVerify()}
            >
              {t("login_verify")}
            </button>
            <button className="btn btn-block" disabled={busy} onClick={() => setStep("phone")}>
              {t("login_change_phone")}
            </button>
          </>
        )}
      </section>
    </Shell>
  );
}

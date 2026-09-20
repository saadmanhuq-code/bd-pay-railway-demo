"use client";

// Public self-serve sandbox signup (spec/16 §F — SANDBOX_PUBLIC only).
// email + display name → POST /v1/sandbox/signups → email OTP → verify →
// bdpk_test_ key shown ONCE + sandbox base URL + docs link.
// Failure paths: wrong OTP (attempts remaining), REVOKED, EXPIRED.

import React, { useState } from "react";
import Link from "next/link";
import { ApiError, createSandboxSignup, verifySandboxSignup } from "@/lib/api/client";
import type { SandboxSignupVerified } from "@/lib/api/launchTypes";
import { PublicShell } from "@/components/PublicShell";
import { SecretOnce } from "@/components/SecretOnce";
import { useLang } from "@/lib/i18n/LangContext";

type Step =
  | { kind: "form" }
  | { kind: "otp"; signupId: string; debugOtpCode: string | null }
  | { kind: "success"; result: SandboxSignupVerified }
  | { kind: "revoked" }
  | { kind: "expired" };

export default function SandboxSignupPage() {
  const { t } = useLang();
  const [step, setStep] = useState<Step>({ kind: "form" });
  const [email, setEmail] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [otp, setOtp] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [secretVisible, setSecretVisible] = useState(true);

  async function onSignup(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const created = await createSandboxSignup(email, displayName);
      setStep({ kind: "otp", signupId: created.sandbox_signup_id, debugOtpCode: created.debug_otp_code ?? null });
    } catch (err) {
      setError(err instanceof ApiError ? err.message : t("error_generic"));
    } finally {
      setBusy(false);
    }
  }

  async function onVerify(e: React.FormEvent) {
    e.preventDefault();
    if (step.kind !== "otp") return;
    setBusy(true);
    setError(null);
    try {
      const result = await verifySandboxSignup(step.signupId, otp);
      setSecretVisible(true);
      setStep({ kind: "success", result });
    } catch (err) {
      if (err instanceof ApiError && err.envelope?.error.code === "signup_revoked") {
        setStep({ kind: "revoked" });
      } else if (err instanceof ApiError && err.envelope?.error.code === "signup_expired") {
        setStep({ kind: "expired" });
      } else {
        // otp_invalid carries the server-authoritative attempts-remaining count.
        setError(err instanceof ApiError ? err.message : t("error_generic"));
      }
      setOtp("");
    } finally {
      setBusy(false);
    }
  }

  function restart() {
    setStep({ kind: "form" });
    setOtp("");
    setError(null);
  }

  return (
    <PublicShell>
      <div className="checkout-wrap">
        <h1>{t("sbx_signup_title")}</h1>
        <p className="subtle">{t("sbx_signup_intro")}</p>

        {step.kind === "form" ? (
          <section className="panel">
            <form onSubmit={(e) => void onSignup(e)}>
              <label className="field">
                <span>{t("sbx_email")}</span>
                <input type="email" value={email} onChange={(e) => setEmail(e.target.value)} autoComplete="email" required />
              </label>
              <label className="field">
                <span>{t("sbx_display_name")}</span>
                <input value={displayName} onChange={(e) => setDisplayName(e.target.value)} required />
              </label>
              {error ? <p className="error-text">{error}</p> : null}
              <button className="btn btn-primary" type="submit" disabled={busy}>
                {t("sbx_send_otp")}
              </button>
            </form>
          </section>
        ) : null}

        {step.kind === "otp" ? (
          <section className="panel">
            <p className="notice">{t("sbx_otp_sent")}</p>
            {step.debugOtpCode ? (
              <p className="hint">
                {t("sbx_demo_otp")}: <span className="mono">{step.debugOtpCode}</span>
              </p>
            ) : null}
            <form onSubmit={(e) => void onVerify(e)}>
              <label className="field">
                <span>{t("sbx_otp_label")}</span>
                <input
                  value={otp}
                  onChange={(e) => setOtp(e.target.value.replace(/[^0-9]/g, "").slice(0, 6))}
                  inputMode="numeric"
                  autoComplete="one-time-code"
                  className="totp-input"
                  required
                />
              </label>
              {error ? <p className="error-text">{error}</p> : null}
              <button className="btn btn-primary" type="submit" disabled={busy || otp.length !== 6}>
                {t("sbx_verify")}
              </button>
            </form>
          </section>
        ) : null}

        {step.kind === "success" ? (
          <>
            <section className="panel">
              <h2>{t("sbx_success_title")}</h2>
              {secretVisible ? (
                <SecretOnce
                  label={`${t("sbx_success_title")} (${step.result.key_prefix}…)`}
                  secret={step.result.api_key}
                  onDismiss={() => setSecretVisible(false)}
                />
              ) : null}
              <dl className="kv">
                <dt>{t("sbx_base_url")}</dt>
                <dd className="mono">{step.result.sandbox_base_url}</dd>
                <dt>{t("sbx_docs_link")}</dt>
                <dd>
                  <Link href={step.result.docs_url}>{step.result.docs_url}</Link>
                </dd>
                <dt>Merchant</dt>
                <dd className="mono">{step.result.merchant_id}</dd>
              </dl>
            </section>
          </>
        ) : null}

        {step.kind === "revoked" ? (
          <section className="panel">
            <p className="notice notice-danger">{t("sbx_revoked")}</p>
            <button className="btn" onClick={restart}>
              {t("sbx_signup_title")}
            </button>
          </section>
        ) : null}

        {step.kind === "expired" ? (
          <section className="panel">
            <p className="notice notice-warn">{t("sbx_expired")}</p>
            <button className="btn" onClick={restart}>
              {t("sbx_signup_title")}
            </button>
          </section>
        ) : null}
      </div>
    </PublicShell>
  );
}

"use client";

import React, { useState } from "react";
import { useRouter } from "next/navigation";
import { ApiError, login, USING_MOCK_API } from "@/lib/api/client";
import { useLang } from "@/lib/i18n/LangContext";
import { DualLabel } from "@/lib/i18n/DualLabel";

export default function LoginPage() {
  const { t, lang, setLang } = useLang();
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [totp, setTotp] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [lockedUntil, setLockedUntil] = useState<string | null>(null);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await login(email, password, totp);
      router.replace("/");
    } catch (err) {
      if (err instanceof ApiError) {
        setError(err.message);
        const code = err.envelope?.error.code;
        if (code === "account_locked") setLockedUntil("30m");
      } else {
        setError(t("error_generic"));
      }
      setBusy(false);
    }
  }

  return (
    <main className="centered">
      <form className="login-card" onSubmit={onSubmit}>
        <h1>
          <DualLabel k="login_title" />
        </h1>
        {USING_MOCK_API || process.env.NODE_ENV !== "production" ? (
          <p className="notice" role="note">
            Mock demo (not live money): any email + any password + TOTP for secret{" "}
            <code>JBSWY3DPEHPK3PXP</code> (e.g. use an authenticator app, or any
            current 6-digit code for that secret).
          </p>
        ) : null}
        <label className="field">
          <span>{t("login_email")}</span>
          <input type="email" value={email} onChange={(e) => setEmail(e.target.value)} aria-label={t("login_email")} required />
        </label>
        <label className="field">
          <span>{t("login_password")}</span>
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            aria-label={t("login_password")}
            required
          />
        </label>
        <label className="field">
          <span>{t("login_totp")}</span>
          <input
            value={totp}
            onChange={(e) => setTotp(e.target.value.replace(/[^0-9]/g, "").slice(0, 6))}
            inputMode="numeric"
            autoComplete="one-time-code"
            aria-label={t("login_totp")}
            className="totp-input"
            required
          />
        </label>
        {error ? <p className="error-text">{error}</p> : null}
        {lockedUntil ? (
          <p className="notice notice-danger">
            <DualLabel k="login_lockout" />
          </p>
        ) : null}
        <p className="notice">
          <DualLabel k="login_lockout" />
        </p>
        <div className="modal-actions">
          <button type="button" className="btn btn-small" onClick={() => setLang(lang === "en" ? "bn" : "en")}>
            {lang === "en" ? "বাংলা" : "English"}
          </button>
          <button type="submit" className="btn btn-primary" disabled={busy || totp.length !== 6}>
            {t("login_submit")}
          </button>
        </div>
      </form>
    </main>
  );
}

"use client";

import React, { useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { ApiError, login } from "@/lib/api/client";
import type { Persona } from "@/lib/api/types";
import { PERSONAS } from "@/lib/api/types";
import { useLang } from "@/lib/i18n/LangContext";

export default function LoginPage() {
  const { t } = useLang();
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [totp, setTotp] = useState("");
  const [persona, setPersona] = useState<Persona>("owner");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await login(email, password, totp, persona);
      router.replace("/");
    } catch (err) {
      setError(err instanceof ApiError ? err.message : t("error_generic"));
      setBusy(false);
    }
  }

  return (
    <main className="centered">
      <form className="login-card" onSubmit={onSubmit}>
        <h1>{t("login_title")}</h1>
        {process.env.NODE_ENV !== "production" ? (
          <p className="notice" role="note">
            Mock demo (not live money): any email + any password + TOTP for secret{" "}
            <code>JBSWY3DPEHPK3PXP</code>; pick a persona. See DEMO.md.
          </p>
        ) : null}
        <label className="field">
          <span>{t("login_email")}</span>
          <input type="email" value={email} onChange={(e) => setEmail(e.target.value)} autoComplete="email" required />
        </label>
        <label className="field">
          <span>{t("login_password")}</span>
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete="current-password"
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
            className="totp-input"
            required
          />
        </label>
        <label className="field">
          <span>{t("login_persona")}</span>
          <select value={persona} onChange={(e) => setPersona(e.target.value as Persona)}>
            {PERSONAS.map((p) => (
              <option key={p} value={p}>
                {p}
              </option>
            ))}
          </select>
        </label>
        <p className="subtle">{t("login_lockout")}</p>
        {error ? <p className="error-text">{error}</p> : null}
        <button className="btn btn-primary" type="submit" disabled={busy || totp.length !== 6}>
          {t("login_submit")}
        </button>
        <p className="subtle">
          New merchant? <Link href="/signup">{t("signup_title")}</Link>
        </p>
      </form>
    </main>
  );
}

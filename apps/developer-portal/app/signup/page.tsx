"use client";

// Merchant self-onboarding — business profile form (spec/08). Bengali
// numerals are normalized to 0-9 client-side as a convenience; the server
// re-normalizes and is authoritative.

import React, { useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { ApiError, signup } from "@/lib/api/client";
import { normalizeBengaliDigits } from "@/lib/format";
import { useLang } from "@/lib/i18n/LangContext";

export default function SignupPage() {
  const { t } = useLang();
  const router = useRouter();
  const [legalName, setLegalName] = useState("");
  const [tradeName, setTradeName] = useState("");
  const [tradeNameBn, setTradeNameBn] = useState("");
  const [bin, setBin] = useState("");
  const [email, setEmail] = useState("");
  const [phone, setPhone] = useState("");
  const [mcc, setMcc] = useState("5411");
  const [city, setCity] = useState("Dhaka");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const result = await signup({
        legal_name: legalName,
        trade_name: tradeName,
        trade_name_bn: tradeNameBn,
        bin_number: normalizeBengaliDigits(bin),
        contact_email: email,
        contact_phone: normalizeBengaliDigits(phone),
        mcc: normalizeBengaliDigits(mcc),
        city,
      });
      router.replace(`/onboarding?application_id=${encodeURIComponent(result.application_id)}`);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : t("error_generic"));
      setBusy(false);
    }
  }

  return (
    <main className="centered">
      <form className="login-card" onSubmit={onSubmit}>
        <h1>{t("signup_title")}</h1>
        <label className="field">
          <span>Legal name</span>
          <input value={legalName} onChange={(e) => setLegalName(e.target.value)} required />
        </label>
        <label className="field">
          <span>Trade name (EN)</span>
          <input value={tradeName} onChange={(e) => setTradeName(e.target.value)} required />
        </label>
        <label className="field">
          <span lang="bn">ব্যবসার নাম (বাংলা)</span>
          <input value={tradeNameBn} onChange={(e) => setTradeNameBn(e.target.value)} lang="bn" />
        </label>
        <label className="field">
          <span>BIN number</span>
          <input value={bin} onChange={(e) => setBin(e.target.value)} inputMode="numeric" required />
        </label>
        <label className="field">
          <span>Contact email</span>
          <input type="email" value={email} onChange={(e) => setEmail(e.target.value)} required />
        </label>
        <label className="field">
          <span>Contact phone</span>
          <input value={phone} onChange={(e) => setPhone(e.target.value)} inputMode="tel" required />
        </label>
        <label className="field">
          <span>MCC</span>
          <input value={mcc} onChange={(e) => setMcc(e.target.value)} inputMode="numeric" required />
        </label>
        <label className="field">
          <span>City</span>
          <input value={city} onChange={(e) => setCity(e.target.value)} required />
        </label>
        <p className="subtle">{t("bn_numeral_note")}</p>
        {error ? <p className="error-text">{error}</p> : null}
        <button className="btn btn-primary" type="submit" disabled={busy}>
          {t("signup_submit")}
        </button>
        <p className="subtle">
          Already activated? <Link href="/login">{t("login_title")}</Link>
        </p>
      </form>
    </main>
  );
}

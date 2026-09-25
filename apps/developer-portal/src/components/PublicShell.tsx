"use client";

// PublicShell — minimal chrome for the unauthenticated surfaces (spec/16):
// public status page, hosted checkout, sandbox signup. Brand + language
// toggle only; no session, no env toggle, no sidebar.

import React from "react";
import { useLang } from "@/lib/i18n/LangContext";
import type { CopyKey } from "@/lib/i18n/copy";

// brandKey: payer-facing surfaces (hosted checkout) show "BD-PAY Checkout"
// rather than the merchant-developer portal name.
export function PublicShell({ children, brandKey = "appName" }: { children: React.ReactNode; brandKey?: CopyKey }) {
  const { lang, setLang, t } = useLang();

  return (
    <div className="portal">
      <header className="topbar">
        <div className="topbar-brand">{t(brandKey)}</div>
        <div className="topbar-actions">
          <button className="btn btn-small" onClick={() => setLang(lang === "en" ? "bn" : "en")} aria-label={t("language")}>
            {lang === "en" ? "বাংলা" : "English"}
          </button>
        </div>
      </header>
      <main className="public-content">{children}</main>
    </div>
  );
}

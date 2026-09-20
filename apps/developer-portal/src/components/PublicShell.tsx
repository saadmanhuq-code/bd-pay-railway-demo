"use client";

// PublicShell — minimal chrome for the unauthenticated surfaces (spec/16):
// public status page, hosted checkout, sandbox signup. Brand + language
// toggle only; no session, no env toggle, no sidebar.

import React from "react";
import { useLang } from "@/lib/i18n/LangContext";

export function PublicShell({ children }: { children: React.ReactNode }) {
  const { lang, setLang, t } = useLang();

  return (
    <div className="portal">
      <header className="topbar">
        <div className="topbar-brand">{t("appName")}</div>
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

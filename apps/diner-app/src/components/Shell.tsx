"use client";

// Consumer shell — Bengali-first mobile-leaning chrome: brand bar with
// language toggle + sign-out, sticky bottom navigation (browse / history).
// PWA-shell pattern per the talent-1-0 lift row (G5 row 7, gated at this
// build); offline queue and native push remain activation-scope.

import React from "react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { dinerLogout } from "@/lib/api/client";
import { useLang } from "@/lib/i18n/LangContext";
import type { DinerSession } from "@/lib/api/types";

export function Shell({
  session,
  children,
}: {
  session: DinerSession | null;
  children: React.ReactNode;
}) {
  const { lang, setLang, t } = useLang();
  const pathname = usePathname();
  const router = useRouter();

  async function onSignOut() {
    try {
      await dinerLogout();
    } finally {
      router.replace("/login");
    }
  }

  return (
    <div className="diner">
      <header className="topbar">
        <div className="topbar-brand">{t("appName")}</div>
        <div className="topbar-actions">
          {session ? <span className="topbar-user">{session.customer.phone_masked}</span> : null}
          <button
            className="btn btn-small"
            onClick={() => setLang(lang === "bn" ? "en" : "bn")}
            aria-label={t("language")}
          >
            {lang === "bn" ? "English" : "বাংলা"}
          </button>
          {session ? (
            <button className="btn btn-small" onClick={() => void onSignOut()}>
              {t("sign_out")}
            </button>
          ) : null}
        </div>
      </header>
      <main className="content">{children}</main>
      {session ? (
        <nav className="bottom-nav" aria-label="main">
          <Link href="/" className={`bottom-link ${pathname === "/" || pathname.startsWith("/merchants") ? "active" : ""}`}>
            {t("nav_browse")}
          </Link>
          <Link href="/history" className={`bottom-link ${pathname === "/history" ? "active" : ""}`}>
            {t("nav_history")}
          </Link>
        </nav>
      ) : null}
    </div>
  );
}

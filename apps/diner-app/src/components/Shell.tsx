"use client";

// Consumer shell — Bengali-first mobile chrome. Browse is always available;
// history + sign-out require a session. Guests see Sign in instead.

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
      router.replace("/");
    }
  }

  const browseActive = pathname === "/" || pathname.startsWith("/merchants");
  const historyActive = pathname === "/history";
  const cardActive = pathname === "/wallet" || pathname.startsWith("/wallet");
  const loginNext = pathname.startsWith("/merchants")
    ? `/login?next=${encodeURIComponent(pathname)}`
    : "/login?next=/";

  return (
    <div className="diner">
      <header className="topbar">
        <Link href="/" className="topbar-brand">
          {t("appName")}
        </Link>
        <div className="topbar-actions">
          {session ? (
            <span className="topbar-user">{session.customer.phone_masked}</span>
          ) : null}
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
          ) : (
            <Link className="btn btn-small btn-primary" href={loginNext}>
              {t("sign_in")}
            </Link>
          )}
        </div>
      </header>
      <main className="content">{children}</main>
      <footer className="osm-attribution" role="contentinfo">
        <a
          href="https://www.openstreetmap.org/copyright"
          target="_blank"
          rel="noopener noreferrer"
        >
          {t("osm_attribution")}
        </a>
      </footer>
      <nav className="bottom-nav" aria-label="main">
        <Link href="/" className={`bottom-link ${browseActive ? "active" : ""}`}>
          {t("nav_browse")}
        </Link>
        {session ? (
          <>
            <Link
              href="/wallet"
              className={`bottom-link ${cardActive ? "active" : ""}`}
            >
              {t("nav_card")}
            </Link>
            <Link
              href="/history"
              className={`bottom-link ${historyActive ? "active" : ""}`}
            >
              {t("nav_history")}
            </Link>
          </>
        ) : (
          <Link href={loginNext} className="bottom-link">
            {t("sign_in")}
          </Link>
        )}
      </nav>
    </div>
  );
}

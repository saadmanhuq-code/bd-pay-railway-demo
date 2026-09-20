"use client";

// Portal shell: top bar (env mode badge toggle, persona badge, language
// toggle, sign-out) + left nav.

import React from "react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { logout } from "@/lib/api/client";
import { useLang } from "@/lib/i18n/LangContext";
import type { CopyKey } from "@/lib/i18n/copy";
import { useMode } from "@/lib/mode";
import { useSession } from "@/lib/useSession";

const NAV: { href: string; key: CopyKey }[] = [
  { href: "/", key: "nav_dashboard" },
  { href: "/onboarding", key: "nav_onboarding" },
  { href: "/api-keys", key: "nav_api_keys" },
  { href: "/webhooks", key: "nav_webhooks" },
  { href: "/docs", key: "nav_docs" },
  { href: "/sandbox", key: "nav_sandbox" },
  { href: "/qr", key: "nav_qr" },
  { href: "/payment-links", key: "nav_payment_links" },
  { href: "/offers", key: "nav_offers" },
  { href: "/disputes", key: "nav_disputes" },
  { href: "/disbursements", key: "nav_disbursements" },
  { href: "/reports", key: "nav_reports" },
  { href: "/notifications", key: "nav_notifications" },
  { href: "/status", key: "nav_status" },
  { href: "/settings", key: "nav_settings" },
];

export function Shell({ children }: { children: React.ReactNode }) {
  const { lang, setLang, t } = useLang();
  const { env, setEnv } = useMode();
  const pathname = usePathname();
  const router = useRouter();
  const { session, loading } = useSession();

  async function onLogout() {
    try {
      await logout();
    } finally {
      router.replace("/login");
    }
  }

  if (loading || !session) {
    return <main className="centered">{t("loading")}</main>;
  }

  return (
    <div className="portal">
      <header className="topbar">
        <div className="topbar-brand">{t("appName")}</div>
        <div className="topbar-merchant">
          {session.merchant.trade_name} <span lang="bn">({session.merchant.trade_name_bn})</span>
        </div>
        <div className="topbar-actions">
          <button
            className={`mode-toggle ${env === "live" ? "mode-live" : "mode-sandbox"}`}
            onClick={() => setEnv(env === "live" ? "sandbox" : "live")}
            aria-label="Environment mode"
            title="Toggle live / sandbox data"
          >
            {env === "live" ? t("env_live") : t("env_sandbox")}
          </button>
          <span className="badge badge-persona" title={t("persona")}>
            {session.member.persona}
          </span>
          <button className="btn btn-small" onClick={() => setLang(lang === "en" ? "bn" : "en")} aria-label={t("language")}>
            {lang === "en" ? "বাংলা" : "English"}
          </button>
          <button className="btn btn-small" onClick={onLogout}>
            {t("logout")}
          </button>
        </div>
      </header>
      <div className="shell">
        <aside className="sidebar">
          <nav>
            {NAV.map((item) => (
              <Link
                key={item.href}
                href={item.href}
                className={`nav-link ${pathname === item.href || (item.href !== "/" && pathname.startsWith(item.href)) ? "active" : ""}`}
              >
                {t(item.key)}
              </Link>
            ))}
          </nav>
          <div className="sidebar-footer">
            <div className="who">
              <div className="who-name">{session.member.display_name}</div>
              <div className="subtle-light">{session.member.email}</div>
            </div>
          </div>
        </aside>
        <div className="content-col">
          <main className="content">{children}</main>
          <footer className="audit-footer">{t("portal_footer")}</footer>
        </div>
      </div>
    </div>
  );
}

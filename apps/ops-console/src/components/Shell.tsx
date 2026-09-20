"use client";

// Console shell: left nav, role badge, language toggle, audit-notice footer.

import React from "react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { logout, USING_MOCK_API } from "@/lib/api/client";
import { useLang } from "@/lib/i18n/LangContext";
import type { CopyKey } from "@/lib/i18n/copy";
import { useSession } from "@/lib/useSession";

const NAV: { href: string; key: CopyKey }[] = [
  { href: "/", key: "nav_dashboard" },
  { href: "/approvals", key: "nav_approvals" },
  { href: "/cases", key: "nav_cases" },
  { href: "/disputes", key: "nav_disputes" },
  { href: "/compensation", key: "nav_compensation" },
  { href: "/recon", key: "nav_recon" },
  { href: "/ledger", key: "nav_ledger" },
  { href: "/connectors", key: "nav_connectors" },
  { href: "/operators", key: "nav_operators" },
  { href: "/camlco", key: "nav_camlco" },
  { href: "/exports", key: "nav_exports" },
];

export function Shell({ children }: { children: React.ReactNode }) {
  const { lang, setLang, t } = useLang();
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

  if (loading) {
    return <main className="centered">{t("loading")}</main>;
  }
  if (!session) {
    return <main className="centered">{t("loading")}</main>;
  }

  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">{t("appName")}</div>
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
            <div className="who-name">{session.operator.display_name}</div>
            <div className="role-badges">
              {session.operator.roles.map((r) => (
                <span key={r} className="badge badge-role">
                  {r}
                </span>
              ))}
            </div>
          </div>
          <button
            className="btn btn-small"
            onClick={() => setLang(lang === "en" ? "bn" : "en")}
            aria-label={t("language")}
          >
            {lang === "en" ? "বাংলা" : "English"}
          </button>
          <button className="btn btn-small" onClick={onLogout}>
            {t("logout")}
          </button>
        </div>
      </aside>
      <div className="content-col">
        {USING_MOCK_API ? (
          <div className="notice notice-warn" role="status" data-testid="mock-simulator-banner">
            MOCK / SIMULATOR — not live money. In-app deterministic mock API
            (<code>/api/mock</code>). See DEMO.md.
          </div>
        ) : null}
        <main className="content">{children}</main>
        <footer className="audit-footer">{t("audit_notice")}</footer>
      </div>
    </div>
  );
}

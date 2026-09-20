"use client";

// Public EatClub-style dining-deals discovery. No session required to browse.
// Redemption still requires sign-in (merchant detail → Sign in to redeem).

import React, { useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import type { DinerMerchant, WindowTag } from "@/lib/api/types";
import { discoverMerchants, type DiscoverSource } from "@/lib/demo/discover";
import { useSession } from "@/lib/useSession";
import { useLang } from "@/lib/i18n/LangContext";
import type { CopyKey } from "@/lib/i18n/copy";
import { Shell } from "@/components/Shell";

const WINDOW_FILTERS: { id: WindowTag | ""; labelKey: CopyKey }[] = [
  { id: "", labelKey: "window_all" },
  { id: "lunch", labelKey: "window_lunch" },
  { id: "early_bird", labelKey: "window_early_bird" },
  { id: "happy_hour", labelKey: "window_happy_hour" },
  { id: "late", labelKey: "window_late" },
];

const WINDOW_CHIP_KEYS: Record<WindowTag, CopyKey> = {
  lunch: "window_lunch",
  early_bird: "window_early_bird",
  happy_hour: "window_happy_hour",
  late: "window_late",
};

export default function BrowsePage() {
  // Public browse — never bounce to /login from home.
  const { session, loading: sessionLoading } = useSession(false);
  const { lang, t } = useLang();

  const [rows, setRows] = useState<DinerMerchant[] | null>(null);
  const [source, setSource] = useState<DiscoverSource>("api");
  const [area, setArea] = useState("");
  const [windowTag, setWindowTag] = useState<WindowTag | "">("");
  const [q, setQ] = useState("");
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    discoverMerchants({ area, q, windowTag })
      .then((result) => {
        setRows(result.merchants);
        setSource(result.source);
        setError(null);
      })
      .catch(() => setError(t("error_generic")));
  }, [area, q, windowTag, t]);

  useEffect(() => {
    load();
  }, [load]);

  // Area chips are derived from the unfiltered catalog once loaded so chips
  // remain stable while the user filters.
  const [areaUniverse, setAreaUniverse] = useState<Array<[string, string]>>([]);
  useEffect(() => {
    discoverMerchants({})
      .then((result) => {
        const seen = new Map<string, string>();
        for (const m of result.merchants) {
          if (!seen.has(m.area)) seen.set(m.area, m.area_bn);
        }
        setAreaUniverse([...seen.entries()]);
      })
      .catch(() => undefined);
  }, []);

  const maxDiscount = useMemo(() => {
    const all = rows ?? [];
    let best = 0;
    for (const m of all) {
      if (m.best_percent_bps !== null) {
        best = Math.max(best, Math.floor(m.best_percent_bps / 100));
      }
    }
    return best || 50;
  }, [rows]);

  return (
    <Shell session={session}>
      <section className="hero">
        <p className="hero-eyebrow">{t("hero_eyebrow")}</p>
        <h1 className="hero-title">
          {lang === "bn"
            ? `আপনার কাছের রেস্তোরাঁয় সর্বোচ্চ ${maxDiscount}% ছাড়`
            : `Up to ${maxDiscount}% off at restaurants near you`}
        </h1>
        <p className="hero-subtitle">{t("hero_subtitle")}</p>
        {!session && !sessionLoading ? (
          <p className="hero-cta-row">
            <Link className="btn btn-primary" href="/login?next=/">
              {t("sign_in")}
            </Link>
            <span className="subtle">{t("sign_in_to_redeem_hint")}</span>
          </p>
        ) : null}
      </section>

      {source === "demo" ? (
        <p className="notice demo-banner" role="status">
          {t("demo_catalog_banner")}
        </p>
      ) : null}

      <label className="field">
        <span>{t("browse_search")}</span>
        <input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder={t("browse_search")}
          inputMode="search"
        />
      </label>

      <div className="chip-row" role="tablist" aria-label={t("browse_area_all")}>
        <button
          type="button"
          className={`chip ${area === "" ? "active" : ""}`}
          onClick={() => setArea("")}
        >
          {t("browse_area_all")}
        </button>
        {areaUniverse.map(([en, bn]) => (
          <button
            type="button"
            key={en}
            className={`chip ${area === en ? "active" : ""}`}
            onClick={() => setArea(en)}
          >
            {lang === "bn" ? bn : en}
          </button>
        ))}
      </div>

      <div className="chip-row window-chip-row" role="tablist" aria-label={t("window_all")}>
        {WINDOW_FILTERS.map((f) => (
          <button
            type="button"
            key={f.id || "all"}
            className={`chip window-chip ${windowTag === f.id ? "active" : ""}`}
            onClick={() => setWindowTag(f.id)}
          >
            {t(f.labelKey)}
          </button>
        ))}
      </div>

      {error ? <p className="error-text">{error}</p> : null}
      {rows === null ? (
        <p>{t("loading")}</p>
      ) : rows.length === 0 ? (
        <p className="notice">{t("browse_empty")}</p>
      ) : (
        <>
          <p className="subtle results-count">
            {rows.length} {t("browse_results")}
          </p>
          {rows.map((m) => (
            <Link
              key={m.merchant_id}
              href={`/merchants/${m.merchant_id}`}
              className="merchant-card"
            >
              <div className="merchant-main">
                <div className="merchant-name">
                  {lang === "bn" ? m.display_name_bn : m.display_name}
                </div>
                <div className="merchant-meta">
                  {lang === "bn"
                    ? `${m.area_bn} · ${m.cuisine_bn}`
                    : `${m.area} · ${m.cuisine}`}
                </div>
                <div className="merchant-meta">
                  {m.live_offer_count > 0
                    ? `${m.live_offer_count} ${t("browse_offers_live")}`
                    : t("browse_no_offers_now")}
                </div>
                {(m.window_tags ?? []).length > 0 ? (
                  <div className="window-tag-row">
                    {(m.window_tags ?? []).map((tag) => (
                      <span key={tag} className="window-tag">
                        {t(WINDOW_CHIP_KEYS[tag])}
                      </span>
                    ))}
                  </div>
                ) : null}
              </div>
              {m.best_percent_bps !== null ? (
                <span className="offer-badge">
                  {Math.floor(m.best_percent_bps / 100)}% {t("browse_discount_off")}
                </span>
              ) : (
                <span className="offer-badge muted">{t("browse_no_offers_now")}</span>
              )}
            </Link>
          ))}
        </>
      )}
    </Shell>
  );
}

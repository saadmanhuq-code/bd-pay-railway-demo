"use client";

// Public EatClub-style dining-deals discovery. No session required to browse.
// Redemption still requires sign-in (merchant detail → Sign in to redeem).

import React, { useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import type { CuisineTag, DinerMerchant, WindowTag } from "@/lib/api/types";
import { areasForCity, CUISINE_FILTERS, DEMO_CITIES } from "@/lib/demo/catalog";
import { discoverMerchants, type DiscoverSource } from "@/lib/demo/discover";
import { formatDistanceKm, haversineKm, type LatLng } from "@/lib/demo/geo";
import { useGeolocation } from "@/lib/useGeolocation";
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

const CUISINE_KEYS: Record<CuisineTag, CopyKey> = {
  Bangladeshi: "cuisine_Bangladeshi",
  Biryani: "cuisine_Biryani",
  Cafe: "cuisine_Cafe",
  Chinese: "cuisine_Chinese",
  Thai: "cuisine_Thai",
  Burgers: "cuisine_Burgers",
  Pizza: "cuisine_Pizza",
  Kabab: "cuisine_Kabab",
};

type MerchantRow = DinerMerchant & { distance_km?: number | null };

export default function BrowsePage() {
  // Public browse — never bounce to /login from home.
  const { session, loading: sessionLoading } = useSession(false);
  const { lang, t } = useLang();
  const geo = useGeolocation();

  const [rows, setRows] = useState<MerchantRow[] | null>(null);
  const [source, setSource] = useState<DiscoverSource>("api");
  const [city, setCity] = useState("Dhaka");
  const [area, setArea] = useState("");
  const [cuisine, setCuisine] = useState<CuisineTag | "">("");
  const [windowTag, setWindowTag] = useState<WindowTag | "">("");
  const [q, setQ] = useState("");
  const [sortNear, setSortNear] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const origin: LatLng | null = geo.position;

  const load = useCallback(() => {
    discoverMerchants({ area, q, windowTag, city, cuisine })
      .then((result) => {
        let merchants: MerchantRow[] = result.merchants.map((m) => {
          let distance_km: number | null = null;
          if (origin && m.lat != null && m.lng != null) {
            distance_km = haversineKm(origin, { lat: m.lat, lng: m.lng });
          }
          return { ...m, distance_km };
        });
        if (sortNear && origin) {
          merchants = [...merchants].sort((a, b) => {
            const da = a.distance_km ?? Number.POSITIVE_INFINITY;
            const db = b.distance_km ?? Number.POSITIVE_INFINITY;
            return da - db;
          });
        }
        setRows(merchants);
        setSource(result.source);
        setError(null);
      })
      .catch(() => setError(t("error_generic")));
  }, [area, q, windowTag, city, cuisine, origin, sortNear, t]);

  useEffect(() => {
    load();
  }, [load]);

  const neighborhoodChips = useMemo(() => areasForCity(city), [city]);

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

  function onNearMe() {
    if (geo.status === "granted" && geo.position) {
      setSortNear(true);
      return;
    }
    geo.request();
    setSortNear(true);
  }

  useEffect(() => {
    if (geo.status === "denied" || geo.status === "unavailable") {
      setSortNear(false);
    }
  }, [geo.status]);

  function onCityChange(next: string) {
    setCity(next);
    setArea("");
  }

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

      {source === "osm" ? (
        <p className="notice demo-banner" role="status">
          {t("osm_catalog_banner")}
        </p>
      ) : source === "demo" ? (
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

      <div className="filter-block">
        <span className="filter-label">{t("city_label")}</span>
        <div className="chip-row" role="tablist" aria-label={t("city_label")}>
          {DEMO_CITIES.map((c) => (
            <button
              type="button"
              key={c.city}
              className={`chip ${city === c.city ? "active" : ""}`}
              onClick={() => onCityChange(c.city)}
            >
              {lang === "bn" ? c.city_bn : c.city}
            </button>
          ))}
        </div>
      </div>

      <div className="chip-row" role="tablist" aria-label={t("neighborhood_all")}>
        <button
          type="button"
          className={`chip ${area === "" ? "active" : ""}`}
          onClick={() => setArea("")}
        >
          {t("neighborhood_all")}
        </button>
        {neighborhoodChips.map(([en, bn]) => (
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

      <div className="chip-row" role="tablist" aria-label={t("cuisine_all")}>
        <button
          type="button"
          className={`chip ${cuisine === "" ? "active" : ""}`}
          onClick={() => setCuisine("")}
        >
          {t("cuisine_all")}
        </button>
        {CUISINE_FILTERS.map((c) => (
          <button
            type="button"
            key={c}
            className={`chip ${cuisine === c ? "active" : ""}`}
            onClick={() => setCuisine(c)}
          >
            {t(CUISINE_KEYS[c])}
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

      <div className="near-me-row">
        <button
          type="button"
          className={`chip near-me-chip ${sortNear && geo.status === "granted" ? "active" : ""}`}
          onClick={onNearMe}
          disabled={geo.status === "prompting"}
        >
          {geo.status === "prompting" ? t("near_me_locating") : t("near_me")}
        </button>
        {sortNear && geo.status === "granted" ? (
          <span className="subtle">{t("sort_nearby")}</span>
        ) : null}
        {geo.status === "denied" ? (
          <span className="subtle geo-hint">{t("near_me_denied")}</span>
        ) : null}
        {geo.status === "unavailable" ? (
          <span className="subtle geo-hint">{t("near_me_unavailable")}</span>
        ) : null}
      </div>

      {error ? <p className="error-text">{error}</p> : null}
      {rows === null ? (
        <p>{t("loading")}</p>
      ) : rows.length === 0 ? (
        <p className="notice">
          {city !== "Dhaka" ? t("city_stub_empty") : t("browse_empty")}
        </p>
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
              {m.photo_urls?.[0] ? (
                // eslint-disable-next-line @next/next/no-img-element
                <img
                  className="merchant-thumb"
                  src={m.photo_urls[0]}
                  alt=""
                  width={72}
                  height={72}
                  loading="lazy"
                />
              ) : (
                <div className="merchant-thumb placeholder" aria-hidden />
              )}
              <div className="merchant-main">
                <div className="merchant-name">
                  {lang === "bn" ? m.display_name_bn : m.display_name}
                </div>
                <div className="merchant-meta">
                  {lang === "bn"
                    ? `${m.area_bn} · ${m.cuisine_bn}`
                    : `${m.area} · ${m.cuisine}`}
                  {m.distance_km != null ? (
                    <span className="distance-pill">
                      {" "}
                      · {formatDistanceKm(m.distance_km, lang)} {t("distance_away")}
                    </span>
                  ) : null}
                </div>
                {m.rating_avg != null ? (
                  <div className="merchant-meta">
                    ★ {m.rating_avg.toFixed(1)} · {m.rating_count ?? 0} {t("rating_label")}
                  </div>
                ) : null}
                <div className="merchant-meta">
                  {m.live_offer_count > 0
                    ? `${m.live_offer_count} ${t("browse_offers_live")}`
                    : t("browse_no_offers_now")}
                </div>
                {(m.cuisine_tags ?? []).length > 0 ? (
                  <div className="window-tag-row">
                    {(m.cuisine_tags ?? []).map((tag) => (
                      <span key={tag} className="window-tag cuisine-tag">
                        {t(CUISINE_KEYS[tag])}
                      </span>
                    ))}
                  </div>
                ) : null}
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

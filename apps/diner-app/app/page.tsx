"use client";

// Browse — restaurants with live offers (area chips + search). spec/18 keeps
// maps/geo/search-ranking out of scope until activation; this is the simple
// directory browse over the activation-time discovery seam (errata S18-E17).

import React, { useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { listDinerMerchants } from "@/lib/api/client";
import type { DinerMerchant } from "@/lib/api/types";
import { useSession } from "@/lib/useSession";
import { useLang } from "@/lib/i18n/LangContext";
import { Shell } from "@/components/Shell";

export default function BrowsePage() {
  const { session, loading } = useSession();
  const { lang, t } = useLang();

  const [rows, setRows] = useState<DinerMerchant[] | null>(null);
  const [area, setArea] = useState("");
  const [q, setQ] = useState("");
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    if (!session) return;
    listDinerMerchants({ area, q })
      .then((page) => {
        setRows(page.data);
        setError(null);
      })
      .catch(() => setError(t("error_generic")));
  }, [session, area, q, t]);

  useEffect(() => {
    load();
  }, [load]);

  const areas = useMemo(() => {
    const all = rows ?? [];
    const seen = new Map<string, string>();
    for (const m of all) {
      if (!seen.has(m.area)) seen.set(m.area, m.area_bn);
    }
    return [...seen.entries()];
  }, [rows]);

  if (loading || !session) {
    return (
      <Shell session={null}>
        <p>{t("loading")}</p>
      </Shell>
    );
  }

  return (
    <Shell session={session}>
      <h1>{t("browse_title")}</h1>
      <p className="subtle">{t("browse_note")}</p>

      <label className="field">
        <span>{t("browse_search")}</span>
        <input value={q} onChange={(e) => setQ(e.target.value)} />
      </label>

      <div className="chip-row">
        <button className={`chip ${area === "" ? "active" : ""}`} onClick={() => setArea("")}>
          {t("browse_area_all")}
        </button>
        {areas.map(([en, bn]) => (
          <button
            key={en}
            className={`chip ${area === en ? "active" : ""}`}
            onClick={() => setArea(en)}
          >
            {lang === "bn" ? bn : en}
          </button>
        ))}
      </div>

      {error ? <p className="error-text">{error}</p> : null}
      {rows === null ? (
        <p>{t("loading")}</p>
      ) : rows.length === 0 ? (
        <p className="notice">{t("browse_empty")}</p>
      ) : (
        rows.map((m) => (
          <Link key={m.merchant_id} href={`/merchants/${m.merchant_id}`} className="merchant-card">
            <div className="merchant-main">
              <div className="merchant-name">
                {lang === "bn" ? m.display_name_bn : m.display_name}
              </div>
              <div className="merchant-meta">
                {lang === "bn" ? `${m.area_bn} · ${m.cuisine_bn}` : `${m.area} · ${m.cuisine}`}
              </div>
              <div className="merchant-meta">
                {m.live_offer_count > 0
                  ? `${m.live_offer_count} ${t("browse_offers_live")}`
                  : t("browse_no_offers_now")}
              </div>
            </div>
            {m.best_percent_bps !== null ? (
              <span className="offer-badge">
                {t("browse_up_to")} {Math.floor(m.best_percent_bps / 100)}% {t("browse_discount_off")}
              </span>
            ) : (
              <span className="offer-badge muted">{t("browse_no_offers_now")}</span>
            )}
          </Link>
        ))
      )}
    </Shell>
  );
}

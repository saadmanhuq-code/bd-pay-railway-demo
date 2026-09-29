"use client";

// Merchant dashboard: payment volume cards, success rate, settlement
// summary, recent intents (spec/02 FSM states), data-age badges.

import React, { useCallback, useEffect, useState } from "react";
import { ApiError, getMerchantDashboard } from "@/lib/api/client";
import type { MerchantDashboard, PaymentIntentSummary } from "@/lib/api/types";
import { AgeBadge, EnvBadge, MethodLabel, StateBadge } from "@/components/Badges";
import { DataTable, type Column } from "@bdpay/edge/ui/DataTable";
import { Shell } from "@/components/Shell";
import { formatBdt, formatTs, shortId } from "@/lib/format";
import { useLang } from "@/lib/i18n/LangContext";
import { useMode } from "@/lib/mode";

const INTENT_COLUMNS: Column<PaymentIntentSummary>[] = [
  { key: "id", label: "Intent", render: (r) => <span className="mono">{shortId(r.payment_intent_id, 18)}</span> },
  {
    key: "amount",
    label: "Amount",
    sortValue: (r) => Number(BigInt(r.amount_minor)),
    render: (r) => formatBdt(r.amount_minor),
  },
  { key: "method", label: "Method", sortValue: (r) => r.method, render: (r) => <MethodLabel value={r.method} /> },
  { key: "status", label: "State", sortValue: (r) => r.status, render: (r) => <StateBadge value={r.status} /> },
  { key: "created", label: "Created", sortValue: (r) => r.created_at, render: (r) => formatTs(r.created_at) },
];

// Breakdown of the recent-intents rows the server already returned (no new
// numbers): fills the success-rate card with what sits behind the rate
// (FE QA 2026-09-29 — the card was one number in a large empty box).
const OUTCOMES: { key: string; label: string; cls: string; match: (s: string) => boolean }[] = [
  { key: "ok", label: "Succeeded", cls: "mix-ok", match: (s) => s === "SUCCEEDED" || s === "PARTIALLY_REFUNDED" },
  { key: "fail", label: "Failed", cls: "mix-fail", match: (s) => s === "FAILED" || s === "CANCELLED" },
  { key: "open", label: "In progress", cls: "mix-open", match: () => true },
];

function StatusMix({ intents }: { intents: PaymentIntentSummary[] }) {
  if (intents.length === 0) {
    return <p className="subtle mix-empty">No payment intents yet — create one in the Sandbox to see outcomes here.</p>;
  }
  const counts = OUTCOMES.map((o) => ({ ...o, n: 0 }));
  for (const r of intents) {
    const hit = counts.find((o) => o.match(r.status));
    if (hit) hit.n += 1;
  }
  return (
    <div className="status-mix">
      <p className="mix-title">Recent outcomes · last {intents.length} payment intents</p>
      <div className="mix-bar" aria-hidden>
        {counts
          .filter((c) => c.n > 0)
          .map((c) => (
            <span key={c.key} className={c.cls} style={{ flexGrow: c.n }} />
          ))}
      </div>
      <ul className="mix-legend">
        {counts.map((c) => (
          <li key={c.key}>
            <span className={`mix-dot ${c.cls}`} aria-hidden /> {c.label} <strong>{c.n}</strong>
          </li>
        ))}
      </ul>
    </div>
  );
}

export default function DashboardPage() {
  const { t } = useLang();
  const { env } = useMode();
  const [dash, setDash] = useState<MerchantDashboard | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    getMerchantDashboard(env)
      .then((d) => {
        setDash(d);
        setError(null);
      })
      .catch((err) => setError(err instanceof ApiError ? err.message : t("error_generic")));
  }, [env, t]);

  useEffect(() => {
    load();
  }, [load]);

  return (
    <Shell>
      <h1>{t("nav_dashboard")}</h1>
      {error ? <p className="error-text">{error}</p> : null}
      {!dash ? (
        <p>{t("loading")}</p>
      ) : (
        <>
          <div className="toolbar">
            <EnvBadge env={dash.env} />
            <AgeBadge asOf={dash.as_of} />
            <button className="btn btn-small" onClick={load}>
              {t("refresh")}
            </button>
          </div>
          <div className="grid-3">
            <section className="panel">
              <div className="kpi">{formatBdt(dash.volume_today_minor)}</div>
              <div className="kpi-label">Volume today · {dash.count_today} intents</div>
            </section>
            <section className="panel">
              <div className="kpi">{formatBdt(dash.volume_7d_minor)}</div>
              <div className="kpi-label">Volume 7 days</div>
            </section>
            <section className="panel">
              <div className="kpi">{formatBdt(dash.volume_30d_minor)}</div>
              <div className="kpi-label">Volume 30 days</div>
            </section>
          </div>
          <div className="grid-2">
            <section className="panel">
              <div className="kpi">{(dash.success_rate_bps / 100).toFixed(2)}%</div>
              <div className="kpi-label">Success rate (terminal intents)</div>
              <StatusMix intents={dash.recent_intents} />
            </section>
            <section className="panel">
              <h2>Settlement</h2>
              <dl className="kv">
                <dt>Pending</dt>
                <dd>{formatBdt(dash.settlement.pending_minor)}</dd>
                <dt>Next release</dt>
                <dd>{formatTs(dash.settlement.next_release_at)}</dd>
                <dt>Last settled</dt>
                <dd>
                  {formatBdt(dash.settlement.last_settled_minor)} <span className="subtle">{formatTs(dash.settlement.last_settled_at)}</span>
                </dd>
              </dl>
            </section>
          </div>
          <section className="panel">
            <div className="panel-head">
              <h2>Recent payment intents</h2>
              <AgeBadge asOf={dash.as_of} />
            </div>
            <DataTable columns={INTENT_COLUMNS} rows={dash.recent_intents} rowKey={(r) => r.payment_intent_id} />
          </section>
        </>
      )}
    </Shell>
  );
}

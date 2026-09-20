"use client";

// Merchant dashboard: payment volume cards, success rate, settlement
// summary, recent intents (spec/02 FSM states), data-age badges.

import React, { useCallback, useEffect, useState } from "react";
import { ApiError, getMerchantDashboard } from "@/lib/api/client";
import type { MerchantDashboard, PaymentIntentSummary } from "@/lib/api/types";
import { AgeBadge, EnvBadge, StateBadge } from "@/components/Badges";
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
  { key: "method", label: "Method", sortValue: (r) => r.method, render: (r) => r.method },
  { key: "status", label: "State", sortValue: (r) => r.status, render: (r) => <StateBadge value={r.status} /> },
  { key: "created", label: "Created", sortValue: (r) => r.created_at, render: (r) => formatTs(r.created_at) },
];

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

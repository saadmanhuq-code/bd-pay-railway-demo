"use client";

// Dashboard home — TCSA coverage, settlement pipeline, connector health grid,
// AML queue depth, SLA percentiles. Every panel carries a data-age badge.

import React, { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { Shell } from "@/components/Shell";
import { AgeBadge, StateBadge } from "@/components/Badges";
import {
  getAmlDashboard,
  getConnectorsDashboard,
  getSettlementDashboard,
  getSlaDashboard,
  getTcsaDashboard,
} from "@/lib/api/client";
import type {
  AmlDashboard,
  ConnectorsDashboard,
  SettlementDashboard,
  SlaDashboard,
  TcsaDashboard,
} from "@/lib/api/types";
import { formatBdt, formatTs } from "@/lib/format";
import { useLang } from "@/lib/i18n/LangContext";

/** Bar height (%) for the TCSA trend, scaled to the observed range so small
 * day-to-day moves are visible (a raw 0–100% scale drew six identical bars). */
function trendHeight(v: number, all: number[]): number {
  const min = Math.min(...all);
  const max = Math.max(...all);
  if (max === min) return 60;
  return Math.round(20 + ((v - min) / (max - min)) * 80);
}

function PanelPending({ failed, onRetry }: { failed: boolean; onRetry: () => void }) {
  const { t } = useLang();
  if (!failed) return <p className="subtle" aria-busy="true">{t("loading")}</p>;
  return (
    <p className="error-text" role="alert">
      {t("dash_panel_failed")}{" "}
      <button className="btn btn-small" onClick={onRetry}>
        {t("dash_retry")}
      </button>
    </p>
  );
}

export default function DashboardPage() {
  const { t } = useLang();
  const [tcsa, setTcsa] = useState<TcsaDashboard | null>(null);
  const [settlement, setSettlement] = useState<SettlementDashboard | null>(null);
  const [conn, setConn] = useState<ConnectorsDashboard | null>(null);
  const [aml, setAml] = useState<AmlDashboard | null>(null);
  const [sla, setSla] = useState<SlaDashboard | null>(null);
  const [failed, setFailed] = useState<Record<string, boolean>>({});

  const load = useCallback(() => {
    setFailed({});
    const fail = (k: string) => () => setFailed((f) => ({ ...f, [k]: true }));
    getTcsaDashboard().then(setTcsa).catch(fail("tcsa"));
    getSettlementDashboard().then(setSettlement).catch(fail("settlement"));
    getConnectorsDashboard().then(setConn).catch(fail("conn"));
    getAmlDashboard().then(setAml).catch(fail("aml"));
    getSlaDashboard().then(setSla).catch(fail("sla"));
  }, []);

  useEffect(load, [load]);

  return (
    <Shell>
      <div className="panel-head">
        <h1>{t("nav_dashboard")}</h1>
        <button className="btn btn-small" onClick={load}>
          {t("refresh")}
        </button>
      </div>

      <div className="grid-2">
        <section className="panel">
          <div className="panel-head">
            <h2>{t("dash_tcsa")}</h2>
            {tcsa ? <AgeBadge asOf={tcsa.as_of} /> : null}
          </div>
          {tcsa ? (
            <>
              <div className="kpi">{(tcsa.coverage_bps / 100).toFixed(2)}%</div>
              <div className="kpi-label">{t("dash_tcsa_label")}</div>
              <dl className="kv">
                <dt>{t("dash_required")}</dt>
                <dd>{formatBdt(tcsa.required_minor)}</dd>
                <dt>{t("dash_available")}</dt>
                <dd>{formatBdt(tcsa.available_minor)}</dd>
                <dt>{t("dash_shortfall")}</dt>
                <dd>{formatBdt(tcsa.shortfall_minor)}</dd>
                <dt>{t("dash_open_comp")}</dt>
                <dd>{tcsa.open_compensation_over_attestation_threshold}</dd>
              </dl>
              <div className="hist-bar" role="img" aria-label={t("dash_tcsa_trend")}>
                {tcsa.trend.map((pt) => (
                  <div key={pt.at} className="hist-col" title={`${formatTs(pt.at)} · ${(pt.coverage_bps / 100).toFixed(2)}%`}>
                    <div
                      className={`hist-fill ${pt.coverage_bps < 10000 ? "hist-fill-bad" : ""}`}
                      style={{ height: `${trendHeight(pt.coverage_bps, tcsa.trend.map((x) => x.coverage_bps))}%` }}
                    />
                    <span>{(pt.coverage_bps / 100).toFixed(1)}%</span>
                  </div>
                ))}
              </div>
              <p className="subtle">{t("dash_tcsa_trend")}</p>
            </>
          ) : (
            <PanelPending failed={Boolean(failed.tcsa)} onRetry={load} />
          )}
        </section>

        <section className="panel">
          <div className="panel-head">
            <h2>{t("dash_settlement")}</h2>
            {settlement ? <AgeBadge asOf={settlement.as_of} /> : null}
          </div>
          {settlement ? (
            <>
              <table className="data-table">
                <thead>
                  <tr>
                    <th>{t("dash_state")}</th>
                    <th>{t("dash_count")}</th>
                    <th>{t("dash_amount")}</th>
                  </tr>
                </thead>
                <tbody>
                  {settlement.instructions_by_state.map((row) => (
                    <tr key={row.state}>
                      <td>
                        <StateBadge value={row.state} />
                      </td>
                      <td>{row.count}</td>
                      <td>{formatBdt(row.amount_minor)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <p className="subtle">
                {t("dash_open_batches")}: {settlement.open_batches} · {t("dash_earliest_release")}{" "}
                {formatTs(settlement.earliest_release_at)} · {t("dash_latest_release")} {formatTs(settlement.latest_release_at)} ·{" "}
                {t("dash_deadline_breaches")}: {settlement.working_day_deadline_breaches}
              </p>
            </>
          ) : (
            <PanelPending failed={Boolean(failed.settlement)} onRetry={load} />
          )}
        </section>
      </div>

      <section className="panel">
        <div className="panel-head">
          <h2>{t("dash_connectors")}</h2>
          {conn ? <AgeBadge asOf={conn.as_of} /> : null}
        </div>
        {conn ? (
          <table className="data-table">
            <thead>
              <tr>
                <th>{t("dash_connector")}</th>
                <th>{t("dash_mode")}</th>
                <th>{t("dash_health")}</th>
                <th>{t("dash_circuit")}</th>
                <th>{t("dash_last_health")}</th>
              </tr>
            </thead>
            <tbody>
              {conn.connectors.map((c) => (
                <tr key={c.connector_id}>
                  <td>
                    <Link href={`/connectors/${c.connector_id}`}>{c.display_name}</Link>
                  </td>
                  <td>
                    <StateBadge value={c.active_mode} />
                  </td>
                  <td>
                    <StateBadge value={c.health_status} />
                  </td>
                  <td>
                    <StateBadge value={c.circuit_state} circuit />
                  </td>
                  <td className="subtle">{formatTs(c.last_health_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <PanelPending failed={Boolean(failed.conn)} onRetry={load} />
        )}
      </section>

      <div className="grid-2">
        <section className="panel">
          <div className="panel-head">
            <h2>{t("dash_aml")}</h2>
            {aml ? <AgeBadge asOf={aml.as_of} /> : null}
          </div>
          {aml ? (
            <>
              <div className="kpi">{aml.alert_queue_depth}</div>
              <div className="kpi-label">{t("dash_open_alerts")}</div>
              <div className="hist-bar">
                {aml.alert_age_histogram.map((b) => (
                  <div key={b.bucket} className="hist-col">
                    <div className="hist-fill" style={{ height: `${Math.min(100, b.count * 18)}%` }} />
                    <span>
                      {b.bucket} ({b.count})
                    </span>
                  </div>
                ))}
              </div>
              <p className="subtle">
                {t("dash_sanctions_age")}: {Math.round(aml.sanctions_feed_age_seconds / 60)} min{" "}
                {aml.sanctions_feed_stale ? t("dash_stale") : t("dash_fresh")} ·{" "}
                <Link href="/camlco">{t("dash_camlco_link")}</Link>
              </p>
            </>
          ) : (
            <PanelPending failed={Boolean(failed.aml)} onRetry={load} />
          )}
        </section>

        <section className="panel">
          <div className="panel-head">
            <h2>{t("dash_sla")}</h2>
            {sla ? <AgeBadge asOf={sla.as_of} /> : null}
          </div>
          {sla ? (
            <table className="data-table">
              <thead>
                <tr>
                  <th>{t("dash_route_group")}</th>
                  <th>p50</th>
                  <th>p95</th>
                  <th>p99</th>
                </tr>
              </thead>
              <tbody>
                {sla.route_groups.map((g) => (
                  <tr key={g.group}>
                    <td>{g.group}</td>
                    <td>{g.p50_ms} ms</td>
                    <td>{g.p95_ms} ms</td>
                    <td>{g.p99_ms} ms</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <PanelPending failed={Boolean(failed.sla)} onRetry={load} />
          )}
        </section>
      </div>
    </Shell>
  );
}

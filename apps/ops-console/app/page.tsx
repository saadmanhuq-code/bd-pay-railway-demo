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
import { formatBdt } from "@/lib/format";
import { useLang } from "@/lib/i18n/LangContext";

export default function DashboardPage() {
  const { t } = useLang();
  const [tcsa, setTcsa] = useState<TcsaDashboard | null>(null);
  const [settlement, setSettlement] = useState<SettlementDashboard | null>(null);
  const [conn, setConn] = useState<ConnectorsDashboard | null>(null);
  const [aml, setAml] = useState<AmlDashboard | null>(null);
  const [sla, setSla] = useState<SlaDashboard | null>(null);

  const load = useCallback(() => {
    getTcsaDashboard().then(setTcsa).catch(() => setTcsa(null));
    getSettlementDashboard().then(setSettlement).catch(() => setSettlement(null));
    getConnectorsDashboard().then(setConn).catch(() => setConn(null));
    getAmlDashboard().then(setAml).catch(() => setAml(null));
    getSlaDashboard().then(setSla).catch(() => setSla(null));
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
            <h2>TCSA coverage</h2>
            {tcsa ? <AgeBadge asOf={tcsa.as_of} /> : null}
          </div>
          {tcsa ? (
            <>
              <div className="kpi">{(tcsa.coverage_bps / 100).toFixed(2)}%</div>
              <div className="kpi-label">trust-account coverage (60s snapshot cadence)</div>
              <dl className="kv">
                <dt>Required</dt>
                <dd>{formatBdt(tcsa.required_minor)}</dd>
                <dt>Available</dt>
                <dd>{formatBdt(tcsa.available_minor)}</dd>
                <dt>Shortfall</dt>
                <dd>{formatBdt(tcsa.shortfall_minor)}</dd>
                <dt>Open compensation items above attestation threshold</dt>
                <dd>{tcsa.open_compensation_over_attestation_threshold}</dd>
              </dl>
              <div className="hist-bar">
                {tcsa.trend.map((pt) => (
                  <div key={pt.at} className="hist-col" title={pt.at}>
                    <div className="hist-fill" style={{ height: `${Math.min(100, (pt.coverage_bps - 10000) / 2 + 30)}%` }} />
                    <span>{(pt.coverage_bps / 100).toFixed(1)}</span>
                  </div>
                ))}
              </div>
            </>
          ) : (
            <p className="subtle">{t("loading")}</p>
          )}
        </section>

        <section className="panel">
          <div className="panel-head">
            <h2>Settlement pipeline</h2>
            {settlement ? <AgeBadge asOf={settlement.as_of} /> : null}
          </div>
          {settlement ? (
            <>
              <table className="data-table">
                <thead>
                  <tr>
                    <th>State</th>
                    <th>Count</th>
                    <th>Amount</th>
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
                Open batches: {settlement.open_batches} · earliest release {settlement.earliest_release_at} · latest{" "}
                {settlement.latest_release_at} · 5-working-day deadline breaches: {settlement.working_day_deadline_breaches}
              </p>
            </>
          ) : (
            <p className="subtle">{t("loading")}</p>
          )}
        </section>
      </div>

      <section className="panel">
        <div className="panel-head">
          <h2>Connector health grid</h2>
          {conn ? <AgeBadge asOf={conn.as_of} /> : null}
        </div>
        {conn ? (
          <table className="data-table">
            <thead>
              <tr>
                <th>Connector</th>
                <th>Mode</th>
                <th>Health</th>
                <th>Circuit</th>
                <th>Last health</th>
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
                  <td className="subtle">{c.last_health_at}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <p className="subtle">{t("loading")}</p>
        )}
      </section>

      <div className="grid-2">
        <section className="panel">
          <div className="panel-head">
            <h2>AML queue depth</h2>
            {aml ? <AgeBadge asOf={aml.as_of} /> : null}
          </div>
          {aml ? (
            <>
              <div className="kpi">{aml.alert_queue_depth}</div>
              <div className="kpi-label">open alerts</div>
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
                Sanctions feed age: {aml.sanctions_feed_age_seconds}s {aml.sanctions_feed_stale ? "— STALE" : "(fresh)"} ·{" "}
                <Link href="/camlco">CAMLCO dashboard →</Link>
              </p>
            </>
          ) : (
            <p className="subtle">{t("loading")}</p>
          )}
        </section>

        <section className="panel">
          <div className="panel-head">
            <h2>SLA percentiles (per route group)</h2>
            {sla ? <AgeBadge asOf={sla.as_of} /> : null}
          </div>
          {sla ? (
            <table className="data-table">
              <thead>
                <tr>
                  <th>Route group</th>
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
            <p className="subtle">{t("loading")}</p>
          )}
        </section>
      </div>
    </Shell>
  );
}

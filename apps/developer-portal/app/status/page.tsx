"use client";

// Public connector-health status page (spec/16 §E). Renders EXCLUSIVELY from
// the three public endpoints — GET /v1/public/status, GET
// /v1/public/certification-matrix and the /v1/public/badges/{id}.svg images.
// No auth, no second data path. Modes are shown verbatim (SIMULATOR/SANDBOX
// honesty pre-license).

import React, { useCallback, useEffect, useState } from "react";
import {
  ApiError,
  getPublicCertificationMatrix,
  getPublicStatus,
  getSandboxDemoFullflow,
  publicBadgeUrl,
} from "@/lib/api/client";
import type { SandboxDemoFullflow } from "@/lib/api/types";
import type {
  CertificationMatrix,
  CircuitState,
  HealthStatus,
  PublicStatusFeed,
} from "@/lib/api/launchTypes";
import { CERT_CHECK_IDS } from "@/lib/api/launchTypes";
import { AgeBadge } from "@/components/Badges";
import { PublicShell } from "@/components/PublicShell";
import { formatTs, shortId } from "@/lib/format";
import { useLang } from "@/lib/i18n/LangContext";

function uptimeLabel(bps: number): string {
  return `${(bps / 100).toFixed(2)}%`;
}

function HealthBadge({ value }: { value: HealthStatus }) {
  const cls = value === "HEALTHY" ? "badge-good" : value === "DEGRADED" ? "badge-warn" : "badge-bad";
  return <span className={`badge ${cls}`}>{value}</span>;
}

function CircuitBadge({ value }: { value: CircuitState }) {
  const cls = value === "CLOSED" ? "badge-good" : value === "HALF_OPEN" ? "badge-warn" : "badge-bad";
  return <span className={`badge ${cls}`}>{value}</span>;
}

export default function StatusPage() {
  const { t } = useLang();
  const [status, setStatus] = useState<PublicStatusFeed | null>(null);
  const [matrix, setMatrix] = useState<CertificationMatrix | null>(null);
  const [demo, setDemo] = useState<SandboxDemoFullflow | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    // Load independently: a missing sandbox demo route must not blank
    // connector health + certification (demo-risk on /status).
    setError(null);
    void getPublicStatus()
      .then((s) => setStatus(s))
      .catch((err) => {
        setStatus(null);
        setError(err instanceof ApiError ? err.message : t("error_generic"));
      });
    void getPublicCertificationMatrix()
      .then((m) => setMatrix(m))
      .catch((err) => {
        setMatrix(null);
        setError((prev) => prev ?? (err instanceof ApiError ? err.message : t("error_generic")));
      });
    void getSandboxDemoFullflow()
      .then((d) => setDemo(d))
      .catch(() => setDemo(null));
  }, [t]);

  useEffect(() => {
    load();
  }, [load]);

  return (
    <PublicShell>
      <h1>{t("status_title")}</h1>
      {error ? <p className="error-text">{error}</p> : null}
      <p className="subtle">{t("status_source_note")}</p>

      <section className="panel">
        <div className="panel-head">
          <h2>Sandbox demo key + payment</h2>
          {demo ? <AgeBadge asOf={demo.summary.as_of} /> : null}
        </div>
        {demo ? (
          <dl className="kv">
            <dt>Merchant</dt>
            <dd className="mono">{demo.intent.merchant_id}</dd>
            <dt>Demo key id</dt>
            <dd className="mono">{demo.auth.principal_id}</dd>
            <dt>Payment intent</dt>
            <dd className="mono">{demo.intent.payment_intent_id}</dd>
            <dt>Status</dt>
            <dd>{demo.summary.status}</dd>
            <dt>Refunded</dt>
            <dd>{demo.summary.refunded_minor} paisa</dd>
          </dl>
        ) : (
          <p className="subtle">Live sandbox demo flow is unavailable.</p>
        )}
      </section>

      <section className="panel">
        <div className="panel-head">
          <h2>{t("status_connectors")}</h2>
          {status ? <AgeBadge asOf={status.as_of} /> : null}
        </div>
        <p className="notice notice-warn">{t("status_mode_note")}</p>
        <table className="data-table">
          <thead>
            <tr>
              <th>Connector</th>
              <th>Mode</th>
              <th>Health</th>
              <th>Circuit</th>
              <th>Uptime 24h</th>
              <th>Uptime 7d</th>
              <th>Last health check</th>
            </tr>
          </thead>
          <tbody>
            {!status ? (
              <tr>
                <td colSpan={7} className="empty-row">
                  {t("loading")}
                </td>
              </tr>
            ) : (
              status.data.map((c) => (
                <tr key={c.connector_id}>
                  <td>
                    {c.display_name}
                    <div className="subtle mono">{c.connector_id}</div>
                  </td>
                  <td>
                    <span className="badge badge-warn">{c.active_mode}</span>
                    <div className="subtle">{c.active_mode === "SIMULATOR" ? t("mode_simulator") : t("mode_sandbox_rail")}</div>
                  </td>
                  <td>
                    <HealthBadge value={c.health_status} />
                  </td>
                  <td>
                    <CircuitBadge value={c.circuit_state} />
                  </td>
                  <td>{uptimeLabel(c.uptime_24h_bps)}</td>
                  <td>{uptimeLabel(c.uptime_7d_bps)}</td>
                  <td>{formatTs(c.last_health_at)}</td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </section>

      <section className="panel">
        <div className="panel-head">
          <h2>{t("status_certification")}</h2>
          {matrix ? <AgeBadge asOf={matrix.as_of} /> : null}
        </div>
        <p className="subtle">{t("status_cert_note")}</p>
        <table className="data-table">
          <thead>
            <tr>
              <th>Connector</th>
              <th>Badge</th>
              <th>Last certified</th>
              <th>Adapter version</th>
              <th>Checks (CERT-01…10)</th>
              <th>Report hash</th>
            </tr>
          </thead>
          <tbody>
            {!matrix ? (
              <tr>
                <td colSpan={6} className="empty-row">
                  {t("loading")}
                </td>
              </tr>
            ) : (
              matrix.data.map((c) => (
                <tr key={c.connector_id}>
                  <td>
                    {c.display_name}
                    <div className="subtle mono">{c.connector_id}</div>
                  </td>
                  <td>
                    {/* eslint-disable-next-line @next/next/no-img-element -- SVG badge served by the public badge endpoint, not a static asset */}
                    <img
                      className="badge-img"
                      src={publicBadgeUrl(c.connector_id)}
                      alt={`${c.display_name} certification: ${c.certification_status}`}
                    />
                  </td>
                  <td>{formatTs(c.last_certified_at)}</td>
                  <td className="mono">{c.adapter_version}</td>
                  <td>
                    <span className="verdict-chips">
                      {CERT_CHECK_IDS.map((checkId) => {
                        const verdict = c.checks[checkId];
                        if (!verdict) {
                          return (
                            <span key={checkId} className="badge" title={`${checkId}: no verdict`}>
                              {checkId.replace("CERT-", "")}
                            </span>
                          );
                        }
                        return (
                          <span
                            key={checkId}
                            className={`badge ${verdict === "PASSED" ? "badge-good" : "badge-bad"}`}
                            title={`${checkId}: ${verdict}`}
                          >
                            {checkId.replace("CERT-", "")}
                          </span>
                        );
                      })}
                    </span>
                  </td>
                  <td className="mono" title={c.report_hash ?? undefined}>
                    {c.report_hash ? shortId(c.report_hash, 16) : "—"}
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </section>
    </PublicShell>
  );
}

"use client";

// CAMLCO dashboard — AML alert age histogram, STR queue, goAML filing queue,
// sanctions feed staleness, freeze action with DualLabel confirm.

import React, { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { Shell } from "@/components/Shell";
import { AgeBadge, StateBadge } from "@/components/Badges";
import { TotpModal } from "@/components/TotpModal";
import { DualLabel } from "@/lib/i18n/DualLabel";
import { ApiError, camlcoFreeze, getAmlDashboard } from "@/lib/api/client";
import type { AmlDashboard } from "@/lib/api/types";
import { formatTs, shortId } from "@/lib/format";
import { useLang } from "@/lib/i18n/LangContext";

export default function CamlcoPage() {
  const { t } = useLang();
  const [aml, setAml] = useState<AmlDashboard | null>(null);
  const [freezeOpen, setFreezeOpen] = useState(false);
  const [subjectId, setSubjectId] = useState("");
  const [subjectType, setSubjectType] = useState("Merchant");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [apprCreated, setApprCreated] = useState<string | null>(null);

  const load = useCallback(() => {
    getAmlDashboard().then(setAml).catch(() => setAml(null));
  }, []);

  useEffect(load, [load]);

  async function onConfirm(totpCode: string, reason: string) {
    setBusy(true);
    setError(null);
    try {
      const appr = await camlcoFreeze(subjectType, subjectId, reason, totpCode);
      setApprCreated(appr.approval_request_id);
      setFreezeOpen(false);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : t("error_generic"));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Shell>
      <div className="panel-head">
        <h1>{t("nav_camlco")}</h1>
        {aml ? <AgeBadge asOf={aml.as_of} /> : null}
      </div>

      {aml ? (
        <>
          <div className="grid-2">
            <section className="panel">
              <div className="panel-head">
                <h2>AML alert age histogram</h2>
                <AgeBadge asOf={aml.as_of} />
              </div>
              <div className="kpi">{aml.alert_queue_depth}</div>
              <div className="kpi-label">open alerts (48h SLA per alert)</div>
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
            </section>

            <section className="panel">
              <div className="panel-head">
                <h2>Sanctions feed</h2>
                <AgeBadge asOf={aml.as_of} />
              </div>
              <div className="kpi">{Math.floor(aml.sanctions_feed_age_seconds / 60)} min</div>
              <div className="kpi-label">feed age</div>
              {aml.sanctions_feed_stale ? (
                <p className="notice notice-danger">Sanctions feed STALE — P0 compliance case opens automatically (cut-last #5).</p>
              ) : (
                <p className="notice">Feed fresh — within staleness threshold.</p>
              )}
            </section>
          </div>

          <div className="grid-2">
            <section className="panel">
              <div className="panel-head">
                <h2>STR queue</h2>
                <AgeBadge asOf={aml.as_of} />
              </div>
              <table className="data-table">
                <thead>
                  <tr>
                    <th>STR</th>
                    <th>Subject</th>
                    <th>State</th>
                    <th>Created</th>
                  </tr>
                </thead>
                <tbody>
                  {aml.str_queue.map((s) => (
                    <tr key={s.str_id}>
                      <td className="mono">{shortId(s.str_id, 18)}</td>
                      <td className="mono">{shortId(s.subject_id, 18)}</td>
                      <td>
                        <StateBadge value={s.state} />
                      </td>
                      <td>{formatTs(s.created_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </section>

            <section className="panel">
              <div className="panel-head">
                <h2>goAML filing queue</h2>
                <AgeBadge asOf={aml.as_of} />
              </div>
              <table className="data-table">
                <thead>
                  <tr>
                    <th>Filing</th>
                    <th>State</th>
                    <th>Submitted</th>
                  </tr>
                </thead>
                <tbody>
                  {aml.goaml_filings.map((f) => (
                    <tr key={f.filing_id}>
                      <td className="mono">{shortId(f.filing_id, 18)}</td>
                      <td>
                        <StateBadge value={f.state} />
                      </td>
                      <td>{formatTs(f.submitted_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </section>
          </div>
        </>
      ) : (
        <p className="subtle">{t("loading")}</p>
      )}

      <section className="panel">
        <h2>
          <DualLabel k="freeze" />
        </h2>
        <p className="notice notice-danger">
          <DualLabel k="freeze_confirm" />
        </p>
        {apprCreated ? (
          <p className="notice">
            Freeze applied. Confirmation pair ApprovalRequest:{" "}
            <Link href={`/approvals/${apprCreated}`} className="mono">
              {apprCreated}
            </Link>
          </p>
        ) : null}
        {error ? <p className="error-text">{error}</p> : null}
        <div className="toolbar">
          <label className="field">
            <span>Subject type</span>
            <select value={subjectType} onChange={(e) => setSubjectType(e.target.value)}>
              <option value="Merchant">Merchant</option>
              <option value="Customer">Customer</option>
            </select>
          </label>
          <label className="field">
            <span>Subject ID</span>
            <input value={subjectId} onChange={(e) => setSubjectId(e.target.value)} aria-label="Freeze subject ID" />
          </label>
          <button className="btn btn-danger" onClick={() => setFreezeOpen(true)} disabled={!subjectId}>
            <DualLabel k="freeze" />
          </button>
        </div>
      </section>

      {freezeOpen ? (
        <TotpModal
          titleKey="freeze"
          messageKey="freeze_confirm"
          danger
          withReason
          busy={busy}
          error={error}
          onConfirm={onConfirm}
          onClose={() => {
            setFreezeOpen(false);
            setError(null);
          }}
        />
      ) : null}
    </Shell>
  );
}

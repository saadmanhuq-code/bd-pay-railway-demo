"use client";

// Connector detail — capabilities, config WITHOUT secret values (OpenBao refs
// only), certification history, breaker state, mode-change request (two-eyes
// notice for PRODUCTION) and circuit reset (spec/10 §API surface).

import React, { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { Shell } from "@/components/Shell";
import { StateBadge } from "@/components/Badges";
import { TotpModal } from "@/components/TotpModal";
import { DualLabel } from "@/lib/i18n/DualLabel";
import { ApiError, getConnector, requestConnectorModeChange, resetCircuit } from "@/lib/api/client";
import { CONNECTOR_MODES, type ConnectorMode, type ConnectorRegistration } from "@/lib/api/types";
import { formatTs } from "@/lib/format";
import { useLang } from "@/lib/i18n/LangContext";

type ModalKind = "mode" | "circuit" | null;

export default function ConnectorDetailPage() {
  const { t } = useLang();
  const params = useParams<{ id: string }>();
  const connectorId = typeof params.id === "string" ? params.id : "";
  const [item, setItem] = useState<ConnectorRegistration | null>(null);
  const [modal, setModal] = useState<ModalKind>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [targetMode, setTargetMode] = useState<ConnectorMode>("SANDBOX");
  const [apprCreated, setApprCreated] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(() => {
    if (connectorId === "") {
      setLoading(false);
      setError("Connector not found.");
      return;
    }
    setLoading(true);
    getConnector(connectorId)
      .then((c) => {
        setItem(c);
        setError(null);
      })
      .catch(() => {
        setItem(null);
        setError("Connector not found.");
      })
      .finally(() => setLoading(false));
  }, [connectorId]);

  useEffect(load, [load]);

  async function onConfirm(totpCode: string, reason: string) {
    if (!item || !modal) return;
    setBusy(true);
    setError(null);
    try {
      if (modal === "circuit") {
        const updated = await resetCircuit(item.connector_id, totpCode);
        setItem(updated);
      } else {
        const res = await requestConnectorModeChange(item.connector_id, targetMode, reason, totpCode);
        if (res.approval_request_id) {
          setApprCreated(res.approval_request_id);
        }
        load();
      }
      setModal(null);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : t("error_generic"));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Shell>
      <h1>{t("nav_connectors")}</h1>
      {item ? (
        <>
          <section className="panel">
            <div className="panel-head">
              <h2>
                {item.display_name} <span className="mono subtle">{item.connector_id}</span>
              </h2>
              <span>
                <StateBadge value={item.active_mode} /> <StateBadge value={item.health_status} />{" "}
                <StateBadge value={item.circuit_state} circuit />
              </span>
            </div>
            <dl className="kv">
              <dt>Registration</dt>
              <dd className="mono">{item.registration_id}</dd>
              <dt>Protocol</dt>
              <dd>{item.protocol}</dd>
              <dt>Capabilities</dt>
              <dd>{item.capabilities.join(", ")}</dd>
              <dt>Supported methods</dt>
              <dd>{item.supported_methods.join(", ")}</dd>
              <dt>Adapter / SDK version</dt>
              <dd>
                {item.adapter_version} / {item.sdk_version}
              </dd>
              <dt>Certification status</dt>
              <dd>
                <StateBadge value={item.certification_status} /> {item.last_certified_at ? formatTs(item.last_certified_at) : ""}
              </dd>
              <dt>Last health</dt>
              <dd>{formatTs(item.last_health_at)}</dd>
            </dl>
          </section>

          <section className="panel">
            <h3>Config (credential values are never returned — OpenBao refs only)</h3>
            <dl className="kv">
              <dt>Base URL</dt>
              <dd className="mono">{item.config.base_url}</dd>
              <dt>Timeout</dt>
              <dd>{item.config.timeout_ms} ms</dd>
              <dt>Credential refs</dt>
              <dd>
                {item.config.credential_refs.map((ref) => (
                  <div key={ref} className="mono">
                    {ref}
                  </div>
                ))}
              </dd>
              <dt>Webhook verify ref</dt>
              <dd className="mono">{item.config.webhook_verify_ref ?? "—"}</dd>
            </dl>
          </section>

          <section className="panel">
            <h3>Certification history</h3>
            {item.certification_history.length === 0 ? (
              <p className="subtle">No certification runs yet</p>
            ) : (
              <table className="data-table">
                <thead>
                  <tr>
                    <th>Run</th>
                    <th>Suite</th>
                    <th>Status</th>
                    <th>Passed</th>
                    <th>Failed</th>
                    <th>Started</th>
                  </tr>
                </thead>
                <tbody>
                  {item.certification_history.map((run) => (
                    <tr key={run.certification_run_id}>
                      <td className="mono">{run.certification_run_id}</td>
                      <td>{run.suite}</td>
                      <td>
                        <StateBadge value={run.status} />
                      </td>
                      <td>{run.cases_passed}</td>
                      <td>{run.cases_failed}</td>
                      <td>{formatTs(run.started_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </section>

          {apprCreated ? (
            <p className="notice">
              ApprovalRequest created (two-eyes):{" "}
              <Link href={`/approvals/${apprCreated}`} className="mono">
                {apprCreated}
              </Link>
            </p>
          ) : null}
          {targetMode === "PRODUCTION" ? (
            <p className="notice">
              <DualLabel k="mode_change_prod_notice" />
            </p>
          ) : null}
          {error ? <p className="error-text">{error}</p> : null}

          <div className="actions-row">
            <label className="field">
              <span>Target mode</span>
              <select value={targetMode} onChange={(e) => setTargetMode(e.target.value as ConnectorMode)}>
                {CONNECTOR_MODES.map((m) => (
                  <option key={m} value={m}>
                    {m}
                  </option>
                ))}
              </select>
            </label>
            <button className="btn btn-primary" onClick={() => setModal("mode")}>
              <DualLabel k="mode_change" />
            </button>
            {item.circuit_state === "OPEN" ? (
              <button className="btn btn-danger" onClick={() => setModal("circuit")}>
                <DualLabel k="reset_circuit" />
              </button>
            ) : null}
          </div>

          {modal ? (
            <TotpModal
              titleKey={modal === "mode" ? "mode_change" : "reset_circuit"}
              messageKey={modal === "circuit" ? "reset_circuit_confirm" : targetMode === "PRODUCTION" ? "mode_change_prod_notice" : "totp_prompt"}
              danger={modal === "circuit" || targetMode === "PRODUCTION"}
              withReason={modal === "mode"}
              busy={busy}
              error={error}
              onConfirm={onConfirm}
              onClose={() => {
                setModal(null);
                setError(null);
              }}
            />
          ) : null}
        </>
      ) : (
        <p className={error ? "error-text" : "subtle"}>{error ?? (loading ? t("loading") : "Connector not found.")}</p>
      )}
    </Shell>
  );
}

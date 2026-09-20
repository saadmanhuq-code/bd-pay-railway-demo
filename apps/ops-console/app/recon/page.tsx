"use client";

// Reconciliation exceptions — filter, drill-in, resolve via case flow.

import React, { useEffect, useState } from "react";
import Link from "next/link";
import { Shell } from "@/components/Shell";
import { DataTable, type Column } from "@bdpay/edge/ui/DataTable";
import { StateBadge } from "@/components/Badges";
import { TotpModal } from "@/components/TotpModal";
import { ApiError, listReconExceptions, resolveReconException } from "@/lib/api/client";
import type { ReconciliationException } from "@/lib/api/types";
import { formatBdt, formatTs, shortId } from "@/lib/format";
import { useLang } from "@/lib/i18n/LangContext";

const KINDS = ["MISSING_AT_RAIL", "MISSING_AT_LEDGER", "AMOUNT_MISMATCH", "DUPLICATE_AT_RAIL", "STATE_MISMATCH"];
const STATES = ["OPEN", "IN_REVIEW", "RESOLVED"];

export default function ReconPage() {
  const { t } = useLang();
  const [rows, setRows] = useState<ReconciliationException[]>([]);
  const [state, setState] = useState("");
  const [kind, setKind] = useState("");
  const [selected, setSelected] = useState<ReconciliationException | null>(null);
  const [resolving, setResolving] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [nonce, setNonce] = useState(0);

  useEffect(() => {
    setLoading(true);
    listReconExceptions({ state, kind })
      .then((res) => setRows(res.data))
      .catch(() => setRows([]))
      .finally(() => setLoading(false));
  }, [state, kind, nonce]);

  async function onConfirm(totpCode: string, reason: string) {
    if (!selected) return;
    setBusy(true);
    setError(null);
    try {
      const updated = await resolveReconException(selected.exception_id, reason, totpCode);
      setSelected(updated);
      setResolving(false);
      setNonce((n) => n + 1);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : t("error_generic"));
    } finally {
      setBusy(false);
    }
  }

  const columns: Column<ReconciliationException>[] = [
    { key: "id", label: "ID", render: (r) => <span className="mono">{shortId(r.exception_id, 18)}</span> },
    { key: "kind", label: "Kind", sortValue: (r) => r.kind, render: (r) => r.kind },
    { key: "connector", label: "Connector", sortValue: (r) => r.connector_id, render: (r) => r.connector_id },
    {
      key: "ledger",
      label: "Ledger amount",
      render: (r) => (r.ledger_amount_minor !== null ? formatBdt(r.ledger_amount_minor) : "—"),
    },
    {
      key: "rail",
      label: "Rail amount",
      render: (r) => (r.rail_amount_minor !== null ? formatBdt(r.rail_amount_minor) : "—"),
    },
    { key: "state", label: "State", sortValue: (r) => r.state, render: (r) => <StateBadge value={r.state} /> },
    { key: "detected", label: "Detected", sortValue: (r) => r.detected_at, render: (r) => formatTs(r.detected_at) },
  ];

  return (
    <Shell>
      <h1>{t("nav_recon")}</h1>
      <div className="toolbar">
        <label className="field">
          <span>State</span>
          <select value={state} onChange={(e) => setState(e.target.value)}>
            <option value="">{t("all")}</option>
            {STATES.map((v) => (
              <option key={v} value={v}>
                {v}
              </option>
            ))}
          </select>
        </label>
        <label className="field">
          <span>Kind</span>
          <select value={kind} onChange={(e) => setKind(e.target.value)}>
            <option value="">{t("all")}</option>
            {KINDS.map((v) => (
              <option key={v} value={v}>
                {v}
              </option>
            ))}
          </select>
        </label>
      </div>
      <div className="panel">
        <DataTable
          columns={columns}
          rows={rows}
          rowKey={(r) => r.exception_id}
          onRowClick={(r) => setSelected(r)}
          empty={loading ? t("loading") : "No exceptions match the filter"}
        />
      </div>

      {selected ? (
        <section className="panel">
          <div className="panel-head">
            <h2 className="mono">{selected.exception_id}</h2>
            <StateBadge value={selected.state} />
          </div>
          <dl className="kv">
            <dt>Recon file</dt>
            <dd className="mono">{selected.recon_file_id}</dd>
            <dt>Connector ref</dt>
            <dd className="mono">{selected.connector_ref ?? "—"}</dd>
            <dt>Linked case</dt>
            <dd>{selected.case_id ? <Link href={`/cases/${selected.case_id}`} className="mono">{selected.case_id}</Link> : "—"}</dd>
            <dt>Resolved</dt>
            <dd>{formatTs(selected.resolved_at)}</dd>
          </dl>
          {error ? <p className="error-text">{error}</p> : null}
          {selected.state !== "RESOLVED" ? (
            <div className="actions-row">
              <button className="btn btn-primary" onClick={() => setResolving(true)}>
                {t("resolve")} (via case flow)
              </button>
            </div>
          ) : null}
        </section>
      ) : null}

      {resolving ? (
        <TotpModal
          titleKey="resolve"
          messageKey="resolve_confirm"
          danger
          withReason
          busy={busy}
          error={error}
          onConfirm={onConfirm}
          onClose={() => {
            setResolving(false);
            setError(null);
          }}
        />
      ) : null}
    </Shell>
  );
}

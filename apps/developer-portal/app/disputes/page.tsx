"use client";

// Merchant-side dispute list with deadline countdown.

import React, { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { ApiError, listDisputes } from "@/lib/api/client";
import type { Dispute, DisputeState } from "@/lib/api/types";
import { StateBadge } from "@/components/Badges";
import { DataTable, type Column } from "@bdpay/edge/ui/DataTable";
import { Shell } from "@/components/Shell";
import { deadlineCountdown, formatBdt, formatTs, shortId } from "@/lib/format";
import { useLang } from "@/lib/i18n/LangContext";
import { useMode } from "@/lib/mode";

const STATES: DisputeState[] = [
  "OPEN",
  "EVIDENCE_PENDING",
  "UNDER_REVIEW",
  "RESOLVED_MERCHANT",
  "RESOLVED_CUSTOMER",
  "CHARGEBACK_FILED",
  "CHARGEBACK_CLOSED",
  "WITHDRAWN",
];

export default function DisputesPage() {
  const { t } = useLang();
  const { env } = useMode();
  const router = useRouter();
  const [rows, setRows] = useState<Dispute[]>([]);
  const [state, setState] = useState("");
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    listDisputes({ state, env })
      .then((page) => {
        setRows(page.data);
        setError(null);
      })
      .catch((err) => setError(err instanceof ApiError ? err.message : t("error_generic")));
  }, [state, env, t]);

  useEffect(() => {
    load();
  }, [load]);

  const columns: Column<Dispute>[] = [
    { key: "id", label: "Dispute", render: (r) => <span className="mono">{shortId(r.dispute_id, 18)}</span> },
    { key: "intent", label: "Intent", render: (r) => <span className="mono">{shortId(r.payment_intent_id, 18)}</span> },
    {
      key: "amount",
      label: "Amount",
      sortValue: (r) => Number(BigInt(r.amount_minor)),
      render: (r) => formatBdt(r.amount_minor),
    },
    { key: "method", label: "Method", sortValue: (r) => r.method, render: (r) => r.method },
    { key: "reason", label: "Reason", render: (r) => r.reason_code },
    { key: "state", label: "State", sortValue: (r) => r.state, render: (r) => <StateBadge value={r.state} /> },
    {
      key: "deadline",
      label: t("evidence_deadline"),
      sortValue: (r) => r.evidence_deadline_at ?? "",
      render: (r) => {
        const cd = deadlineCountdown(r.evidence_deadline_at);
        if (!cd) return "—";
        return (
          <span className={`deadline ${cd.late ? "deadline-late" : ""}`}>
            {cd.label}
            <div className="subtle">{formatTs(r.evidence_deadline_at)}</div>
          </span>
        );
      },
    },
    { key: "opened", label: "Opened", sortValue: (r) => r.opened_at, render: (r) => formatTs(r.opened_at) },
  ];

  return (
    <Shell>
      <h1>{t("nav_disputes")}</h1>
      {error ? <p className="error-text">{error}</p> : null}
      <div className="toolbar">
        <label className="field">
          <span>State</span>
          <select value={state} onChange={(e) => setState(e.target.value)}>
            <option value="">All</option>
            {STATES.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        </label>
        <button className="btn btn-small" onClick={load}>
          {t("refresh")}
        </button>
      </div>
      <DataTable
        columns={columns}
        rows={rows}
        rowKey={(r) => r.dispute_id}
        onRowClick={(r) => router.push(`/disputes/${encodeURIComponent(r.dispute_id)}`)}
      />
    </Shell>
  );
}

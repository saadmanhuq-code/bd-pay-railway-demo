"use client";

import React, { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { Shell } from "@/components/Shell";
import { DataTable, type Column } from "@bdpay/edge/ui/DataTable";
import { StateBadge } from "@/components/Badges";
import { listDisputes } from "@/lib/api/client";
import { DISPUTE_STATES, type Dispute } from "@/lib/api/types";
import { formatBdt, formatTs, shortId } from "@/lib/format";
import { useLang } from "@/lib/i18n/LangContext";

const METHODS = ["BKASH", "NAGAD", "CARD", "NPSB_IBFT", "BANGLA_QR"];

export default function DisputesPage() {
  const { t } = useLang();
  const router = useRouter();
  const [rows, setRows] = useState<Dispute[]>([]);
  const [state, setState] = useState("");
  const [method, setMethod] = useState("");
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    setLoading(true);
    listDisputes({ state, method })
      .then((res) => setRows(res.data))
      .catch(() => setRows([]))
      .finally(() => setLoading(false));
  }, [state, method]);

  const columns: Column<Dispute>[] = [
    { key: "id", label: "ID", render: (r) => <span className="mono">{shortId(r.dispute_id, 18)}</span> },
    { key: "merchant", label: "Merchant", sortValue: (r) => r.merchant_name, render: (r) => r.merchant_name },
    { key: "amount", label: "Amount", sortValue: (r) => BigInt(r.amount_minor).toString().padStart(20, "0"), render: (r) => formatBdt(r.amount_minor) },
    { key: "method", label: "Method", sortValue: (r) => r.method, render: (r) => r.method },
    { key: "reason", label: "Reason", sortValue: (r) => r.reason_code, render: (r) => r.reason_code },
    { key: "state", label: "State", sortValue: (r) => r.state, render: (r) => <StateBadge value={r.state} /> },
    { key: "opened", label: "Opened", sortValue: (r) => r.opened_at, render: (r) => formatTs(r.opened_at) },
  ];

  return (
    <Shell>
      <h1>{t("nav_disputes")}</h1>
      <div className="toolbar">
        <label className="field">
          <span>State</span>
          <select value={state} onChange={(e) => setState(e.target.value)}>
            <option value="">{t("all")}</option>
            {DISPUTE_STATES.map((v) => (
              <option key={v} value={v}>
                {v}
              </option>
            ))}
          </select>
        </label>
        <label className="field">
          <span>Method</span>
          <select value={method} onChange={(e) => setMethod(e.target.value)}>
            <option value="">{t("all")}</option>
            {METHODS.map((v) => (
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
          rowKey={(r) => r.dispute_id}
          onRowClick={(r) => router.push(`/disputes/${r.dispute_id}`)}
          empty={loading ? t("loading") : "No disputes match the filter"}
        />
      </div>
    </Shell>
  );
}

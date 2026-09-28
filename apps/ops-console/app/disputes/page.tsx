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
import { enumLabel } from "@/lib/i18n/enums";

const METHODS = ["BKASH", "NAGAD", "CARD", "NPSB_IBFT", "BANGLA_QR"];

export default function DisputesPage() {
  const { t, lang } = useLang();
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
    { key: "id", label: t("col_id"), render: (r) => <span className="mono">{shortId(r.dispute_id, 18)}</span> },
    { key: "merchant", label: t("col_merchant"), sortValue: (r) => r.merchant_name, render: (r) => r.merchant_name },
    { key: "amount", label: t("col_amount"), sortValue: (r) => BigInt(r.amount_minor).toString().padStart(20, "0"), render: (r) => formatBdt(r.amount_minor) },
    { key: "method", label: t("col_method"), sortValue: (r) => r.method, render: (r) => <span title={r.method}>{enumLabel(r.method, lang)}</span> },
    { key: "reason", label: t("col_reason"), sortValue: (r) => r.reason_code, render: (r) => <span title={r.reason_code}>{enumLabel(r.reason_code, lang)}</span> },
    { key: "state", label: t("col_state"), sortValue: (r) => r.state, render: (r) => <StateBadge value={r.state} /> },
    { key: "opened", label: t("col_opened"), sortValue: (r) => r.opened_at, render: (r) => formatTs(r.opened_at) },
  ];

  return (
    <Shell>
      <h1>{t("nav_disputes")}</h1>
      <div className="toolbar">
        <label className="field">
          <span>{t("col_state")}</span>
          <select value={state} onChange={(e) => setState(e.target.value)}>
            <option value="">{t("all")}</option>
            {DISPUTE_STATES.map((v) => (
              <option key={v} value={v}>
                {enumLabel(v, lang)}
              </option>
            ))}
          </select>
        </label>
        <label className="field">
          <span>{t("col_method")}</span>
          <select value={method} onChange={(e) => setMethod(e.target.value)}>
            <option value="">{t("all")}</option>
            {METHODS.map((v) => (
              <option key={v} value={v}>
                {enumLabel(v, lang)}
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
          empty={loading ? t("loading") : t("empty_disputes")}
        />
      </div>
    </Shell>
  );
}

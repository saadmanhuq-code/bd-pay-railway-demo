"use client";

import React, { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { Shell } from "@/components/Shell";
import { DataTable, type Column } from "@bdpay/edge/ui/DataTable";
import { StateBadge } from "@/components/Badges";
import { listCompensation } from "@/lib/api/client";
import { COMPENSATION_STATES, type CompensationQueueItem } from "@/lib/api/types";
import { formatBdt, formatTs, shortId } from "@/lib/format";
import { useLang } from "@/lib/i18n/LangContext";
import { enumLabel } from "@/lib/i18n/enums";

export default function CompensationPage() {
  const { t, lang } = useLang();
  const router = useRouter();
  const [rows, setRows] = useState<CompensationQueueItem[]>([]);
  const [state, setState] = useState("");
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    setLoading(true);
    listCompensation({ state })
      .then((res) => setRows(res.data))
      .catch(() => setRows([]))
      .finally(() => setLoading(false));
  }, [state]);

  const columns: Column<CompensationQueueItem>[] = [
    { key: "id", label: t("col_id"), render: (r) => <span className="mono">{shortId(r.compensation_id, 18)}</span> },
    { key: "connector", label: t("col_connector"), sortValue: (r) => r.connector_id, render: (r) => r.connector_id },
    { key: "ref", label: t("col_connector_ref"), render: (r) => <span className="mono">{r.connector_ref}</span> },
    {
      key: "amount",
      label: t("col_amount"),
      sortValue: (r) => BigInt(r.amount_minor).toString().padStart(20, "0"),
      render: (r) => formatBdt(r.amount_minor),
    },
    { key: "state", label: t("col_state"), sortValue: (r) => r.state, render: (r) => <StateBadge value={r.state} /> },
    { key: "kind", label: t("col_resolution_kind"), render: (r) => enumLabel(r.resolution_kind, lang) },
    { key: "queued", label: t("col_queued"), sortValue: (r) => r.queued_at, render: (r) => formatTs(r.queued_at) },
  ];

  return (
    <Shell>
      <h1>{t("nav_compensation")}</h1>
      <p className="notice notice-danger">{t("comp_queue_notice")}</p>
      <div className="toolbar">
        <label className="field">
          <span>{t("col_state")}</span>
          <select value={state} onChange={(e) => setState(e.target.value)}>
            <option value="">{t("all")}</option>
            {COMPENSATION_STATES.map((v) => (
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
          rowKey={(r) => r.compensation_id}
          onRowClick={(r) => router.push(`/compensation/${r.compensation_id}`)}
          rowClassName={(r) => (r.state === "QUEUED" ? "row-p0" : "")}
          empty={loading ? t("loading") : t("empty_compensation")}
        />
      </div>
    </Shell>
  );
}

"use client";

// Deterministic report downloads — sha256 manifest values plus the
// X-BDPay-Content-Sha256 contract explanation (spec/15 §Dashboards & exports).

import React, { useEffect, useState } from "react";
import { Shell } from "@/components/Shell";
import { DataTable, type Column } from "@bdpay/edge/ui/DataTable";
import { DualLabel } from "@/lib/i18n/DualLabel";
import { listReports } from "@/lib/api/client";
import type { ReportItem } from "@/lib/api/types";
import { formatTs } from "@/lib/format";
import { useLang } from "@/lib/i18n/LangContext";

export default function ExportsPage() {
  const { t } = useLang();
  const [rows, setRows] = useState<ReportItem[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    listReports()
      .then((res) => setRows(res.data))
      .catch(() => setRows([]))
      .finally(() => setLoading(false));
  }, []);

  const columns: Column<ReportItem>[] = [
    { key: "kind", label: "Kind", sortValue: (r) => r.kind, render: (r) => r.kind },
    { key: "label", label: "Report", sortValue: (r) => r.label, render: (r) => r.label },
    { key: "generated", label: "Generated", sortValue: (r) => r.generated_at, render: (r) => formatTs(r.generated_at) },
    { key: "size", label: "Size", sortValue: (r) => r.size_bytes, render: (r) => `${r.size_bytes.toLocaleString("en-US")} B` },
    { key: "sha", label: "SHA-256 (manifest)", render: (r) => <span className="mono">{r.sha256}</span> },
  ];

  return (
    <Shell>
      <h1>{t("nav_exports")}</h1>
      <p className="notice">
        <DualLabel k="exports_sha_note" />
      </p>
      <div className="panel">
        <DataTable columns={columns} rows={rows} rowKey={(r) => r.report_id} empty={loading ? t("loading") : "No reports"} />
      </div>
    </Shell>
  );
}

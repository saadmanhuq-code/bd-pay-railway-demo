"use client";

// Settlement / recon report downloads with sha256 and the
// X-BDPay-Content-Sha256 deterministic-download contract note.

import React, { useCallback, useEffect, useState } from "react";
import { ApiError, listReports } from "@/lib/api/client";
import type { ReportItem } from "@/lib/api/types";
import { EnvBadge } from "@/components/Badges";
import { DataTable, type Column } from "@bdpay/edge/ui/DataTable";
import { Shell } from "@/components/Shell";
import { formatTs, groupThousands, shortId } from "@/lib/format";
import { useLang } from "@/lib/i18n/LangContext";
import { useMode } from "@/lib/mode";

function downloadReport(r: ReportItem) {
  // The mock has no file-bytes endpoint; downloads serve a deterministic
  // manifest text derived from the report metadata so the sha256 contract is
  // visible end to end.
  const body = [
    `report_id: ${r.report_id}`,
    `kind: ${r.kind}`,
    `label: ${r.label}`,
    `generated_at: ${r.generated_at}`,
    `sha256 (manifest): ${r.sha256}`,
    `note: in production the X-BDPay-Content-Sha256 response header carries the SHA-256 of the exact served bytes.`,
  ].join("\n");
  const blob = new Blob([body + "\n"], { type: "text/plain" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `${r.report_id}.txt`;
  a.click();
  URL.revokeObjectURL(url);
}

export default function ReportsPage() {
  const { t } = useLang();
  const { env } = useMode();
  const [rows, setRows] = useState<ReportItem[]>([]);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    listReports(env)
      .then((page) => {
        setRows(page.data);
        setError(null);
      })
      .catch((err) => setError(err instanceof ApiError ? err.message : t("error_generic")));
  }, [env, t]);

  useEffect(() => {
    load();
  }, [load]);

  const columns: Column<ReportItem>[] = [
    { key: "kind", label: "Kind", sortValue: (r) => r.kind, render: (r) => <span className="badge">{r.kind}</span> },
    { key: "label", label: "Report", sortValue: (r) => r.label, render: (r) => r.label },
    { key: "generated", label: "Generated", sortValue: (r) => r.generated_at, render: (r) => formatTs(r.generated_at) },
    { key: "size", label: "Size (bytes)", sortValue: (r) => r.size_bytes, render: (r) => groupThousands(String(r.size_bytes)) },
    { key: "sha", label: "SHA-256", render: (r) => <span className="mono">{shortId(r.sha256, 24)}</span> },
    {
      key: "dl",
      label: "",
      render: (r) => (
        <button className="btn btn-small" onClick={() => downloadReport(r)}>
          {t("download")}
        </button>
      ),
    },
  ];

  return (
    <Shell>
      <h1>{t("nav_reports")}</h1>
      {error ? <p className="error-text">{error}</p> : null}
      <div className="toolbar">
        <EnvBadge env={env} />
      </div>
      <p className="notice">{t("reports_sha_note")}</p>
      <DataTable columns={columns} rows={rows} rowKey={(r) => r.report_id} empty="No reports for this environment" />
    </Shell>
  );
}

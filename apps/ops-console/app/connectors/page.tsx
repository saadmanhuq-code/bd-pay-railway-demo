"use client";

import React, { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { Shell } from "@/components/Shell";
import { DataTable, type Column } from "@bdpay/edge/ui/DataTable";
import { StateBadge } from "@/components/Badges";
import { listConnectors } from "@/lib/api/client";
import type { ConnectorRegistration } from "@/lib/api/types";
import { formatTs } from "@/lib/format";
import { useLang } from "@/lib/i18n/LangContext";
import { enumLabel } from "@/lib/i18n/enums";

export default function ConnectorsPage() {
  const { t, lang } = useLang();
  const router = useRouter();
  const [rows, setRows] = useState<ConnectorRegistration[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    listConnectors({})
      .then((res) => setRows(res.data))
      .catch(() => setRows([]))
      .finally(() => setLoading(false));
  }, []);

  const columns: Column<ConnectorRegistration>[] = [
    { key: "id", label: t("col_connector"), sortValue: (r) => r.connector_id, render: (r) => <span className="mono">{r.connector_id}</span> },
    { key: "name", label: t("col_name"), sortValue: (r) => r.display_name, render: (r) => r.display_name },
    { key: "protocol", label: t("col_protocol"), sortValue: (r) => r.protocol, render: (r) => r.protocol },
    { key: "mode", label: t("col_mode"), sortValue: (r) => r.active_mode, render: (r) => <StateBadge value={r.active_mode} /> },
    { key: "health", label: t("col_health"), sortValue: (r) => r.health_status, render: (r) => <StateBadge value={r.health_status} /> },
    { key: "circuit", label: t("col_circuit"), sortValue: (r) => r.circuit_state, render: (r) => <StateBadge value={r.circuit_state} circuit /> },
    { key: "cert", label: t("col_certification"), sortValue: (r) => r.certification_status, render: (r) => <StateBadge value={r.certification_status} /> },
    { key: "health_at", label: t("col_last_health"), sortValue: (r) => r.last_health_at, render: (r) => formatTs(r.last_health_at) },
  ];

  return (
    <Shell>
      <h1>{t("nav_connectors")}</h1>
      <div className="panel">
        <DataTable
          columns={columns}
          rows={rows}
          rowKey={(r) => r.connector_id}
          onRowClick={(r) => router.push(`/connectors/${r.connector_id}`)}
          empty={loading ? t("loading") : t("empty_connectors")}
        />
      </div>
    </Shell>
  );
}

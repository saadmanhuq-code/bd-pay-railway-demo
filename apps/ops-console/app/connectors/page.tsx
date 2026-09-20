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

export default function ConnectorsPage() {
  const { t } = useLang();
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
    { key: "id", label: "Connector", sortValue: (r) => r.connector_id, render: (r) => <span className="mono">{r.connector_id}</span> },
    { key: "name", label: "Name", sortValue: (r) => r.display_name, render: (r) => r.display_name },
    { key: "protocol", label: "Protocol", sortValue: (r) => r.protocol, render: (r) => r.protocol },
    { key: "mode", label: "Mode", sortValue: (r) => r.active_mode, render: (r) => <StateBadge value={r.active_mode} /> },
    { key: "health", label: "Health", sortValue: (r) => r.health_status, render: (r) => <StateBadge value={r.health_status} /> },
    { key: "circuit", label: "Circuit", sortValue: (r) => r.circuit_state, render: (r) => <StateBadge value={r.circuit_state} circuit /> },
    { key: "cert", label: "Certification", sortValue: (r) => r.certification_status, render: (r) => <StateBadge value={r.certification_status} /> },
    { key: "health_at", label: "Last health", sortValue: (r) => r.last_health_at, render: (r) => formatTs(r.last_health_at) },
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
          empty={loading ? t("loading") : "No connectors registered"}
        />
      </div>
    </Shell>
  );
}

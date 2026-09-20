"use client";

import React, { useEffect, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { Shell } from "@/components/Shell";
import { DataTable, type Column } from "@bdpay/edge/ui/DataTable";
import { PriorityBadge, SlaBadge, StateBadge } from "@/components/Badges";
import { listCases } from "@/lib/api/client";
import { OPS_CASE_STATES, OPS_CASE_TYPES, type OpsCase } from "@/lib/api/types";
import { formatTs, shortId } from "@/lib/format";
import { useLang } from "@/lib/i18n/LangContext";

export default function CasesPage() {
  const { t } = useLang();
  const router = useRouter();
  const [rows, setRows] = useState<OpsCase[]>([]);
  const [type, setType] = useState("");
  const [state, setState] = useState("");
  const [priority, setPriority] = useState("");
  const [slaOnly, setSlaOnly] = useState(false);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    setLoading(true);
    listCases({ type, state, priority, sla: slaOnly ? "breached" : "" })
      .then((res) => setRows(res.data))
      .catch(() => setRows([]))
      .finally(() => setLoading(false));
  }, [type, state, priority, slaOnly]);

  const columns: Column<OpsCase>[] = [
    {
      key: "id",
      label: "ID",
      render: (r) => (
        <Link href={`/cases/${encodeURIComponent(r.case_id)}`} className="mono case-id-link" onClick={(e) => e.stopPropagation()}>
          {shortId(r.case_id, 18)}
        </Link>
      ),
    },
    { key: "type", label: "Type", sortValue: (r) => r.case_type, render: (r) => r.case_type },
    { key: "priority", label: "Priority", sortValue: (r) => r.priority, render: (r) => <PriorityBadge value={r.priority} /> },
    { key: "state", label: "State", sortValue: (r) => r.state, render: (r) => <StateBadge value={r.state} /> },
    {
      key: "subject",
      label: "Subject",
      render: (r) => (
        <span className="case-subject">
          {r.subject_type} <span className="mono subtle">{shortId(r.subject_id, 16)}</span>
        </span>
      ),
    },
    { key: "assignee", label: "Assignee", sortValue: (r) => r.assignee_name ?? "", render: (r) => r.assignee_name ?? "—" },
    {
      key: "sla",
      label: "SLA due",
      sortValue: (r) => r.sla_due_at,
      render: (r) => (
        <span className="case-sla">
          {formatTs(r.sla_due_at)} <SlaBadge breached={r.sla_breached} />
        </span>
      ),
    },
    { key: "opened", label: "Opened", sortValue: (r) => r.opened_at, render: (r) => formatTs(r.opened_at) },
  ];

  return (
    <Shell>
      <h1>{t("nav_cases")}</h1>
      <div className="toolbar">
        <label className="field">
          <span>Type</span>
          <select value={type} onChange={(e) => setType(e.target.value)}>
            <option value="">{t("all")}</option>
            {OPS_CASE_TYPES.map((v) => (
              <option key={v} value={v}>
                {v}
              </option>
            ))}
          </select>
        </label>
        <label className="field">
          <span>State</span>
          <select value={state} onChange={(e) => setState(e.target.value)}>
            <option value="">{t("all")}</option>
            {OPS_CASE_STATES.map((v) => (
              <option key={v} value={v}>
                {v}
              </option>
            ))}
          </select>
        </label>
        <label className="field">
          <span>Priority</span>
          <select value={priority} onChange={(e) => setPriority(e.target.value)}>
            <option value="">{t("all")}</option>
            {["P0", "P1", "P2", "P3"].map((v) => (
              <option key={v} value={v}>
                {v}
              </option>
            ))}
          </select>
        </label>
        <label className="field">
          <span>{t("sla_breached")}</span>
          <select value={slaOnly ? "yes" : ""} onChange={(e) => setSlaOnly(e.target.value === "yes")}>
            <option value="">{t("all")}</option>
            <option value="yes">{t("sla_breached")}</option>
          </select>
        </label>
      </div>
      <div className="panel panel-table-scroll">
        <DataTable
          columns={columns}
          rows={rows}
          rowKey={(r) => r.case_id}
          onRowClick={(r) => router.push(`/cases/${encodeURIComponent(r.case_id)}`)}
          rowClassName={(r) => (r.priority === "P0" && r.state !== "RESOLVED" && r.state !== "CANCELLED" ? "row-p0" : "")}
          empty={loading ? t("loading") : "No cases match the filter"}
        />
      </div>
    </Shell>
  );
}

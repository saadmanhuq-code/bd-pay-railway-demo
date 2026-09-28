"use client";

import React, { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { Shell } from "@/components/Shell";
import { DataTable, type Column } from "@bdpay/edge/ui/DataTable";
import { StateBadge } from "@/components/Badges";
import { listApprovals } from "@/lib/api/client";
import { APPROVAL_ACTION_TYPES, APPROVAL_STATES, type ApprovalRequest } from "@/lib/api/types";
import { formatTs, shortId } from "@/lib/format";
import { useLang } from "@/lib/i18n/LangContext";
import { enumLabel, humanizeEnum } from "@/lib/i18n/enums";

export default function ApprovalsPage() {
  const { t, lang } = useLang();
  const router = useRouter();
  const [rows, setRows] = useState<ApprovalRequest[]>([]);
  const [state, setState] = useState("PENDING_SECOND_APPROVER");
  const [actionType, setActionType] = useState("");
  const [mine, setMine] = useState("");
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    setLoading(true);
    listApprovals({ state, action_type: actionType, mine: mine as "" | "initiated" | "awaiting_me" })
      .then((res) => setRows(res.data))
      .catch(() => setRows([]))
      .finally(() => setLoading(false));
  }, [state, actionType, mine]);

  const columns: Column<ApprovalRequest>[] = [
    {
      key: "id",
      label: t("col_id"),
      render: (r) => <span className="mono">{shortId(r.approval_request_id, 18)}</span>,
    },
    {
      key: "action",
      label: t("col_action_type"),
      sortValue: (r) => r.action_type,
      render: (r) => <span title={r.action_type}>{enumLabel(r.action_type, lang)}</span>,
    },
    {
      key: "subject",
      label: t("col_subject"),
      sortValue: (r) => r.subject_type,
      render: (r) => (
        <span>
          {humanizeEnum(r.subject_type)} <span className="mono subtle">{shortId(r.subject_id, 16)}</span>
        </span>
      ),
    },
    {
      key: "state",
      label: t("col_state"),
      sortValue: (r) => r.state,
      render: (r) => <StateBadge value={r.state} />,
    },
    {
      key: "initiator",
      label: t("col_initiator"),
      sortValue: (r) => r.initiator_name,
      render: (r) => r.initiator_name,
    },
    {
      key: "created",
      label: t("col_created"),
      sortValue: (r) => r.created_at,
      render: (r) => formatTs(r.created_at),
    },
    {
      key: "expires",
      label: t("col_expires"),
      sortValue: (r) => r.expires_at,
      render: (r) => formatTs(r.expires_at),
    },
  ];

  return (
    <Shell>
      <h1>{t("nav_approvals")}</h1>
      <div className="toolbar">
        <label className="field">
          <span>{t("col_state")}</span>
          <select value={state} onChange={(e) => setState(e.target.value)}>
            <option value="">{t("all")}</option>
            {APPROVAL_STATES.map((s) => (
              <option key={s} value={s}>
                {enumLabel(s, lang)}
              </option>
            ))}
          </select>
        </label>
        <label className="field">
          <span>{t("col_action_type")}</span>
          <select value={actionType} onChange={(e) => setActionType(e.target.value)}>
            <option value="">{t("all")}</option>
            {APPROVAL_ACTION_TYPES.map((a) => (
              <option key={a} value={a}>
                {enumLabel(a, lang)}
              </option>
            ))}
          </select>
        </label>
        <label className="field">
          <span>{t("col_mine")}</span>
          <select value={mine} onChange={(e) => setMine(e.target.value)}>
            <option value="">{t("all")}</option>
            <option value="initiated">{t("mine_initiated")}</option>
            <option value="awaiting_me">{t("mine_awaiting")}</option>
          </select>
        </label>
      </div>
      <div className="panel">
        <DataTable
          columns={columns}
          rows={rows}
          rowKey={(r) => r.approval_request_id}
          onRowClick={(r) => router.push(`/approvals/${r.approval_request_id}`)}
          empty={loading ? t("loading") : t("empty_approvals")}
        />
      </div>
    </Shell>
  );
}

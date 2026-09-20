"use client";

// Operator IAM — roles, credential_state, locked_until, disable action, and
// the role-matrix reference table rendered from a const (spec/15 §Role matrix).

import React, { useEffect, useState } from "react";
import { Shell } from "@/components/Shell";
import { DataTable, type Column } from "@bdpay/edge/ui/DataTable";
import { StateBadge } from "@/components/Badges";
import { TotpModal } from "@/components/TotpModal";
import { DualLabel } from "@/lib/i18n/DualLabel";
import { ApiError, disableOperator, listOperators } from "@/lib/api/client";
import { OPERATOR_ROLES, type Operator } from "@/lib/api/types";
import { formatTs } from "@/lib/format";
import { useLang } from "@/lib/i18n/LangContext";

// Role-matrix reference (subset of spec/15 §Role matrix; ✓(pair) = two
// distinct holders via ApprovalRequest; deny-overrides-allow ACL on top).
const ROLE_MATRIX: { action: string; marks: [string, string, string, string, string, string, string] }[] = [
  { action: "View dashboards", marks: ["✓", "✓", "✓", "✓", "✓", "✓", "—"] },
  { action: "View pre-redaction audit payloads", marks: ["—", "—", "—", "—", "—", "—", "✓ (audited)"] },
  { action: "Initiate refund > threshold", marks: ["✓", "—", "—", "✓", "✓", "—", "—"] },
  { action: "Approve refund > threshold", marks: ["—", "—", "—", "✓", "✓", "—", "—"] },
  { action: "Merchant activation initiate / approve", marks: ["✓ / —", "— / ✓", "— / ✓", "—", "✓/✓*", "—", "—"] },
  { action: "KYC manual review decide", marks: ["—", "✓ (pair)", "✓", "—", "—", "—", "—"] },
  { action: "AML alert disposition / case close", marks: ["—", "✓ init", "✓ approve", "—", "—", "—", "—"] },
  { action: "Sanctions clear/unfreeze", marks: ["—", "✓ init", "✓ approve", "—", "—", "—", "—"] },
  { action: "Freeze (CAMLCO)", marks: ["—", "—", "✓", "—", "confirm pair", "—", "—"] },
  { action: "Settlement batch retry / release override", marks: ["—", "—", "—", "✓ (pair)", "✓", "—", "—"] },
  { action: "Recon ledger adjustment", marks: ["—", "—", "—", "✓ (pair)", "✓", "—", "—"] },
  { action: "Compensation queue resolve", marks: ["—", "—", "—", "✓ (pair)", "✓", "—", "—"] },
  { action: "Dispute triage / resolve-money", marks: ["✓ / init", "—", "—", "approve", "✓", "—", "—"] },
  { action: "Connector mode change (non-prod) / → PRODUCTION", marks: ["✓ / init", "—", "—", "—", "✓ / approve", "—", "—"] },
  { action: "Operator create / role change", marks: ["—", "—", "—", "—", "✓ (pair)", "—", "—"] },
  { action: "View audit chain verify status", marks: ["✓", "✓", "✓", "✓", "✓", "✓", "✓"] },
  { action: "Export BB/BFIU evidence pack", marks: ["—", "✓", "✓", "—", "✓", "✓", "✓"] },
];

export default function OperatorsPage() {
  const { t } = useLang();
  const [rows, setRows] = useState<Operator[]>([]);
  const [target, setTarget] = useState<Operator | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [nonce, setNonce] = useState(0);

  useEffect(() => {
    setLoading(true);
    listOperators({})
      .then((res) => setRows(res.data))
      .catch(() => setRows([]))
      .finally(() => setLoading(false));
  }, [nonce]);

  async function onConfirm(totpCode: string, reason: string) {
    if (!target) return;
    setBusy(true);
    setError(null);
    try {
      await disableOperator(target.operator_id, reason, totpCode);
      setTarget(null);
      setNonce((n) => n + 1);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : t("error_generic"));
    } finally {
      setBusy(false);
    }
  }

  const columns: Column<Operator>[] = [
    { key: "name", label: "Operator", sortValue: (r) => r.display_name, render: (r) => r.display_name },
    { key: "email", label: "Email", sortValue: (r) => r.email, render: (r) => <span className="mono">{r.email}</span> },
    {
      key: "roles",
      label: "Roles",
      render: (r) => (
        <span>
          {r.roles.map((role) => (
            <span key={role} className="badge" style={{ marginRight: 4 }}>
              {role}
            </span>
          ))}
        </span>
      ),
    },
    { key: "state", label: "Credential state", sortValue: (r) => r.credential_state, render: (r) => <StateBadge value={r.credential_state} /> },
    { key: "locked", label: "Locked until", render: (r) => formatTs(r.locked_until) },
    { key: "pw", label: "Password set (90d max age)", sortValue: (r) => r.password_set_at, render: (r) => formatTs(r.password_set_at) },
    {
      key: "actions",
      label: "",
      render: (r) =>
        r.credential_state !== "DISABLED" ? (
          <button
            className="btn btn-small btn-danger"
            onClick={(e) => {
              e.stopPropagation();
              setTarget(r);
            }}
          >
            {t("disable_operator")}
          </button>
        ) : null,
    },
  ];

  return (
    <Shell>
      <h1>{t("nav_operators")}</h1>
      <div className="panel">
        <DataTable columns={columns} rows={rows} rowKey={(r) => r.operator_id} empty={loading ? t("loading") : "No operators"} />
      </div>

      <section className="panel">
        <h2>Role matrix reference (least-privilege; explicit-deny wins)</h2>
        <table className="data-table">
          <thead>
            <tr>
              <th>Action</th>
              {OPERATOR_ROLES.map((r) => (
                <th key={r}>{r}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {ROLE_MATRIX.map((row) => (
              <tr key={row.action}>
                <td>{row.action}</td>
                {row.marks.map((m, i) => (
                  <td key={i}>{m}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      {target ? (
        <TotpModal
          titleKey="disable_operator"
          messageKey="disable_operator_confirm"
          danger
          withReason
          busy={busy}
          error={error}
          onConfirm={onConfirm}
          onClose={() => {
            setTarget(null);
            setError(null);
          }}
        />
      ) : null}
    </Shell>
  );
}

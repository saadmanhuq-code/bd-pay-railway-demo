"use client";

import React, { useCallback, useEffect, useState } from "react";
import { useParams } from "next/navigation";
import { Shell } from "@/components/Shell";
import { PriorityBadge, SlaBadge, StateBadge } from "@/components/Badges";
import { TotpModal } from "@/components/TotpModal";
import { DualLabel } from "@/lib/i18n/DualLabel";
import {
  ApiError,
  assignCase,
  attachCaseEvidence,
  commentCase,
  getCase,
  resolveCase,
} from "@/lib/api/client";
import type { OpsCase } from "@/lib/api/types";
import { formatTs } from "@/lib/format";
import { useLang } from "@/lib/i18n/LangContext";
import { useSession } from "@/lib/useSession";

type ModalKind = "assign" | "comment" | "evidence" | "resolve" | null;

export default function CaseDetailPage() {
  const { t } = useLang();
  const { session } = useSession();
  const params = useParams<{ id: string }>();
  const caseId = typeof params.id === "string" ? params.id : "";
  const [item, setItem] = useState<OpsCase | null>(null);
  const [modal, setModal] = useState<ModalKind>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [evPointer, setEvPointer] = useState("");
  const [evSha, setEvSha] = useState("");
  const [evLabel, setEvLabel] = useState("");
  const [loading, setLoading] = useState(true);

  const load = useCallback(() => {
    if (caseId === "") {
      setLoading(false);
      setError("Case not found.");
      return;
    }
    setLoading(true);
    getCase(caseId)
      .then((c) => {
        setItem(c);
        setError(null);
      })
      .catch(() => {
        setItem(null);
        setError("Case not found.");
      })
      .finally(() => setLoading(false));
  }, [caseId]);

  useEffect(load, [load]);

  async function onConfirm(totpCode: string, reason: string) {
    if (!item || !modal || !session) return;
    setBusy(true);
    setError(null);
    try {
      const updated =
        modal === "assign"
          ? await assignCase(item.case_id, session.operator.operator_id, totpCode)
          : modal === "comment"
            ? await commentCase(item.case_id, reason, totpCode)
            : modal === "evidence"
              ? await attachCaseEvidence(item.case_id, evPointer, evSha, evLabel, totpCode)
              : await resolveCase(item.case_id, reason, totpCode);
      setItem(updated);
      setModal(null);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : t("error_generic"));
    } finally {
      setBusy(false);
    }
  }

  const open = item !== null && (item.state === "OPEN" || item.state === "IN_PROGRESS" || item.state === "WAITING_EXTERNAL");

  return (
    <Shell>
      <h1>{t("nav_cases")}</h1>
      {item ? (
        <>
          <section className="panel">
            <div className="panel-head">
              <h2 className="mono">{item.case_id}</h2>
              <span>
                <PriorityBadge value={item.priority} /> <StateBadge value={item.state} /> <SlaBadge breached={item.sla_breached} />
              </span>
            </div>
            <dl className="kv">
              <dt>Type</dt>
              <dd>{item.case_type}</dd>
              <dt>Subject</dt>
              <dd>
                {item.subject_type} <span className="mono">{item.subject_id}</span>
              </dd>
              <dt>Assignee</dt>
              <dd>{item.assignee_name ?? "—"}</dd>
              <dt>SLA due</dt>
              <dd>{formatTs(item.sla_due_at)}</dd>
              <dt>Opened</dt>
              <dd>{formatTs(item.opened_at)}</dd>
              <dt>Resolved</dt>
              <dd>{formatTs(item.resolved_at)}</dd>
              <dt>Resolution note</dt>
              <dd>{item.resolution_note ?? "—"}</dd>
            </dl>
          </section>

          <section className="panel">
            <h3>Evidence (pointer + sha256 — hash recorded in audit_events)</h3>
            {item.evidence.length === 0 ? (
              <p className="subtle">No evidence attached</p>
            ) : (
              <table className="data-table">
                <thead>
                  <tr>
                    <th>Label</th>
                    <th>Pointer</th>
                    <th>SHA-256</th>
                    <th>Added</th>
                  </tr>
                </thead>
                <tbody>
                  {item.evidence.map((e) => (
                    <tr key={e.sha256}>
                      <td>{e.label}</td>
                      <td className="mono">{e.pointer}</td>
                      <td className="mono">{e.sha256}</td>
                      <td className="subtle">{formatTs(e.added_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
            {open ? (
              <div className="toolbar" style={{ marginTop: 10 }}>
                <label className="field">
                  <span>Pointer</span>
                  <input value={evPointer} onChange={(e) => setEvPointer(e.target.value)} aria-label="Evidence pointer" />
                </label>
                <label className="field">
                  <span>SHA-256</span>
                  <input value={evSha} onChange={(e) => setEvSha(e.target.value)} aria-label="Evidence sha256" />
                </label>
                <label className="field">
                  <span>Label</span>
                  <input value={evLabel} onChange={(e) => setEvLabel(e.target.value)} aria-label="Evidence label" />
                </label>
                <button className="btn" onClick={() => setModal("evidence")} disabled={!evPointer || !evSha || !evLabel}>
                  {t("attach_evidence")}
                </button>
              </div>
            ) : null}
          </section>

          <section className="panel">
            <h3>{t("comment")}</h3>
            {item.comments.length === 0 ? (
              <p className="subtle">No comments</p>
            ) : (
              <ul className="checklist">
                {item.comments.map((c, idx) => (
                  <li key={idx}>
                    <span>
                      <strong>{c.author_name}</strong> <span className="subtle">{formatTs(c.at)}</span>
                      <br />
                      {c.body}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </section>

          {error ? <p className="error-text">{error}</p> : null}
          <div className="actions-row">
            {open ? (
              <>
                <button className="btn" onClick={() => setModal("assign")}>
                  {t("assign")}
                </button>
                <button className="btn" onClick={() => setModal("comment")}>
                  {t("add_comment")}
                </button>
                {item.state !== "OPEN" ? (
                  <button className="btn btn-primary" onClick={() => setModal("resolve")}>
                    <DualLabel k="resolve" />
                  </button>
                ) : null}
              </>
            ) : null}
          </div>

          {modal ? (
            <TotpModal
              titleKey={modal === "assign" ? "assign" : modal === "comment" ? "add_comment" : modal === "evidence" ? "attach_evidence" : "resolve"}
              messageKey={modal === "resolve" ? "resolve_confirm" : "totp_prompt"}
              danger={modal === "resolve"}
              withReason={modal === "comment" || modal === "resolve"}
              busy={busy}
              error={error}
              onConfirm={onConfirm}
              onClose={() => {
                setModal(null);
                setError(null);
              }}
            />
          ) : null}
        </>
      ) : (
        <p className={error ? "error-text" : "subtle"}>{error ?? (loading ? t("loading") : "Case not found.")}</p>
      )}
    </Shell>
  );
}

"use client";

// Compensation item detail — interactive 6-step binding runbook checklist,
// resolution-kind picker, evidence, approval linkage (spec/15 §Comp FSM).

import React, { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { Shell } from "@/components/Shell";
import { StateBadge } from "@/components/Badges";
import { TotpModal } from "@/components/TotpModal";
import { DualLabel } from "@/lib/i18n/DualLabel";
import {
  ApiError,
  attachCompensationEvidence,
  firstTouchCompensation,
  getCompensation,
  resolveCompensation,
  setCompensationRunbookStep,
} from "@/lib/api/client";
import {
  COMPENSATION_RESOLUTION_KINDS,
  type CompensationQueueItem,
  type CompensationResolutionKind,
} from "@/lib/api/types";
import { formatBdt, formatTs } from "@/lib/format";
import { RUNBOOK_KEYS } from "@/lib/i18n/copy";
import { useLang } from "@/lib/i18n/LangContext";

type ModalKind = "first_touch" | "resolve" | "evidence" | null;

export default function CompensationDetailPage() {
  const { t } = useLang();
  const params = useParams<{ id: string }>();
  const compensationId = typeof params.id === "string" ? params.id : "";
  const [item, setItem] = useState<CompensationQueueItem | null>(null);
  const [modal, setModal] = useState<ModalKind>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [kind, setKind] = useState<CompensationResolutionKind>("RAIL_REVERSED_CONFIRMED");
  const [evPointer, setEvPointer] = useState("");
  const [evSha, setEvSha] = useState("");
  const [evLabel, setEvLabel] = useState("");
  const [stepTotp, setStepTotp] = useState("");
  const [loading, setLoading] = useState(true);

  const load = useCallback(() => {
    if (compensationId === "") {
      setLoading(false);
      setError("Compensation item not found.");
      return;
    }
    setLoading(true);
    getCompensation(compensationId)
      .then((c) => {
        setItem(c);
        setError(null);
      })
      .catch(() => {
        setItem(null);
        setError("Compensation item not found.");
      })
      .finally(() => setLoading(false));
  }, [compensationId]);

  useEffect(load, [load]);

  async function onToggleStep(step: number, done: boolean) {
    if (!item) return;
    if (stepTotp.length !== 6) {
      setError(t("totp_prompt"));
      return;
    }
    setError(null);
    try {
      const updated = await setCompensationRunbookStep(item.compensation_id, step, done, stepTotp);
      setItem(updated);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : t("error_generic"));
    }
  }

  async function onConfirm(totpCode: string, reason: string) {
    if (!item || !modal) return;
    setBusy(true);
    setError(null);
    try {
      const updated =
        modal === "first_touch"
          ? await firstTouchCompensation(item.compensation_id, totpCode)
          : modal === "evidence"
            ? await attachCompensationEvidence(item.compensation_id, evPointer, evSha, evLabel, totpCode)
            : await resolveCompensation(item.compensation_id, kind, reason, totpCode);
      setItem(updated);
      setModal(null);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : t("error_generic"));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Shell>
      <h1>{t("nav_compensation")}</h1>
      {item ? (
        <>
          <section className="panel">
            <div className="panel-head">
              <h2 className="mono">{item.compensation_id}</h2>
              <StateBadge value={item.state} />
            </div>
            <dl className="kv">
              <dt>Amount</dt>
              <dd>{formatBdt(item.amount_minor)}</dd>
              <dt>Connector</dt>
              <dd>
                {item.connector_id} <span className="mono subtle">{item.connector_ref}</span>
              </dd>
              <dt>Payment intent / attempt</dt>
              <dd className="mono">
                {item.payment_intent_id} / {item.payment_attempt_id}
              </dd>
              <dt>Resolution kind</dt>
              <dd>{item.resolution_kind ?? "—"}</dd>
              <dt>Approval linkage</dt>
              <dd>
                {item.approval_request_id ? (
                  <Link href={`/approvals/${item.approval_request_id}`} className="mono">
                    {item.approval_request_id}
                  </Link>
                ) : (
                  "—"
                )}
              </dd>
              <dt>Queued</dt>
              <dd>{formatTs(item.queued_at)}</dd>
              <dt>Resolved</dt>
              <dd>{formatTs(item.resolved_at)}</dd>
            </dl>
          </section>

          <section className="panel">
            <h3>
              <DualLabel k="runbook_title" />
            </h3>
            <ul className="checklist">
              {item.runbook_checklist.map((s) => {
                const labelKey = RUNBOOK_KEYS[s.step - 1];
                return (
                  <li key={s.step}>
                    <input
                      type="checkbox"
                      checked={s.done}
                      disabled={item.state !== "INVESTIGATING"}
                      onChange={(e) => onToggleStep(s.step, e.target.checked)}
                      aria-label={`Runbook step ${s.step}`}
                    />
                    <span>
                      {labelKey ? <DualLabel k={labelKey} /> : null}
                      {s.done && s.done_at ? <span className="subtle"> — {formatTs(s.done_at)}</span> : null}
                    </span>
                  </li>
                );
              })}
            </ul>
            {item.state === "INVESTIGATING" ? (
              <label className="field" style={{ marginTop: 8, maxWidth: 220 }}>
                <span>{t("login_totp")} (step-up for checklist updates)</span>
                <input
                  value={stepTotp}
                  onChange={(e) => setStepTotp(e.target.value.replace(/[^0-9]/g, "").slice(0, 6))}
                  inputMode="numeric"
                  aria-label="TOTP for runbook updates"
                  className="totp-input"
                />
              </label>
            ) : null}
          </section>

          <section className="panel">
            <h3>Evidence (connector query results, bank statement line, recon record)</h3>
            {item.evidence.length === 0 ? (
              <p className="subtle">No evidence attached — required before proposing a resolution</p>
            ) : (
              <table className="data-table">
                <thead>
                  <tr>
                    <th>Label</th>
                    <th>Pointer</th>
                    <th>SHA-256</th>
                  </tr>
                </thead>
                <tbody>
                  {item.evidence.map((e) => (
                    <tr key={e.sha256}>
                      <td>{e.label}</td>
                      <td className="mono">{e.pointer}</td>
                      <td className="mono">{e.sha256}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
            {item.state === "INVESTIGATING" ? (
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

          {error ? <p className="error-text">{error}</p> : null}
          <div className="actions-row">
            {item.state === "QUEUED" ? (
              <button className="btn btn-primary" onClick={() => setModal("first_touch")}>
                <DualLabel k="first_touch" />
              </button>
            ) : null}
            {item.state === "INVESTIGATING" ? (
              <>
                <label className="field">
                  <span>{t("comp_resolution")}</span>
                  <select value={kind} onChange={(e) => setKind(e.target.value as CompensationResolutionKind)}>
                    {COMPENSATION_RESOLUTION_KINDS.map((k) => (
                      <option key={k} value={k}>
                        {k}
                      </option>
                    ))}
                  </select>
                </label>
                <button className="btn btn-danger" onClick={() => setModal("resolve")}>
                  <DualLabel k="comp_resolution" />
                </button>
              </>
            ) : null}
          </div>

          {modal ? (
            <TotpModal
              titleKey={modal === "first_touch" ? "first_touch" : modal === "evidence" ? "attach_evidence" : "comp_resolution"}
              messageKey={modal === "resolve" ? "comp_resolve_confirm" : "totp_prompt"}
              danger={modal === "resolve"}
              withReason={modal === "resolve"}
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
        <p className={error ? "error-text" : "subtle"}>{error ?? (loading ? t("loading") : "Compensation item not found.")}</p>
      )}
    </Shell>
  );
}

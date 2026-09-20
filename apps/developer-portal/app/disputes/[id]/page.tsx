"use client";

// Dispute detail — evidence upload (pointer + sha256), deadline countdown,
// state history.

import React, { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { ApiError, getDispute, submitDisputeEvidence } from "@/lib/api/client";
import type { Dispute } from "@/lib/api/types";
import { StateBadge } from "@/components/Badges";
import { Shell } from "@/components/Shell";
import { TotpModal } from "@/components/TotpModal";
import { deadlineCountdown, formatBdt, formatTs, shortId } from "@/lib/format";
import { evidencePointer, sha256OfFile } from "@/lib/hash";
import { useLang } from "@/lib/i18n/LangContext";

export default function DisputeDetailPage() {
  const { t } = useLang();
  const params = useParams<{ id: string }>();
  const disputeId = typeof params.id === "string" ? params.id : "";
  const [dispute, setDispute] = useState<Dispute | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pendingEvidence, setPendingEvidence] = useState<{ pointer: string; sha256: string; label: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [modalError, setModalError] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  const load = useCallback(() => {
    if (disputeId === "") {
      setError(t("error_generic"));
      return;
    }
    getDispute(disputeId)
      .then((d) => {
        setDispute(d);
        setError(null);
      })
      .catch((err) => setError(err instanceof ApiError ? err.message : t("error_generic")));
  }, [disputeId, t]);

  useEffect(() => {
    load();
  }, [load]);

  async function onFileChosen(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!file) return;
    const digest = await sha256OfFile(file);
    setPendingEvidence({ pointer: evidencePointer("bdpay-evidence", file.name, digest), sha256: digest, label: file.name });
    setModalError(null);
  }

  async function confirm(totpCode: string) {
    if (!pendingEvidence || !dispute) return;
    setBusy(true);
    setModalError(null);
    try {
      const updated = await submitDisputeEvidence(
        dispute.dispute_id,
        pendingEvidence.pointer,
        pendingEvidence.sha256,
        pendingEvidence.label,
        totpCode,
      );
      setDispute(updated);
      setPendingEvidence(null);
    } catch (err) {
      setModalError(err instanceof ApiError ? err.message : t("error_generic"));
    } finally {
      setBusy(false);
    }
  }

  const cd = dispute ? deadlineCountdown(dispute.evidence_deadline_at) : null;
  const canSubmit = dispute !== null && (dispute.state === "EVIDENCE_PENDING" || dispute.state === "OPEN");

  return (
    <Shell>
      <p>
        <Link href="/disputes">← {t("nav_disputes")}</Link>
      </p>
      {error ? <p className="error-text">{error}</p> : null}
      {!dispute && !error ? (
        <p>{t("loading")}</p>
      ) : !dispute ? null : (
        <>
          <div className="panel-head">
            <h1 className="mono">{dispute.dispute_id}</h1>
            <StateBadge value={dispute.state} />
          </div>

          <section className="panel">
            <dl className="kv">
              <dt>Payment intent</dt>
              <dd className="mono">{dispute.payment_intent_id}</dd>
              <dt>Amount</dt>
              <dd>
                {formatBdt(dispute.amount_minor)} · {dispute.method}
              </dd>
              <dt>Reason</dt>
              <dd>{dispute.reason_code}</dd>
              <dt>{t("evidence_deadline")}</dt>
              <dd>
                {cd ? (
                  <span className={`deadline ${cd.late ? "deadline-late" : ""}`}>
                    {cd.label} <span className="subtle">({formatTs(dispute.evidence_deadline_at)})</span>
                  </span>
                ) : (
                  "—"
                )}
              </dd>
              <dt>Opened</dt>
              <dd>{formatTs(dispute.opened_at)}</dd>
              <dt>Resolved</dt>
              <dd>{formatTs(dispute.resolved_at)}</dd>
            </dl>
          </section>

          <section className="panel">
            <div className="panel-head">
              <h2>Evidence</h2>
              {canSubmit ? (
                <button className="btn btn-primary" onClick={() => fileInputRef.current?.click()}>
                  {t("submit_evidence")}
                </button>
              ) : null}
            </div>
            {canSubmit ? <p className="notice">{t("submit_evidence_confirm")}</p> : null}
            <input
              type="file"
              ref={fileInputRef}
              onChange={(e) => void onFileChosen(e)}
              style={{ display: "none" }}
              aria-label={t("submit_evidence")}
            />
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
                {dispute.evidence.length === 0 ? (
                  <tr>
                    <td colSpan={4} className="empty-row">
                      No evidence submitted yet
                    </td>
                  </tr>
                ) : (
                  dispute.evidence.map((ev, i) => (
                    <tr key={`${ev.sha256}-${i}`}>
                      <td>{ev.label}</td>
                      <td className="mono">{shortId(ev.pointer, 36)}</td>
                      <td className="mono">{shortId(ev.sha256, 20)}</td>
                      <td>{formatTs(ev.added_at)}</td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </section>

          <section className="panel">
            <h2>State history</h2>
            <ul className="checklist">
              {dispute.state_history.map((h, i) => (
                <li key={`${h.state}-${i}`}>
                  <StateBadge value={h.state} /> <span className="subtle">{formatTs(h.at)}</span>
                </li>
              ))}
            </ul>
          </section>

          {pendingEvidence ? (
            <TotpModal
              titleKey="submit_evidence"
              messageKey="submit_evidence_confirm"
              busy={busy}
              error={modalError}
              onConfirm={(code) => void confirm(code)}
              onClose={() => setPendingEvidence(null)}
            />
          ) : null}
        </>
      )}
    </Shell>
  );
}

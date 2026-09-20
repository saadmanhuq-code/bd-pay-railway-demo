"use client";

// Dispute detail — FSM-aware action buttons per state (spec/15 §Dispute FSM):
// OPEN → request evidence; EVIDENCE_PENDING → mark under review;
// UNDER_REVIEW → resolve merchant/customer (two-eyes notice) or file
// chargeback (method=CARD only).

import React, { useCallback, useEffect, useState } from "react";
import { useParams } from "next/navigation";
import { Shell } from "@/components/Shell";
import { StateBadge } from "@/components/Badges";
import { TotpModal } from "@/components/TotpModal";
import { DualLabel } from "@/lib/i18n/DualLabel";
import {
  ApiError,
  fileChargeback,
  getDispute,
  markDisputeUnderReview,
  requestDisputeEvidence,
  resolveDispute,
} from "@/lib/api/client";
import type { Dispute } from "@/lib/api/types";
import { formatBdt, formatTs } from "@/lib/format";
import { useLang } from "@/lib/i18n/LangContext";

type ModalKind = "request_evidence" | "under_review" | "resolve_merchant" | "resolve_customer" | "chargeback" | null;

export default function DisputeDetailPage() {
  const { t } = useLang();
  const params = useParams<{ id: string }>();
  const disputeId = typeof params.id === "string" ? params.id : "";
  const [item, setItem] = useState<Dispute | null>(null);
  const [modal, setModal] = useState<ModalKind>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(() => {
    if (disputeId === "") {
      setLoading(false);
      setError("Dispute not found.");
      return;
    }
    setLoading(true);
    getDispute(disputeId)
      .then((d) => {
        setItem(d);
        setError(null);
      })
      .catch(() => {
        setItem(null);
        setError("Dispute not found.");
      })
      .finally(() => setLoading(false));
  }, [disputeId]);

  useEffect(load, [load]);

  async function onConfirm(totpCode: string, reason: string) {
    if (!item || !modal) return;
    setBusy(true);
    setError(null);
    try {
      const updated =
        modal === "request_evidence"
          ? await requestDisputeEvidence(item.dispute_id, totpCode)
          : modal === "under_review"
            ? await markDisputeUnderReview(item.dispute_id, totpCode)
            : modal === "chargeback"
              ? await fileChargeback(item.dispute_id, totpCode)
              : await resolveDispute(item.dispute_id, modal === "resolve_merchant" ? "RESOLVED_MERCHANT" : "RESOLVED_CUSTOMER", reason, totpCode);
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
      <h1>{t("nav_disputes")}</h1>
      {item ? (
        <>
          <section className="panel">
            <div className="panel-head">
              <h2 className="mono">{item.dispute_id}</h2>
              <StateBadge value={item.state} />
            </div>
            <dl className="kv">
              <dt>Merchant</dt>
              <dd>
                {item.merchant_name} <span className="mono subtle">{item.merchant_id}</span>
              </dd>
              <dt>Payment intent</dt>
              <dd className="mono">{item.payment_intent_id}</dd>
              <dt>Amount</dt>
              <dd>{formatBdt(item.amount_minor)}</dd>
              <dt>Method</dt>
              <dd>{item.method}</dd>
              <dt>Reason code</dt>
              <dd>{item.reason_code}</dd>
              <dt>Opened by</dt>
              <dd className="mono">{item.opened_by}</dd>
              <dt>Evidence deadline</dt>
              <dd>{formatTs(item.evidence_deadline_at)}</dd>
              <dt>No merchant evidence flag</dt>
              <dd>{item.no_merchant_evidence ? "true" : "false"}</dd>
              <dt>Refund</dt>
              <dd className="mono">{item.refund_id ?? "—"}</dd>
              <dt>Scheme case ref</dt>
              <dd className="mono">{item.scheme_case_ref ?? "—"}</dd>
              <dt>Opened</dt>
              <dd>{formatTs(item.opened_at)}</dd>
              <dt>Resolved</dt>
              <dd>{formatTs(item.resolved_at)}</dd>
            </dl>
          </section>

          <section className="panel">
            <h3>Evidence</h3>
            {item.evidence.length === 0 ? (
              <p className="subtle">No evidence submitted</p>
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
          </section>

          {item.state === "UNDER_REVIEW" ? (
            <p className="notice">
              <DualLabel k="two_eyes_notice" />
            </p>
          ) : null}
          {error ? <p className="error-text">{error}</p> : null}

          <div className="actions-row">
            {item.state === "OPEN" ? (
              <button className="btn btn-primary" onClick={() => setModal("request_evidence")}>
                <DualLabel k="request_evidence" />
              </button>
            ) : null}
            {item.state === "EVIDENCE_PENDING" ? (
              <button className="btn btn-primary" onClick={() => setModal("under_review")}>
                <DualLabel k="mark_under_review" />
              </button>
            ) : null}
            {item.state === "UNDER_REVIEW" ? (
              <>
                <button className="btn btn-primary" onClick={() => setModal("resolve_merchant")}>
                  <DualLabel k="resolve_merchant" />
                </button>
                <button className="btn btn-primary" onClick={() => setModal("resolve_customer")}>
                  <DualLabel k="resolve_customer" />
                </button>
                {item.method === "CARD" ? (
                  <button className="btn btn-danger" onClick={() => setModal("chargeback")}>
                    <DualLabel k="file_chargeback" />
                  </button>
                ) : null}
              </>
            ) : null}
          </div>

          {modal ? (
            <TotpModal
              titleKey={
                modal === "request_evidence"
                  ? "request_evidence"
                  : modal === "under_review"
                    ? "mark_under_review"
                    : modal === "resolve_merchant"
                      ? "resolve_merchant"
                      : modal === "resolve_customer"
                        ? "resolve_customer"
                        : "file_chargeback"
              }
              messageKey={modal === "resolve_merchant" || modal === "resolve_customer" ? "dispute_resolve_confirm" : "totp_prompt"}
              danger={modal === "chargeback" || modal === "resolve_customer"}
              withReason={modal === "resolve_merchant" || modal === "resolve_customer"}
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
        <p className={error ? "error-text" : "subtle"}>{error ?? (loading ? t("loading") : "Dispute not found.")}</p>
      )}
    </Shell>
  );
}

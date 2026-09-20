"use client";

import React, { useCallback, useEffect, useState } from "react";
import { useParams } from "next/navigation";
import { Shell } from "@/components/Shell";
import { StateBadge } from "@/components/Badges";
import { TotpModal } from "@/components/TotpModal";
import { DualLabel } from "@/lib/i18n/DualLabel";
import { ApiError, approveApproval, getApproval, rejectApproval, withdrawApproval } from "@/lib/api/client";
import type { ApprovalRequest } from "@/lib/api/types";
import { formatBdt, formatTs } from "@/lib/format";
import { useLang } from "@/lib/i18n/LangContext";
import { useSession } from "@/lib/useSession";

type ModalKind = "approve" | "reject" | "withdraw" | null;

export default function ApprovalDetailPage() {
  const { t } = useLang();
  const { session } = useSession();
  const params = useParams<{ id: string }>();
  const approvalId = typeof params.id === "string" ? params.id : "";
  const [appr, setAppr] = useState<ApprovalRequest | null>(null);
  const [modal, setModal] = useState<ModalKind>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [pageError, setPageError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(() => {
    if (approvalId === "") {
      setLoading(false);
      setPageError("Approval request not found.");
      return;
    }
    setLoading(true);
    getApproval(approvalId)
      .then((a) => {
        setAppr(a);
        setPageError(null);
      })
      .catch(() => {
        setAppr(null);
        setPageError("Approval request not found.");
      })
      .finally(() => setLoading(false));
  }, [approvalId]);

  useEffect(load, [load]);

  async function onConfirm(totpCode: string, reason: string) {
    if (!appr || !modal) return;
    setBusy(true);
    setError(null);
    try {
      const updated =
        modal === "approve"
          ? await approveApproval(appr.approval_request_id, reason, totpCode)
          : modal === "reject"
            ? await rejectApproval(appr.approval_request_id, reason, totpCode)
            : await withdrawApproval(appr.approval_request_id, totpCode);
      setAppr(updated);
      setModal(null);
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        setError(t("conflict_409"));
      } else if (err instanceof ApiError) {
        setError(err.message);
      } else {
        setError(t("error_generic"));
      }
    } finally {
      setBusy(false);
    }
  }

  const selfInitiated = appr !== null && session !== null && appr.initiator_id === session.operator.operator_id;
  const pending = appr?.state === "PENDING_SECOND_APPROVER";
  const retryable = appr?.state === "APPROVED_EXECUTION_FAILED";

  return (
    <Shell>
      <h1>{t("nav_approvals")}</h1>
      {appr ? (
        <>
          <section className="panel">
            <div className="panel-head">
              <h2 className="mono">{appr.approval_request_id}</h2>
              <StateBadge value={appr.state} />
            </div>
            <dl className="kv">
              <dt>Action type</dt>
              <dd>{appr.action_type}</dd>
              <dt>Subject</dt>
              <dd>
                {appr.subject_type} <span className="mono">{appr.subject_id}</span>
              </dd>
              <dt>Reason</dt>
              <dd>{appr.reason}</dd>
              <dt>Initiator</dt>
              <dd>
                {appr.initiator_name} <span className="mono subtle">{appr.initiator_id}</span>
              </dd>
              <dt>Approver</dt>
              <dd>{appr.approver_name ?? "—"}</dd>
              <dt>Decision reason</dt>
              <dd>{appr.decision_reason ?? "—"}</dd>
              <dt>Threshold</dt>
              <dd>{appr.threshold_minor !== null ? formatBdt(appr.threshold_minor) : "—"}</dd>
              <dt>Created</dt>
              <dd>{formatTs(appr.created_at)}</dd>
              <dt>Expires (24h TTL, fail-closed)</dt>
              <dd>{formatTs(appr.expires_at)}</dd>
              <dt>Decided</dt>
              <dd>{formatTs(appr.decided_at)}</dd>
              <dt>Executed</dt>
              <dd>{formatTs(appr.executed_at)}</dd>
            </dl>
          </section>

          <section className="panel">
            <h3>
              <DualLabel k="payload_hash" />
            </h3>
            <p className="mono">{appr.payload_hash}</p>
            <h3>Payload</h3>
            <pre className="mono">{JSON.stringify(appr.payload, null, 2)}</pre>
          </section>

          {selfInitiated && pending ? (
            <p className="notice">
              <DualLabel k="self_initiated_notice" />
            </p>
          ) : null}
          {error ? <p className="error-text">{error}</p> : null}

          <div className="actions-row">
            {pending && !selfInitiated ? (
              <>
                <button className="btn btn-primary" onClick={() => setModal("approve")}>
                  <DualLabel k="approve" />
                </button>
                <button className="btn btn-danger" onClick={() => setModal("reject")}>
                  <DualLabel k="reject" />
                </button>
              </>
            ) : null}
            {retryable && !selfInitiated ? (
              <button className="btn btn-primary" onClick={() => setModal("approve")}>
                Retry callback execution
              </button>
            ) : null}
            {pending && selfInitiated ? (
              <button className="btn btn-danger" onClick={() => setModal("withdraw")}>
                <DualLabel k="withdraw" />
              </button>
            ) : null}
          </div>

          {modal ? (
            <TotpModal
              titleKey={modal === "approve" ? "approve" : modal === "reject" ? "reject" : "withdraw"}
              messageKey={modal === "approve" ? "approve_confirm" : modal === "reject" ? "reject_confirm" : "withdraw_confirm"}
              danger={modal !== "approve"}
              withReason={modal !== "withdraw"}
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
        <p className={pageError ? "error-text" : "subtle"}>{pageError ?? (loading ? t("loading") : "Approval request not found.")}</p>
      )}
    </Shell>
  );
}

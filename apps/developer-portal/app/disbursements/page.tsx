"use client";

// Bulk disbursement maker-checker: CSV upload with per-row validation
// (row-level error codes, Bengali numeral normalization), maker submits a
// batch, checker approval queue (checker ≠ maker enforced by the server and
// reflected in the UI), release status, finance co-sign notice > BDT 10 lakh.

import React, { useCallback, useEffect, useRef, useState } from "react";
import {
  ApiError,
  approveDisbursementBatch,
  createDisbursementBatch,
  listDisbursementBatches,
  rejectDisbursementBatch,
} from "@/lib/api/client";
import type { DisbursementBatch } from "@/lib/api/types";
import { EnvBadge, StateBadge } from "@/components/Badges";
import { Shell } from "@/components/Shell";
import { TotpModal } from "@/components/TotpModal";
import { CSV_HEADER, parseDisbursementCsv, type CsvParseResult } from "@/lib/csv";
import { formatBdt, formatTs, shortId } from "@/lib/format";
import { DualLabel } from "@/lib/i18n/DualLabel";
import { useLang } from "@/lib/i18n/LangContext";
import { useMode } from "@/lib/mode";
import { useSession } from "@/lib/useSession";

const COSIGN_THRESHOLD = 100000000n; // BDT 10 lakh in paisa

type PendingAction =
  | { kind: "submit" }
  | { kind: "approve"; batch: DisbursementBatch }
  | { kind: "reject"; batch: DisbursementBatch };

export default function DisbursementsPage() {
  const { t } = useLang();
  const { env } = useMode();
  const { session } = useSession();
  const [batches, setBatches] = useState<DisbursementBatch[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [csv, setCsv] = useState<CsvParseResult | null>(null);
  const [csvName, setCsvName] = useState("");
  const [pending, setPending] = useState<PendingAction | null>(null);
  const [busy, setBusy] = useState(false);
  const [modalError, setModalError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  const load = useCallback(() => {
    listDisbursementBatches(env)
      .then((page) => {
        setBatches(page.data);
        setError(null);
      })
      .catch((err) => setError(err instanceof ApiError ? err.message : t("error_generic")));
  }, [env, t]);

  useEffect(() => {
    load();
  }, [load]);

  async function onFileChosen(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!file) return;
    const text = await file.text();
    setCsvName(file.name);
    setCsv(parseDisbursementCsv(text));
  }

  async function confirm(totpCode: string, reason: string) {
    if (!pending) return;
    setBusy(true);
    setModalError(null);
    try {
      if (pending.kind === "submit") {
        if (!csv) return;
        await createDisbursementBatch(env, csv.validRows, totpCode);
        setCsv(null);
        setCsvName("");
      } else if (pending.kind === "approve") {
        await approveDisbursementBatch(pending.batch.batch_id, totpCode);
      } else {
        await rejectDisbursementBatch(pending.batch.batch_id, reason, totpCode);
      }
      setPending(null);
      load();
    } catch (err) {
      setModalError(err instanceof ApiError ? err.message : t("error_generic"));
    } finally {
      setBusy(false);
    }
  }

  const persona = session?.member.persona ?? "owner";
  const isChecker = persona === "finance_checker" || persona === "owner";
  const isMaker = persona === "finance_maker" || persona === "owner";
  const errorRowCount = csv ? csv.reports.filter((r) => r.errors.length > 0).length : 0;
  const csvOverThreshold = csv !== null && BigInt(csv.totalMinor) > COSIGN_THRESHOLD;

  return (
    <Shell>
      <h1>{t("nav_disbursements")}</h1>
      {error ? <p className="error-text">{error}</p> : null}
      <p className="notice">
        <DualLabel k="checker_neq_maker" />
      </p>

      {isMaker ? (
        <section className="panel">
          <div className="panel-head">
            <h2>
              {t("upload_csv")} <EnvBadge env={env} />
            </h2>
            <button className="btn" onClick={() => fileInputRef.current?.click()}>
              {t("upload_csv")}
            </button>
          </div>
          <p className="subtle">
            Header: <code className="mono">{CSV_HEADER}</code> — amounts in BDT (e.g. 1250.50; Bengali numerals accepted).
          </p>
          <p className="subtle">{t("bn_numeral_note")}</p>
          <input
            type="file"
            accept=".csv,text/csv"
            ref={fileInputRef}
            onChange={(e) => void onFileChosen(e)}
            style={{ display: "none" }}
            aria-label={t("upload_csv")}
          />
          {csv ? (
            <>
              <h3>
                {csvName} — {csv.validRows.length} valid row(s), {errorRowCount} error row(s), total {formatBdt(csv.totalMinor)}
              </h3>
              {!csv.headerOk ? <p className="notice notice-warn">Header row missing or malformed — rows parsed positionally.</p> : null}
              {csvOverThreshold ? (
                <p className="notice notice-warn">
                  <DualLabel k="finance_cosign_notice" />
                </p>
              ) : null}
              <table className="data-table">
                <thead>
                  <tr>
                    <th>#</th>
                    <th>Beneficiary</th>
                    <th>Account</th>
                    <th>Routing</th>
                    <th>Amount</th>
                    <th>Reference</th>
                    <th>Errors</th>
                  </tr>
                </thead>
                <tbody>
                  {csv.reports.map((rep) => (
                    <tr key={rep.row_no} className={rep.errors.length > 0 ? "row-error" : ""}>
                      <td>{rep.row_no}</td>
                      <td>{rep.row?.beneficiary_name ?? "—"}</td>
                      <td className="mono">{rep.row?.account_number ?? "—"}</td>
                      <td className="mono">{rep.row?.routing_number ?? "—"}</td>
                      <td>{rep.row ? formatBdt(rep.row.amount_minor) : "—"}</td>
                      <td className="mono">{rep.row?.reference ?? "—"}</td>
                      <td className="error-text">{rep.errors.join(", ")}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <div className="actions-row">
                <button
                  className="btn btn-primary"
                  disabled={csv.validRows.length === 0 || errorRowCount > 0}
                  onClick={() => {
                    setModalError(null);
                    setPending({ kind: "submit" });
                  }}
                >
                  <DualLabel k="submit_batch" />
                </button>
                <button
                  className="btn"
                  onClick={() => {
                    setCsv(null);
                    setCsvName("");
                  }}
                >
                  {t("cancel")}
                </button>
              </div>
              {errorRowCount > 0 ? <p className="subtle">Fix all error rows in the file and re-upload before submitting.</p> : null}
            </>
          ) : null}
        </section>
      ) : null}

      <section className="panel">
        <h2>Batches</h2>
        <table className="data-table">
          <thead>
            <tr>
              <th>Batch</th>
              <th>State</th>
              <th>Rows</th>
              <th>Total</th>
              <th>Maker</th>
              <th>Checker</th>
              <th>Created</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {batches.length === 0 ? (
              <tr>
                <td colSpan={8} className="empty-row">
                  No batches in this environment
                </td>
              </tr>
            ) : (
              batches.map((b) => {
                const mine = session?.member.member_id === b.maker_member_id;
                return (
                  <React.Fragment key={b.batch_id}>
                    <tr className="clickable" onClick={() => setExpanded(expanded === b.batch_id ? null : b.batch_id)}>
                      <td className="mono">{shortId(b.batch_id, 18)}</td>
                      <td>
                        <StateBadge value={b.state} />
                        {b.finance_cosign_required ? <div className="subtle">finance co-sign required</div> : null}
                      </td>
                      <td>{b.row_count}</td>
                      <td>{formatBdt(b.total_minor)}</td>
                      <td>{b.maker_name}</td>
                      <td>{b.checker_name ?? "—"}</td>
                      <td>{formatTs(b.created_at)}</td>
                      <td>
                        {b.state === "PENDING_CHECKER" && isChecker && !mine ? (
                          <span className="actions-row">
                            <button
                              className="btn btn-small btn-primary"
                              onClick={(e) => {
                                e.stopPropagation();
                                setModalError(null);
                                setPending({ kind: "approve", batch: b });
                              }}
                            >
                              <DualLabel k="approve_batch" />
                            </button>
                            <button
                              className="btn btn-small btn-danger"
                              onClick={(e) => {
                                e.stopPropagation();
                                setModalError(null);
                                setPending({ kind: "reject", batch: b });
                              }}
                            >
                              {t("reject_batch")}
                            </button>
                          </span>
                        ) : b.state === "PENDING_CHECKER" && mine ? (
                          <span className="subtle">{t("checker_neq_maker")}</span>
                        ) : null}
                      </td>
                    </tr>
                    {expanded === b.batch_id ? (
                      <tr>
                        <td colSpan={8}>
                          {b.reject_reason ? <p className="error-text">Rejected: {b.reject_reason}</p> : null}
                          {b.approval_request_id ? (
                            <p className="subtle">
                              Finance co-sign approval request: <span className="mono">{b.approval_request_id}</span>
                            </p>
                          ) : null}
                          <table className="data-table">
                            <thead>
                              <tr>
                                <th>#</th>
                                <th>Beneficiary</th>
                                <th>Account</th>
                                <th>Routing</th>
                                <th>Amount</th>
                                <th>Reference</th>
                              </tr>
                            </thead>
                            <tbody>
                              {b.rows.map((r) => (
                                <tr key={r.row_no}>
                                  <td>{r.row_no}</td>
                                  <td>{r.beneficiary_name}</td>
                                  <td className="mono">{r.account_number}</td>
                                  <td className="mono">{r.routing_number}</td>
                                  <td>{formatBdt(r.amount_minor)}</td>
                                  <td className="mono">{r.reference}</td>
                                </tr>
                              ))}
                            </tbody>
                          </table>
                        </td>
                      </tr>
                    ) : null}
                  </React.Fragment>
                );
              })
            )}
          </tbody>
        </table>
        <p className="subtle">{t("finance_cosign_notice")}</p>
      </section>

      {pending ? (
        <TotpModal
          titleKey={pending.kind === "submit" ? "submit_batch" : pending.kind === "approve" ? "approve_batch" : "reject_batch"}
          messageKey={
            pending.kind === "submit" ? "submit_batch_confirm" : pending.kind === "approve" ? "approve_batch_confirm" : "reject_batch_confirm"
          }
          danger={pending.kind !== "submit"}
          withReason={pending.kind === "reject"}
          busy={busy}
          error={modalError}
          onConfirm={(code, reason) => void confirm(code, reason)}
          onClose={() => setPending(null)}
        />
      ) : null}
    </Shell>
  );
}

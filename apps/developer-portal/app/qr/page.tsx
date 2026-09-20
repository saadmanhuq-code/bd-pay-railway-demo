"use client";

// Bangla QR management (spec/13) against the REAL engine routes:
//   POST/GET /v1/merchants/{merchant_id}/qr-codes  — issue static / list
//   GET      /v1/qr-codes/{merchant_qr_id}         — fetch one
//   POST     /v1/qr-codes/{merchant_qr_id}/suspend | reactivate | revoke
// The EMVCo TLV payload is ENGINE-issued and returned once at issuance;
// print-ready assets come from the render endpoints (PNG/SVG/kit PDF).

import React, { useCallback, useEffect, useState } from "react";
import {
  ApiError,
  issueStaticQr,
  listMerchantQrs,
  qrAssetUrl,
  reactivateQr,
  revokeQr,
  suspendQr,
} from "@/lib/api/client";
import type { MerchantQr, StaticQrIssued } from "@/lib/api/types";
import { StateBadge } from "@/components/Badges";
import { Shell } from "@/components/Shell";
import { TotpModal } from "@/components/TotpModal";
import { formatBdt, formatTs } from "@/lib/format";
import { useLang } from "@/lib/i18n/LangContext";
import type { CopyKey } from "@/lib/i18n/copy";
import { useSession } from "@/lib/useSession";

type LifecycleKind = "suspend" | "reactivate" | "revoke";

type PendingAction = { kind: "issue" } | { kind: LifecycleKind; qr: MerchantQr };

function downloadPayload(issued: StaticQrIssued, label: string) {
  const blob = new Blob([issued.payload + "\n"], { type: "text/plain" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `bangla-qr-${label.replace(/[^a-zA-Z0-9-]/g, "_")}.txt`;
  a.click();
  URL.revokeObjectURL(url);
}

export default function QrPage() {
  const { t } = useLang();
  const { session } = useSession();
  const merchantId = session?.merchant.merchant_id ?? null;

  const [qrs, setQrs] = useState<MerchantQr[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [label, setLabel] = useState("");
  const [storeId, setStoreId] = useState("");
  const [terminalId, setTerminalId] = useState("");
  const [pending, setPending] = useState<PendingAction | null>(null);
  const [busy, setBusy] = useState(false);
  const [modalError, setModalError] = useState<string | null>(null);
  const [issued, setIssued] = useState<{ result: StaticQrIssued; label: string } | null>(null);
  const [selected, setSelected] = useState<MerchantQr | null>(null);

  const load = useCallback(() => {
    if (!merchantId) return;
    listMerchantQrs(merchantId)
      .then((res) => {
        setQrs(res.data);
        setError(null);
        setSelected((cur) => (cur ? res.data.find((q) => q.merchant_qr_id === cur.merchant_qr_id) ?? null : cur));
      })
      .catch((err) => setError(err instanceof ApiError ? err.message : t("error_generic")));
  }, [merchantId, t]);

  useEffect(() => {
    load();
  }, [load]);

  async function confirm(totpCode: string, reason: string) {
    if (!pending || !merchantId) return;
    setBusy(true);
    setModalError(null);
    try {
      if (pending.kind === "issue") {
        const result = await issueStaticQr(
          merchantId,
          label,
          storeId.trim() === "" ? null : storeId.trim(),
          terminalId.trim() === "" ? null : terminalId.trim(),
          totpCode,
        );
        setIssued({ result, label });
        setLabel("");
        setStoreId("");
        setTerminalId("");
      } else {
        const qrId = pending.qr.merchant_qr_id;
        const trimmedReason = reason.trim() === "" ? null : reason.trim();
        if (pending.kind === "suspend") {
          await suspendQr(qrId, trimmedReason, totpCode);
        } else if (pending.kind === "reactivate") {
          await reactivateQr(qrId, totpCode);
        } else {
          await revokeQr(qrId, trimmedReason, totpCode);
          if (issued?.result.merchant_qr_id === qrId) setIssued(null);
        }
      }
      setPending(null);
      load();
    } catch (err) {
      setModalError(err instanceof ApiError ? err.message : t("error_generic"));
    } finally {
      setBusy(false);
    }
  }

  const modalKeys: Record<PendingAction["kind"], { title: CopyKey; message: CopyKey }> = {
    issue: { title: "register_qr", message: "qr_static_limit" },
    suspend: { title: "suspend_qr", message: "suspend_qr_confirm" },
    reactivate: { title: "reactivate_qr", message: "reactivate_qr_confirm" },
    revoke: { title: "revoke_qr", message: "revoke_qr_confirm" },
  };

  return (
    <Shell>
      <h1>{t("nav_qr")}</h1>
      {error ? <p className="error-text">{error}</p> : null}

      <section className="panel">
        <h2>{t("register_qr")}</h2>
        <div className="toolbar">
          <label className="field">
            <span>Label</span>
            <input value={label} onChange={(e) => setLabel(e.target.value)} />
          </label>
          <label className="field">
            <span>Store ID (optional)</span>
            <input value={storeId} onChange={(e) => setStoreId(e.target.value)} />
          </label>
          <label className="field">
            <span>Terminal ID (optional)</span>
            <input value={terminalId} onChange={(e) => setTerminalId(e.target.value)} />
          </label>
        </div>
        <p className="subtle">{t("qr_static_limit")}</p>
        <button
          className="btn btn-primary"
          disabled={label.trim() === "" || !merchantId}
          onClick={() => {
            setModalError(null);
            setPending({ kind: "issue" });
          }}
        >
          {t("register_qr")}
        </button>
      </section>

      {issued ? (
        <section className="panel">
          <div className="panel-head">
            <h2>EMVCo payload — {issued.label}</h2>
            <button className="btn btn-small" onClick={() => downloadPayload(issued.result, issued.label)}>
              {t("download")}
            </button>
          </div>
          <p className="subtle">{t("qr_payload_once")}</p>
          <div className="qr-payload mono">{issued.result.payload}</div>
          <dl className="kv">
            <dt>Payload hash</dt>
            <dd className="mono">{issued.result.payload_hash}</dd>
            <dt>Static per-scan limit</dt>
            <dd>{formatBdt(issued.result.static_limit_minor)}</dd>
            <dt>Print-ready assets</dt>
            <dd>
              <a href={qrAssetUrl(issued.result.render_urls.png)} target="_blank" rel="noreferrer">
                PNG
              </a>
              {" · "}
              <a href={qrAssetUrl(issued.result.render_urls.svg)} target="_blank" rel="noreferrer">
                SVG
              </a>
              {" · "}
              <a href={qrAssetUrl(issued.result.render_urls.kit_pdf)} target="_blank" rel="noreferrer">
                Kit PDF
              </a>
            </dd>
          </dl>
        </section>
      ) : null}

      <section className="panel">
        <h2>Issued QRs</h2>
        <table className="data-table">
          <thead>
            <tr>
              <th>Label</th>
              <th>Store / terminal</th>
              <th>State</th>
              <th>Updated</th>
              <th>Created</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {qrs.length === 0 ? (
              <tr>
                <td colSpan={6} className="empty-row">
                  No QR codes issued for this merchant
                </td>
              </tr>
            ) : (
              qrs.map((qr) => (
                <tr key={qr.merchant_qr_id} className="clickable" onClick={() => setSelected(qr)}>
                  <td>{qr.label}</td>
                  <td>
                    {qr.store_id ?? "—"}
                    {qr.terminal_id ? <div className="subtle">{qr.terminal_id}</div> : null}
                  </td>
                  <td>
                    <StateBadge value={qr.state} />
                    {qr.suspend_reason ? <div className="subtle">{qr.suspend_reason}</div> : null}
                  </td>
                  <td>{formatTs(qr.updated_at)}</td>
                  <td>{formatTs(qr.created_at)}</td>
                  <td>
                    {qr.state === "ACTIVE" ? (
                      <button
                        className="btn btn-small"
                        onClick={(e) => {
                          e.stopPropagation();
                          setModalError(null);
                          setPending({ kind: "suspend", qr });
                        }}
                      >
                        {t("suspend_qr")}
                      </button>
                    ) : null}
                    {qr.state === "SUSPENDED" ? (
                      <button
                        className="btn btn-small"
                        onClick={(e) => {
                          e.stopPropagation();
                          setModalError(null);
                          setPending({ kind: "reactivate", qr });
                        }}
                      >
                        {t("reactivate_qr")}
                      </button>
                    ) : null}
                    {qr.state !== "REVOKED" ? (
                      <button
                        className="btn btn-small btn-danger"
                        onClick={(e) => {
                          e.stopPropagation();
                          setModalError(null);
                          setPending({ kind: "revoke", qr });
                        }}
                      >
                        {t("revoke_qr")}
                      </button>
                    ) : null}
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </section>

      {selected ? (
        <section className="panel">
          <div className="panel-head">
            <h2>
              {selected.label} <StateBadge value={selected.state} />
            </h2>
          </div>
          {/* Engine-rendered asset (deterministic bytes; X-BDPay-Content-Sha256). */}
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img
            src={qrAssetUrl(selected.render_urls.svg)}
            alt={`Bangla QR — ${selected.label}`}
            width={240}
            height={240}
          />
          <dl className="kv">
            <dt>Merchant QR ID</dt>
            <dd className="mono">{selected.merchant_qr_id}</dd>
            <dt>Store / terminal</dt>
            <dd>
              {selected.store_id ?? "—"} / {selected.terminal_id ?? "—"}
            </dd>
            {selected.suspend_reason ? (
              <>
                <dt>Suspend reason</dt>
                <dd>{selected.suspend_reason}</dd>
              </>
            ) : null}
            <dt>Print-ready assets</dt>
            <dd>
              <a href={qrAssetUrl(selected.render_urls.png)} target="_blank" rel="noreferrer">
                PNG
              </a>
              {" · "}
              <a href={qrAssetUrl(selected.render_urls.svg)} target="_blank" rel="noreferrer">
                SVG
              </a>
              {" · "}
              <a href={qrAssetUrl(selected.render_urls.kit_pdf)} target="_blank" rel="noreferrer">
                Kit PDF
              </a>
            </dd>
          </dl>
        </section>
      ) : null}

      {pending ? (
        <TotpModal
          titleKey={modalKeys[pending.kind].title}
          messageKey={modalKeys[pending.kind].message}
          danger={pending.kind === "revoke"}
          withReason={pending.kind === "suspend" || pending.kind === "revoke"}
          busy={busy}
          error={modalError}
          onConfirm={(code, reason) => void confirm(code, reason)}
          onClose={() => setPending(null)}
        />
      ) : null}
    </Shell>
  );
}

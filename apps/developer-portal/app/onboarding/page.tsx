"use client";

// Onboarding status tracker + KYB document checklist upload UI (spec/08).
// Reachable two ways: pre-auth with ?application_id=… (fresh signup), or
// authenticated (the merchant's current application).

import React, { Suspense, useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { ApiError, getMyOnboardingApplication, getOnboardingApplication, uploadKybDocument } from "@/lib/api/client";
import type { KybDocument, OnboardingApplication } from "@/lib/api/types";
import { KYB_TRACKER_STATES } from "@/lib/api/types";
import { StateBadge } from "@/components/Badges";
import { TotpModal } from "@/components/TotpModal";
import { formatTs, shortId } from "@/lib/format";
import { evidencePointer, sha256OfFile } from "@/lib/hash";
import { useLang } from "@/lib/i18n/LangContext";

function Tracker({ app }: { app: OnboardingApplication }) {
  const currentIdx = KYB_TRACKER_STATES.indexOf(app.kyb_status);
  return (
    <div className="tracker">
      {KYB_TRACKER_STATES.map((s, i) => {
        const cls = app.kyb_status === "REJECTED" ? "" : i < currentIdx ? "done" : i === currentIdx ? "current" : "";
        return (
          <React.Fragment key={s}>
            {i > 0 ? <span className="tracker-sep">→</span> : null}
            <span className={`tracker-step ${cls}`}>{s}</span>
          </React.Fragment>
        );
      })}
      {app.kyb_status === "REJECTED" ? <StateBadge value="REJECTED" /> : null}
    </div>
  );
}

function OnboardingInner() {
  const { t } = useLang();
  const params = useSearchParams();
  const applicationId = params.get("application_id") ?? "";
  const [app, setApp] = useState<OnboardingApplication | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pendingDoc, setPendingDoc] = useState<{ doc: KybDocument; pointer: string; sha256: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [modalError, setModalError] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const [selectedDocType, setSelectedDocType] = useState<string | null>(null);

  const load = useCallback(() => {
    const fetcher = applicationId !== "" ? getOnboardingApplication(applicationId) : getMyOnboardingApplication();
    fetcher
      .then((a) => {
        setApp(a);
        setError(null);
      })
      .catch((err) => setError(err instanceof ApiError ? err.message : t("error_generic")));
  }, [applicationId, t]);

  useEffect(() => {
    load();
  }, [load]);

  function pickFile(doc: KybDocument) {
    setSelectedDocType(doc.doc_type);
    fileInputRef.current?.click();
  }

  async function onFileChosen(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!file || !app || selectedDocType === null) return;
    const doc = app.documents.find((d) => d.doc_type === selectedDocType);
    if (!doc) return;
    const digest = await sha256OfFile(file);
    setPendingDoc({ doc, pointer: evidencePointer("bdpay-kyb", file.name, digest), sha256: digest });
    setModalError(null);
  }

  async function confirmUpload(totpCode: string) {
    if (!pendingDoc || !app) return;
    setBusy(true);
    setModalError(null);
    try {
      const updated = await uploadKybDocument(app.application_id, pendingDoc.doc.doc_type, pendingDoc.pointer, pendingDoc.sha256, totpCode);
      setApp(updated);
      setPendingDoc(null);
    } catch (err) {
      setModalError(err instanceof ApiError ? err.message : t("error_generic"));
    } finally {
      setBusy(false);
    }
  }

  if (error !== null) {
    return (
      <main className="content">
        <h1>{t("onboarding_title")}</h1>
        <p className="error-text">{error}</p>
        <p>
          <Link href="/signup">{t("signup_title")}</Link> · <Link href="/login">{t("login_title")}</Link>
        </p>
      </main>
    );
  }
  if (!app) {
    return <main className="centered">{t("loading")}</main>;
  }

  return (
    <main className="content">
      <h1>{t("onboarding_title")}</h1>
      <section className="panel">
        <div className="panel-head">
          <h2>
            {app.trade_name} <span lang="bn">({app.trade_name_bn})</span>
          </h2>
          <StateBadge value={app.kyb_status} />
        </div>
        <Tracker app={app} />
        {app.kyb_status !== "ACTIVE" && app.kyb_status !== "REJECTED" ? (
          <p className="notice">{t("activation_two_eyes")}</p>
        ) : null}
        <dl className="kv">
          <dt>Application</dt>
          <dd className="mono">{app.application_id}</dd>
          <dt>BIN</dt>
          <dd className="mono">{app.bin_number}</dd>
          <dt>Contact</dt>
          <dd>
            {app.contact_email} · {app.contact_phone}
          </dd>
          <dt>MCC / City</dt>
          <dd>
            {app.mcc} / {app.city}
          </dd>
          <dt>Submitted</dt>
          <dd>{formatTs(app.created_at)}</dd>
        </dl>
      </section>

      <section className="panel">
        <h2>{t("kyb_checklist")}</h2>
        <p className="subtle">{t("bn_numeral_note")}</p>
        <table className="data-table">
          <thead>
            <tr>
              <th>Document</th>
              <th>Status</th>
              <th>Pointer</th>
              <th>SHA-256</th>
              <th>Uploaded</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {app.documents.map((d) => (
              <tr key={d.doc_type}>
                <td>
                  {d.label_en}
                  <div className="subtle" lang="bn">
                    {d.label_bn}
                  </div>
                </td>
                <td>
                  <StateBadge value={d.status} />
                </td>
                <td className="mono">{d.pointer ? shortId(d.pointer, 30) : "—"}</td>
                <td className="mono">{d.sha256 ? shortId(d.sha256, 16) : "—"}</td>
                <td>{formatTs(d.uploaded_at)}</td>
                <td>
                  {d.status === "REQUIRED" || d.status === "REJECTED" ? (
                    <button className="btn btn-small btn-primary" onClick={() => pickFile(d)}>
                      {t("upload_document")}
                    </button>
                  ) : null}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <input type="file" ref={fileInputRef} onChange={onFileChosen} style={{ display: "none" }} aria-label={t("upload_document")} />
      </section>

      <section className="panel">
        <h2>State history</h2>
        <ul className="checklist">
          {app.state_history.map((h, i) => (
            <li key={`${h.state}-${i}`}>
              <StateBadge value={h.state} /> <span className="subtle">{formatTs(h.at)}</span>
            </li>
          ))}
        </ul>
      </section>

      {pendingDoc ? (
        <TotpModal
          titleKey="upload_document"
          messageKey="totp_prompt"
          busy={busy}
          error={modalError}
          onConfirm={(code) => void confirmUpload(code)}
          onClose={() => setPendingDoc(null)}
        />
      ) : null}
    </main>
  );
}

export default function OnboardingPage() {
  return (
    <Suspense fallback={<main className="centered">Loading…</main>}>
      <OnboardingInner />
    </Suspense>
  );
}

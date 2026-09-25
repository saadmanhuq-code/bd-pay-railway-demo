"use client";

// TOTP step-up confirmation modal. Safety-critical messaging renders via
// DualLabel — BOTH languages simultaneously (spec/15 §i18n).
//
// A factory, like createLangContext / createDualLabel: the modal needs the
// consuming app's own DualLabel component and useLang hook (each app has its
// own copy table and storage key), and a shared package must not reach back
// into an app through the app's "@/..." path alias. Each app instantiates it
// once in src/components/TotpModal.tsx and the pages import from there.

import React, { useState } from "react";

/** The copy keys this modal renders itself; every app's copy table carries them. */
export type TotpModalCopyKey = "reason" | "cancel" | "totp_prompt" | "confirm";

export interface TotpModalProps<TCopyKey extends string> {
  titleKey: TCopyKey;
  messageKey: TCopyKey;
  danger?: boolean;
  withReason?: boolean;
  busy: boolean;
  error: string | null;
  onConfirm: (totpCode: string, reason: string) => void;
  onClose: () => void;
}

export interface TotpModalDeps<TCopyKey extends string> {
  /** The app's DualLabel (from createDualLabel over the app's copy table). */
  DualLabel: (props: { k: TCopyKey | TotpModalCopyKey; className?: string }) => React.JSX.Element;
  /** The app's useLang hook (from createLangContext); only `t` is used. */
  useLang: () => { t: (key: TCopyKey | TotpModalCopyKey) => string };
}

export function createTotpModal<TCopyKey extends string>({
  DualLabel,
  useLang,
}: TotpModalDeps<TCopyKey>): (props: TotpModalProps<TCopyKey>) => React.JSX.Element {
  return function TotpModal({ titleKey, messageKey, danger, withReason, busy, error, onConfirm, onClose }: TotpModalProps<TCopyKey>) {
    const { t } = useLang();
    const [totp, setTotp] = useState("");
    const [reason, setReason] = useState("");

    return (
      <div className="modal-backdrop" role="dialog" aria-modal="true">
        <div className={`modal ${danger ? "modal-danger" : ""}`}>
          <h3>
            <DualLabel k={titleKey} />
          </h3>
          {/* The TOTP field below always carries the totp_prompt label, so a
              messageKey of totp_prompt would render the same sentence twice. */}
          {messageKey === "totp_prompt" ? null : (
            <p className="modal-message">
              <DualLabel k={messageKey} />
            </p>
          )}
          {withReason ? (
            <label className="field">
              <span>{t("reason")}</span>
              <textarea value={reason} onChange={(e) => setReason(e.target.value)} rows={2} aria-label={t("reason")} />
            </label>
          ) : null}
          <label className="field">
            <span>
              <DualLabel k="totp_prompt" />
            </span>
            <input
              value={totp}
              onChange={(e) => setTotp(e.target.value.replace(/[^0-9]/g, "").slice(0, 6))}
              inputMode="numeric"
              autoComplete="one-time-code"
              aria-label="TOTP code"
              className="totp-input"
            />
          </label>
          {error ? <p className="error-text">{error}</p> : null}
          <div className="modal-actions">
            <button className="btn" onClick={onClose} disabled={busy}>
              {t("cancel")}
            </button>
            <button
              className={`btn ${danger ? "btn-danger" : "btn-primary"}`}
              onClick={() => onConfirm(totp, reason)}
              disabled={busy || totp.length !== 6}
            >
              <DualLabel k="confirm" />
            </button>
          </div>
        </div>
      </div>
    );
  };
}

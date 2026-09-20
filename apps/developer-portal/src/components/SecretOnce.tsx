"use client";

// Display-once secret panel with a copy button. Used for API key secrets and
// webhook signing secrets — the value never appears again after dismissal.

import React, { useState } from "react";
import { DualLabel } from "@/lib/i18n/DualLabel";
import { useLang } from "@/lib/i18n/LangContext";

export function SecretOnce({ label, secret, onDismiss }: { label: string; secret: string; onDismiss: () => void }) {
  const { t } = useLang();
  const [copied, setCopied] = useState(false);

  async function copy() {
    try {
      await navigator.clipboard.writeText(secret);
      setCopied(true);
    } catch {
      setCopied(false);
    }
  }

  return (
    <div className="notice notice-danger secret-once">
      <h3>{label}</h3>
      <p>
        <DualLabel k="secret_once" />
      </p>
      <div className="secret-row">
        <code className="mono secret-value">{secret}</code>
        <button className="btn btn-small" onClick={copy}>
          {copied ? t("copied") : t("copy")}
        </button>
      </div>
      <div className="modal-actions">
        <button className="btn btn-primary" onClick={onDismiss}>
          {t("confirm")}
        </button>
      </div>
    </div>
  );
}

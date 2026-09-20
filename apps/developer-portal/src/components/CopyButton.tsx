"use client";

// Small copy-to-clipboard button (bilingual label via COPY).

import React, { useState } from "react";
import { useLang } from "@/lib/i18n/LangContext";

export function CopyButton({ value }: { value: string }) {
  const { t } = useLang();
  const [copied, setCopied] = useState(false);

  async function copy() {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
    } catch {
      setCopied(false);
    }
  }

  return (
    <button className="btn btn-small" onClick={() => void copy()}>
      {copied ? t("copied") : t("copy")}
    </button>
  );
}

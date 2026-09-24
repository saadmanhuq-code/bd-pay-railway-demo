"use client";

// Bare /pay is not a checkout — hosted links live at /pay/l/{public_code}.
// Without this page Next serves a stock 404, which reads as a broken demo.

import React from "react";
import Link from "next/link";
import { PublicShell } from "@/components/PublicShell";
import { useLang } from "@/lib/i18n/LangContext";

export default function PayIndexPage() {
  const { t } = useLang();
  return (
    <PublicShell>
      <div className="checkout-wrap">
        <section className="panel">
          <h1>{t("pay_index_title")}</h1>
          <p>{t("pay_index_body")}</p>
          <p className="subtle">{t("pay_index_hint")}</p>
          <Link className="btn btn-primary" href="/payment-links">
            {t("pay_index_cta")}
          </Link>
        </section>
      </div>
    </PublicShell>
  );
}

"use client";

// Redemption history — the customer-scoped GET /v1/payment-intents listing
// with the spec/18 read-only offer block on each row (errata S18-E19: no new
// endpoint; the gateway already scopes the customer principal to its own
// intents). Receipts always show gross / discount / net — spec/18:
// invisible-at-staff is the UX goal, invisible-on-receipt would be a
// consumer-protection problem.

import React, { useCallback, useEffect, useState } from "react";
import { listMyPaymentIntents } from "@/lib/api/client";
import type { PaymentIntentView, RedemptionState } from "@/lib/api/types";
import { formatBdt, formatBdtBn, formatTs } from "@/lib/format";
import { useSession } from "@/lib/useSession";
import { useLang } from "@/lib/i18n/LangContext";
import type { CopyKey } from "@/lib/i18n/copy";
import { Shell } from "@/components/Shell";

const STATE_KEYS: Record<RedemptionState, CopyKey> = {
  RESERVED: "state_RESERVED",
  APPLIED: "state_APPLIED",
  RELEASED: "state_RELEASED",
  REVERSED: "state_REVERSED",
  SETTLED: "state_SETTLED",
};

export default function HistoryPage() {
  const { session, loading } = useSession();
  const { lang, t } = useLang();
  const [rows, setRows] = useState<PaymentIntentView[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const money = (minor: number) => (lang === "bn" ? formatBdtBn(minor) : formatBdt(minor));

  const load = useCallback(() => {
    if (!session) return;
    listMyPaymentIntents({})
      .then((page) => {
        setRows(page.data);
        setError(null);
      })
      .catch(() => setError(t("error_generic")));
  }, [session, t]);

  useEffect(() => {
    load();
  }, [load]);

  if (loading || !session) {
    return (
      <Shell session={null}>
        <p>{t("loading")}</p>
      </Shell>
    );
  }

  return (
    <Shell session={session}>
      <h1>{t("history_title")}</h1>
      {error ? <p className="error-text">{error}</p> : null}
      {rows === null ? (
        <p>{t("loading")}</p>
      ) : rows.length === 0 ? (
        <p className="notice">{t("history_empty")}</p>
      ) : (
        <section className="panel">
          {rows.map((r) => (
            <div key={r.payment_intent_id} className="history-row">
              <div className="history-head">
                <strong>{lang === "bn" ? r.merchant_display_name_bn : r.merchant_display_name}</strong>
                {r.redemption_state !== null ? (
                  <span className={`state-badge state-${r.redemption_state}`}>
                    {t(STATE_KEYS[r.redemption_state])}
                  </span>
                ) : null}
              </div>
              <div className="subtle">
                {t("history_when")}: {formatTs(r.created_at)}
              </div>
              {r.offer !== null ? (
                <div className="history-amounts">
                  <span className="strike">{money(r.offer.gross_amount_minor)}</span>
                  <span className="discount-ink">-{money(r.offer.discount_minor)}</span>
                  <span className="net">{money(r.amount_minor)}</span>
                </div>
              ) : (
                <div className="history-amounts">
                  <span className="net">{money(r.amount_minor)}</span>
                  <span className="subtle">{r.status}</span>
                </div>
              )}
            </div>
          ))}
        </section>
      )}
    </Shell>
  );
}

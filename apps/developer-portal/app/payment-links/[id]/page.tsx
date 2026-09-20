"use client";

// Payment link detail (spec/16 §C): all fields, payment_intent_id once paid,
// cancel (ACTIVE → CANCELLED) with the bilingual DualLabel confirm pattern.

import React, { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { ApiError, cancelPaymentLink, getPaymentLink } from "@/lib/api/client";
import type { PaymentLink } from "@/lib/api/launchTypes";
import { CopyButton } from "@/components/CopyButton";
import { EnvBadge, StateBadge } from "@/components/Badges";
import { Shell } from "@/components/Shell";
import { TotpModal } from "@/components/TotpModal";
import { formatBdt, formatTs } from "@/lib/format";
import { useLang } from "@/lib/i18n/LangContext";

export default function PaymentLinkDetailPage() {
  const { t } = useLang();
  const params = useParams<{ id: string }>();
  const plinkId = typeof params.id === "string" ? params.id : "";
  const [link, setLink] = useState<PaymentLink | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pendingCancel, setPendingCancel] = useState(false);
  const [busy, setBusy] = useState(false);
  const [modalError, setModalError] = useState<string | null>(null);

  const load = useCallback(() => {
    if (plinkId === "") {
      setError(t("error_generic"));
      return;
    }
    getPaymentLink(plinkId)
      .then((l) => {
        setLink(l);
        setError(null);
      })
      .catch((err) => setError(err instanceof ApiError ? err.message : t("error_generic")));
  }, [plinkId, t]);

  useEffect(() => {
    load();
  }, [load]);

  async function confirmCancel(totpCode: string) {
    if (!link) return;
    setBusy(true);
    setModalError(null);
    try {
      const updated = await cancelPaymentLink(link.payment_link_id, totpCode);
      setLink(updated);
      setPendingCancel(false);
    } catch (err) {
      setModalError(err instanceof ApiError ? err.message : t("error_generic"));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Shell>
      <p>
        <Link href="/payment-links">← {t("nav_payment_links")}</Link>
      </p>
      {error ? <p className="error-text">{error}</p> : null}
      {!link && !error ? (
        <p>{t("loading")}</p>
      ) : !link ? null : (
        <>
          <div className="panel-head">
            <h1 className="mono">{link.payment_link_id}</h1>
            <StateBadge value={link.state} />
          </div>

          <section className="panel">
            <dl className="kv">
              <dt>{t("link_description")}</dt>
              <dd>{link.description}</dd>
              <dt>{t("checkout_amount")}</dt>
              <dd>
                {link.amount_minor !== null
                  ? formatBdt(link.amount_minor)
                  : link.amount_min_minor !== null && link.amount_max_minor !== null
                    ? `${formatBdt(link.amount_min_minor)} – ${formatBdt(link.amount_max_minor)}`
                    : "—"}
              </dd>
              <dt>Environment</dt>
              <dd>
                <EnvBadge env={link.env} />
              </dd>
              <dt>{t("link_single_use")}</dt>
              <dd>{link.single_use ? "yes" : "no"}</dd>
              <dt>{t("link_public_url")}</dt>
              <dd>
                <span className="actions-row">
                  <Link href={link.url} className="mono">
                    {link.url}
                  </Link>
                  <CopyButton value={link.url} />
                </span>
              </dd>
              <dt>Public code</dt>
              <dd className="mono">{link.public_code}</dd>
              <dt>Payment intent</dt>
              <dd className="mono">{link.payment_intent_id ?? "—"}</dd>
              <dt>Expires</dt>
              <dd>{formatTs(link.expires_at)}</dd>
              <dt>Created</dt>
              <dd>{formatTs(link.created_at)}</dd>
            </dl>
            {link.state === "ACTIVE" ? (
              <div className="actions-row">
                <button
                  className="btn btn-danger"
                  onClick={() => {
                    setModalError(null);
                    setPendingCancel(true);
                  }}
                >
                  {t("cancel_link")}
                </button>
              </div>
            ) : null}
          </section>

          {pendingCancel ? (
            <TotpModal
              titleKey="cancel_link"
              messageKey="cancel_link_confirm"
              danger
              busy={busy}
              error={modalError}
              onConfirm={(code) => void confirmCancel(code)}
              onClose={() => setPendingCancel(false)}
            />
          ) : null}
        </>
      )}
    </Shell>
  );
}

"use client";

// Hosted checkout — GET /pay/l/{public_code} (spec/16 §C). Public, no auth.
// ACTIVE links: merchant display name, fixed amount or bounded amount entry
// (Bengali-numeral tolerant), method selection, and a dynamic Bangla QR
// (spec/13 TLV payload) bound to the intent once created. Non-ACTIVE links
// render a friendly terminal page — NEVER an error envelope.

import React, { useCallback, useEffect, useState } from "react";
import { useParams } from "next/navigation";
import { ApiError, createPublicLinkIntent, getPublicPaymentLink } from "@/lib/api/client";
import type { CheckoutMethod, PublicLinkIntentResult, PublicPaymentLinkView } from "@/lib/api/launchTypes";
import { PublicShell } from "@/components/PublicShell";
import { formatBdt, formatTs, parseAmountToMinor } from "@/lib/format";
import { useLang } from "@/lib/i18n/LangContext";
import type { CopyKey } from "@/lib/i18n/copy";

const METHODS: { method: CheckoutMethod; labelKey: CopyKey }[] = [
  { method: "BANGLA_QR", labelKey: "method_bangla_qr" },
  { method: "CARD", labelKey: "method_card" },
  { method: "MFS", labelKey: "method_mfs" },
];

function TerminalPanel({ titleKey, bodyKey }: { titleKey: CopyKey; bodyKey: CopyKey }) {
  const { t } = useLang();
  return (
    <div className="checkout-wrap">
      <section className="panel">
        <h1>{t(titleKey)}</h1>
        <p>{t(bodyKey)}</p>
      </section>
    </div>
  );
}

export default function HostedCheckoutPage() {
  const { t } = useLang();
  const params = useParams<{ code: string }>();
  const code = typeof params.code === "string" ? params.code : "";

  const [view, setView] = useState<PublicPaymentLinkView | null>(null);
  const [notFound, setNotFound] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);

  const [amountRaw, setAmountRaw] = useState("");
  const [method, setMethod] = useState<CheckoutMethod>("BANGLA_QR");
  const [busy, setBusy] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [intent, setIntent] = useState<PublicLinkIntentResult | null>(null);

  const load = useCallback(() => {
    if (code === "") {
      setNotFound(true);
      return;
    }
    getPublicPaymentLink(code)
      .then((v) => {
        setView(v);
        setNotFound(false);
        setLoadError(null);
      })
      .catch((err) => {
        if (err instanceof ApiError && err.status === 404) {
          // Friendly terminal page — never render the error envelope.
          setNotFound(true);
          setLoadError(null);
        } else {
          setLoadError(t("error_generic"));
        }
      });
  }, [code, t]);

  useEffect(() => {
    load();
  }, [load]);

  const bounded = view !== null && view.amount_minor === null;
  const enteredMinor = bounded ? parseAmountToMinor(amountRaw) : view?.amount_minor ?? null;
  const outOfBounds =
    bounded &&
    enteredMinor !== null &&
    view !== null &&
    (BigInt(enteredMinor) < BigInt(view.amount_min_minor ?? "0") || BigInt(enteredMinor) > BigInt(view.amount_max_minor ?? "0"));

  async function onContinue() {
    if (!view || enteredMinor === null || outOfBounds) return;
    setBusy(true);
    setSubmitError(null);
    try {
      const result = await createPublicLinkIntent(view.public_code, bounded ? enteredMinor : null, method);
      setIntent(result);
    } catch (err) {
      setSubmitError(err instanceof ApiError ? err.message : t("error_generic"));
    } finally {
      setBusy(false);
    }
  }

  if (notFound) {
    return (
      <PublicShell>
        <TerminalPanel titleKey="link_not_found_title" bodyKey="link_not_found_body" />
      </PublicShell>
    );
  }

  if (view && view.state !== "ACTIVE") {
    const titleKey: CopyKey =
      view.state === "PAID" ? "link_paid_title" : view.state === "EXPIRED" ? "link_expired_title" : "link_cancelled_title";
    const bodyKey: CopyKey =
      view.state === "PAID" ? "link_paid_body" : view.state === "EXPIRED" ? "link_expired_body" : "link_cancelled_body";
    return (
      <PublicShell>
        <TerminalPanel titleKey={titleKey} bodyKey={bodyKey} />
      </PublicShell>
    );
  }

  return (
    <PublicShell>
      <div className="checkout-wrap">
        {loadError ? <p className="error-text">{loadError}</p> : null}
        {!view && !loadError ? (
          <p>{t("loading")}</p>
        ) : !view ? null : (
          <section className="panel">
            <h1>{t("checkout_title")}</h1>
            <dl className="kv">
              <dt>Merchant</dt>
              <dd>
                {view.merchant_display_name}{" "}
                <span lang="bn" className="subtle">
                  ({view.merchant_display_name_bn})
                </span>
              </dd>
              <dt>{t("link_description")}</dt>
              <dd>{view.description}</dd>
              <dt>{t("checkout_amount")}</dt>
              <dd>{view.amount_minor !== null ? formatBdt(view.amount_minor) : "—"}</dd>
              {bounded ? (
                <>
                  <dt>{t("checkout_bounds")}</dt>
                  <dd>
                    {formatBdt(view.amount_min_minor ?? "0")} – {formatBdt(view.amount_max_minor ?? "0")}
                  </dd>
                </>
              ) : null}
            </dl>

            {intent === null ? (
              <>
                {bounded ? (
                  <label className="field">
                    <span>{t("checkout_enter_amount")}</span>
                    <input value={amountRaw} onChange={(e) => setAmountRaw(e.target.value)} inputMode="decimal" />
                  </label>
                ) : null}
                {outOfBounds ? <p className="error-text">{t("amount_out_of_bounds")}</p> : null}

                <h2>{t("checkout_method")}</h2>
                <div className="method-row">
                  {METHODS.map((m) => (
                    <button
                      key={m.method}
                      className={`btn ${method === m.method ? "btn-primary" : ""}`}
                      aria-pressed={method === m.method}
                      onClick={() => setMethod(m.method)}
                    >
                      {t(m.labelKey)}
                    </button>
                  ))}
                </div>

                {submitError ? <p className="error-text">{submitError}</p> : null}
                <button
                  className="btn btn-primary"
                  disabled={busy || enteredMinor === null || outOfBounds}
                  onClick={() => void onContinue()}
                >
                  {t("checkout_continue")}
                  {enteredMinor !== null && !outOfBounds ? ` — ${formatBdt(enteredMinor)}` : ""}
                </button>
              </>
            ) : intent.next_action?.type === "redirect_to_url" ? (
              <>
                <p className="notice">{t("checkout_redirect_note")}</p>
                <dl className="kv">
                  <dt>{t("checkout_amount")}</dt>
                  <dd>{formatBdt(intent.amount_minor)}</dd>
                  <dt>Payment intent</dt>
                  <dd className="mono">{intent.payment_intent_id}</dd>
                </dl>
                <a className="btn btn-primary" href={intent.next_action.redirect_to_url}>
                  {t("checkout_continue")}
                </a>
              </>
            ) : intent.qr_payload ? (
              <>
                <h2>{t("checkout_qr_title")}</h2>
                <p className="subtle">{t("checkout_qr_note")}</p>
                {/* ENGINE-issued spec/13 dynamic TLV payload bound to the intent
                    (qr_payload from the intent envelope — never built client-side). */}
                <div className="qr-payload mono">{intent.qr_payload}</div>
                <dl className="kv">
                  <dt>{t("checkout_amount")}</dt>
                  <dd>{formatBdt(intent.amount_minor)}</dd>
                  <dt>Payment intent</dt>
                  <dd className="mono">{intent.payment_intent_id}</dd>
                  {intent.qr_payload_hash ? (
                    <>
                      <dt>Payload hash</dt>
                      <dd className="mono">{intent.qr_payload_hash}</dd>
                    </>
                  ) : null}
                  {intent.qr_expires_at ? (
                    <>
                      <dt>QR expires</dt>
                      <dd>{formatTs(intent.qr_expires_at)}</dd>
                    </>
                  ) : null}
                </dl>
              </>
            ) : (
              <>
                {/* Scan-only refusal: the engine created the intent without a
                    QR (qr_* fields absent) and gave no redirect — degrade
                    gracefully, never error. */}
                <p className="notice">{t("checkout_qr_unavailable")}</p>
                <dl className="kv">
                  <dt>{t("checkout_amount")}</dt>
                  <dd>{formatBdt(intent.amount_minor)}</dd>
                  <dt>Payment intent</dt>
                  <dd className="mono">{intent.payment_intent_id}</dd>
                </dl>
              </>
            )}
          </section>
        )}
      </div>
    </PublicShell>
  );
}

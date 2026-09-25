"use client";

// Sandbox payer authorisation — the mock simulator's stand-in for the card /
// MFS redirect of the hosted checkout (/pay/l/{code} → Continue to payment).
// Public, no auth. Only reachable in mock mode: the live engine returns the
// real acquirer / wallet redirect URL instead of this page.

import React, { Suspense, useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { useParams, useSearchParams } from "next/navigation";
import { ApiError, getPublicLinkIntent, simulatePublicLinkIntent } from "@/lib/api/client";
import type { PublicIntentSimView } from "@/lib/api/launchTypes";
import { PublicShell } from "@/components/PublicShell";
import { formatBdt, formatTs } from "@/lib/format";
import { useLang } from "@/lib/i18n/LangContext";

function SimulateInner() {
  const { t, lang } = useLang();
  const params = useParams<{ code: string }>();
  const search = useSearchParams();
  const code = typeof params.code === "string" ? params.code : "";
  const pi = search.get("pi") ?? "";

  const [view, setView] = useState<PublicIntentSimView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    if (code === "" || pi === "") {
      setError(t("link_not_found_body"));
      return;
    }
    getPublicLinkIntent(code, pi)
      .then((v) => {
        setView(v);
        setError(null);
      })
      .catch((err) => setError(err instanceof ApiError ? err.message : t("error_generic")));
  }, [code, pi, t]);

  useEffect(() => {
    load();
  }, [load]);

  async function decide(outcome: "succeed" | "fail") {
    setBusy(true);
    setError(null);
    try {
      setView(await simulatePublicLinkIntent(code, pi, outcome));
    } catch (err) {
      setError(err instanceof ApiError ? err.message : t("error_generic"));
    } finally {
      setBusy(false);
    }
  }

  const backHref = `/pay/l/${encodeURIComponent(code)}`;
  const methodLabel = view?.method === "CARD" ? t("method_card") : view?.method === "MFS" ? t("method_mfs") : t("method_bangla_qr");

  return (
    <div className="checkout-wrap">
      <p className="notice" role="status">
        {t("checkout_sandbox_banner")}
      </p>
      {error ? <p className="error-text">{error}</p> : null}
      {!view && !error ? <p>{t("loading")}</p> : null}
      {view ? (
        <section className="panel">
          <h1>
            {view.status === "SUCCEEDED"
              ? t("sim_result_success_title")
              : view.status === "FAILED"
                ? t("sim_result_failed_title")
                : t("sim_auth_title")}
          </h1>
          <dl className="kv">
            <dt>Merchant</dt>
            <dd>{lang === "bn" ? view.merchant_display_name_bn : view.merchant_display_name}</dd>
            <dt>{t("checkout_amount")}</dt>
            <dd>{formatBdt(view.amount_minor)}</dd>
            <dt>{t("checkout_method")}</dt>
            <dd>{methodLabel}</dd>
            <dt>Payment intent</dt>
            <dd className="mono">{view.payment_intent_id}</dd>
            {view.status !== "REQUIRES_ACTION" ? (
              <>
                <dt>{t("sim_completed_at")}</dt>
                <dd>{formatTs(view.updated_at)}</dd>
              </>
            ) : null}
          </dl>
          {view.status === "REQUIRES_ACTION" ? (
            <>
              <p className="subtle">{t("sim_auth_note")}</p>
              <div className="method-row">
                <button className="btn btn-primary" disabled={busy} onClick={() => void decide("succeed")}>
                  {t("sim_auth_approve")}
                </button>
                <button className="btn" disabled={busy} onClick={() => void decide("fail")}>
                  {t("sim_auth_decline")}
                </button>
              </div>
            </>
          ) : view.status === "SUCCEEDED" ? (
            <p className="notice" role="status">
              {t("sim_result_success_body")}
            </p>
          ) : (
            <>
              <p className="error-text">{t("sim_result_failed_body")}</p>
              <Link className="btn btn-primary" href={backHref}>
                {t("sim_back_to_link")}
              </Link>
            </>
          )}
        </section>
      ) : error ? (
        <Link className="btn" href={backHref}>
          {t("sim_back_to_link")}
        </Link>
      ) : null}
    </div>
  );
}

export default function SandboxAuthorisePage() {
  return (
    <PublicShell brandKey="checkout_brand">
      <Suspense fallback={null}>
        <SimulateInner />
      </Suspense>
    </PublicShell>
  );
}

"use client";

// EatClub-like digital diner card + tap-to-pay simulator.
// Honest copy: simulated / not a licensed Mastercard issuer; not live Apple/Google Pay.

import React, { Suspense, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { ApiError, demoDinerPay } from "@/lib/api/client";
import type { DinerPayResult, EligibleOffer } from "@/lib/api/types";
import { discoverMerchantOffers } from "@/lib/demo/discover";
import { computeDiscountMinor } from "@/lib/offers/engine";
import { formatBdt, formatBdtBn } from "@/lib/format";
import { useSession } from "@/lib/useSession";
import { useLang } from "@/lib/i18n/LangContext";
import { Shell } from "@/components/Shell";

function money(lang: "bn" | "en", minor: number): string {
  return lang === "bn" ? formatBdtBn(minor) : formatBdt(minor);
}

function last4FromCustomerId(id: string): string {
  const digits = id.replace(/\D/g, "");
  if (digits.length >= 4) return digits.slice(-4);
  const hash = Array.from(id).reduce((acc, ch) => (acc * 31 + ch.charCodeAt(0)) >>> 0, 7);
  return String(hash % 10000).padStart(4, "0");
}

const DEMO_BILL_MINOR = 120000; // ৳1,200 sample bill for tap sim

function WalletInner() {
  const { session, loading } = useSession(true);
  const { lang, t } = useLang();
  const search = useSearchParams();
  const merchantId = search.get("merchant_id") ?? "";
  const offerId = search.get("offer_id") ?? "";

  const [merchantName, setMerchantName] = useState<string>("");
  const [offer, setOffer] = useState<EligibleOffer | null>(null);
  const [phase, setPhase] = useState<"idle" | "holding" | "done">("idle");
  const [toast, setToast] = useState<string | null>(null);
  const [result, setResult] = useState<DinerPayResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!merchantId) return;
    discoverMerchantOffers(merchantId)
      .then((r) => {
        if (r.merchant) {
          setMerchantName(lang === "bn" ? r.merchant.display_name_bn : r.merchant.display_name);
        }
        const found = r.offers.find((o) => o.offer_id === offerId) ?? r.offers[0] ?? null;
        setOffer(found);
      })
      .catch(() => undefined);
  }, [merchantId, offerId, lang]);

  const preview = useMemo(() => {
    if (!offer || offer.percent_bps === null) return null;
    if (DEMO_BILL_MINOR < offer.min_spend_minor) return null;
    try {
      const discount = computeDiscountMinor(
        DEMO_BILL_MINOR,
        offer.percent_bps,
        offer.max_discount_minor,
      );
      return { discount, net: DEMO_BILL_MINOR - discount };
    } catch {
      return null;
    }
  }, [offer]);

  const last4 = session ? last4FromCustomerId(session.customer.customer_id) : "0000";

  function onWalletDemo() {
    setToast(t("wallet_wallet_toast"));
    window.setTimeout(() => setToast(null), 3200);
  }

  async function onTap() {
    if (!merchantId) {
      setError(t("wallet_tap_need_merchant"));
      return;
    }
    setError(null);
    setPhase("holding");
    await new Promise((r) => window.setTimeout(r, 1600));
    try {
      const amount = preview?.net ?? DEMO_BILL_MINOR;
      const done = await demoDinerPay({ merchant_id: merchantId, amount_minor: amount });
      setResult(done);
      setPhase("done");
    } catch (err) {
      setPhase("idle");
      if (err instanceof ApiError) {
        setError(err.message);
      } else {
        setError(t("error_generic"));
      }
    }
  }

  if (loading || !session) {
    return (
      <Shell session={null}>
        <p>{t("loading")}</p>
      </Shell>
    );
  }

  return (
    <Shell session={session}>
      <h1>{t("wallet_title")}</h1>
      <p className="subtle">{t("wallet_subtitle")}</p>
      <p className="notice demo-banner" role="status">
        {t("wallet_disclaimer")}
      </p>

      <div className="diner-card" aria-label={t("wallet_title")}>
        <div className="diner-card-top">
          <span className="diner-card-brand">{t("appName")}</span>
          <span className="diner-card-network">{t("wallet_card_network")}</span>
        </div>
        <div className="diner-card-chip" aria-hidden />
        <div className="diner-card-number">•••• •••• •••• {last4}</div>
        <div className="diner-card-footer">
          <div>
            <div className="diner-card-label">{t("wallet_card_holder")}</div>
            <div className="diner-card-holder">
              {session.customer.display_name || session.customer.phone_masked}
            </div>
          </div>
          <div className="diner-card-contactless" aria-hidden>
            )))
          </div>
        </div>
      </div>

      <div className="wallet-actions">
        <button type="button" className="btn btn-block" onClick={onWalletDemo}>
          {t("wallet_add_apple")}
        </button>
        <button type="button" className="btn btn-block" onClick={onWalletDemo}>
          {t("wallet_add_google")}
        </button>
      </div>
      {toast ? (
        <p className="notice" role="status">
          {toast}
        </p>
      ) : null}

      <section className="panel tap-panel">
        <h2>{t("wallet_tap_title")}</h2>
        {merchantId ? (
          <p className="subtle">
            {t("wallet_pick_offer")}: <strong>{merchantName || merchantId}</strong>
            {offer ? <> · {lang === "bn" ? offer.title_bn : offer.title}</> : null}
          </p>
        ) : (
          <p className="notice">{t("wallet_tap_need_merchant")}</p>
        )}
        {preview ? (
          <dl className="kv">
            <dt>{t("redeem_gross")}</dt>
            <dd>{money(lang, DEMO_BILL_MINOR)}</dd>
            <dt>{t("redeem_discount")}</dt>
            <dd className="discount-ink">-{money(lang, preview.discount)}</dd>
            <dt>{t("redeem_net")}</dt>
            <dd className="total">{money(lang, preview.net)}</dd>
          </dl>
        ) : null}

        {phase === "done" && result ? (
          <div className="notice notice-good">
            <p>{t("wallet_tap_success")}</p>
            <p className="mono subtle">{result.payment_intent_id}</p>
            <Link className="btn btn-block" href="/history">
              {t("receipt_view_history")}
            </Link>
          </div>
        ) : (
          <button
            type="button"
            className={`btn btn-primary btn-block tap-hold-btn ${phase === "holding" ? "holding" : ""}`}
            disabled={!merchantId || phase === "holding"}
            onClick={() => void onTap()}
          >
            {phase === "holding" ? t("wallet_tap_holding") : t("wallet_tap_hold")}
          </button>
        )}
        {error ? <p className="error-text">{error}</p> : null}
        {!merchantId ? (
          <Link className="btn btn-block" href="/">
            {t("nav_browse")}
          </Link>
        ) : (
          <Link className="btn btn-block" href={`/merchants/${merchantId}`}>
            {merchantName || t("nav_browse")}
          </Link>
        )}
      </section>
    </Shell>
  );
}

export default function WalletPage() {
  return (
    <Suspense fallback={<p>…</p>}>
      <WalletInner />
    </Suspense>
  );
}

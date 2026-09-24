"use client";

// Merchant offers + the spec/18 redemption flow:
//   discover (GET /v1/offers/eligible) -> reserve (POST /v1/payment-intents
//   with offer_id + gross_amount_minor; atomic fail-closed counters) ->
//   dynamic QR handoff (POST /v1/qr-codes/dynamic) -> confirm -> receipt.
// The discount is resolved BEFORE amount fixation; the QR carries the NET —
// no mid-flow repricing, ever. The client-side discount preview is display
// only; the server is authoritative (same floor math via the shared engine).

import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import {
  ApiError,
  confirmPaymentIntent,
  createOfferIntent,
  demoDinerPay,
  issueDynamicQr,
} from "@/lib/api/client";
import type {
  CheckoutMethod,
  DinerMerchant,
  DinerPayResult,
  DynamicQr,
  EligibleOffer,
  PaymentIntentView,
} from "@/lib/api/types";
import { computeDiscountMinor } from "@/lib/offers/engine";
import { formatBdt, formatBdtBn, formatTs, parseAmountToMinor, shortId } from "@/lib/format";
import { useSession } from "@/lib/useSession";
import { useLang } from "@/lib/i18n/LangContext";
import { DualLabel } from "@/lib/i18n/DualLabel";
import { REASON_COPY, REFUSAL_COPY, type CopyKey } from "@/lib/i18n/copy";
import { Shell } from "@/components/Shell";
import { QrCode } from "@/components/QrCode";
import { MerchantMap } from "@/components/MerchantMap";
import { discoverMerchantOffers } from "@/lib/demo/discover";
import { formatDistanceKm, haversineKm } from "@/lib/demo/geo";
import { useGeolocation } from "@/lib/useGeolocation";

const METHOD_KEYS: Record<CheckoutMethod, CopyKey> = {
  BANGLA_QR: "method_BANGLA_QR",
  BKASH: "method_BKASH",
  NAGAD: "method_NAGAD",
};

function money(lang: "bn" | "en", minor: number): string {
  return lang === "bn" ? formatBdtBn(minor) : formatBdt(minor);
}

export default function MerchantOffersPage() {
  const { session, loading } = useSession(false);
  const { lang, t } = useLang();
  const geo = useGeolocation();
  const params = useParams<{ merchant_id: string | string[] }>();
  const merchantId = Array.isArray(params.merchant_id)
    ? params.merchant_id.join("/")
    : typeof params.merchant_id === "string"
      ? params.merchant_id
      : "";

  const [merchant, setMerchant] = useState<DinerMerchant | null>(null);
  const [offers, setOffers] = useState<EligibleOffer[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  const [selected, setSelected] = useState<EligibleOffer | null>(null);
  const [amountRaw, setAmountRaw] = useState("");
  const [method, setMethod] = useState<CheckoutMethod>("BANGLA_QR");
  const [busy, setBusy] = useState(false);
  const [refusal, setRefusal] = useState<{ code: string; message: string } | null>(null);

  const [intent, setIntent] = useState<PaymentIntentView | null>(null);
  const [qr, setQr] = useState<DynamicQr | null>(null);
  const [receipt, setReceipt] = useState<PaymentIntentView | null>(null);

  const [demoAmountRaw, setDemoAmountRaw] = useState("");
  const [demoSuccess, setDemoSuccess] = useState<DinerPayResult | null>(null);
  const demoSuccessRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    if (demoSuccess) demoSuccessRef.current?.scrollIntoView({ behavior: "smooth", block: "center" });
  }, [demoSuccess]);
  const [source, setSource] = useState<"api" | "demo" | "osm">("api");

  const load = useCallback(() => {
    if (merchantId === "") {
      setLoadError(t("error_generic"));
      return;
    }
    discoverMerchantOffers(merchantId)
      .then((result) => {
        setMerchant(result.merchant);
        setOffers(result.offers);
        setSource(result.source);
        setLoadError(null);
      })
      .catch(() => setLoadError(t("error_generic")));
  }, [merchantId, t]);

  useEffect(() => {
    load();
  }, [load]);

  const grossMinorStr = parseAmountToMinor(amountRaw);
  const grossMinor =
    grossMinorStr !== null && Number.isSafeInteger(Number(grossMinorStr))
      ? Number(grossMinorStr)
      : null;

  // Display-only preview — server-authoritative on reserve.
  const preview = useMemo(() => {
    if (!selected || grossMinor === null || grossMinor <= 0) return null;
    if (grossMinor < selected.min_spend_minor) {
      return { kind: "min_spend" as const };
    }
    if (selected.percent_bps === null) return null;
    try {
      const discount = computeDiscountMinor(
        grossMinor,
        selected.percent_bps,
        selected.max_discount_minor,
      );
      return { kind: "ok" as const, discount, net: grossMinor - discount };
    } catch {
      return { kind: "invalid" as const };
    }
  }, [selected, grossMinor]);

  function selectOffer(offer: EligibleOffer) {
    setSelected(offer);
    setRefusal(null);
    setIntent(null);
    setQr(null);
    setReceipt(null);
    const first = offer.allowed_methods[0];
    if (first && !offer.allowed_methods.includes(method)) setMethod(first);
  }

  async function onReserve() {
    if (!selected || grossMinor === null || preview?.kind !== "ok") return;
    setBusy(true);
    setRefusal(null);
    try {
      const created = await createOfferIntent({
        merchant_id: merchantId,
        offer_id: selected.offer_id,
        gross_amount_minor: grossMinor,
        method,
      });
      setIntent(created);
      // The spec handoff: reservation held -> dynamic-amount QR (net only).
      const issued = await issueDynamicQr(created.payment_intent_id);
      setQr(issued);
    } catch (err) {
      if (err instanceof ApiError) {
        setRefusal({ code: err.code ?? "internal", message: err.message });
      } else {
        setRefusal({ code: "internal", message: t("error_generic") });
      }
    } finally {
      setBusy(false);
    }
  }

  async function onConfirm() {
    if (!intent) return;
    setBusy(true);
    try {
      const done = await confirmPaymentIntent(intent.payment_intent_id);
      setReceipt(done);
    } catch (err) {
      if (err instanceof ApiError) {
        setRefusal({ code: err.code ?? "internal", message: err.message });
      } else {
        setRefusal({ code: "internal", message: t("error_generic") });
      }
    } finally {
      setBusy(false);
    }
  }

  const demoMinorStr = parseAmountToMinor(demoAmountRaw);
  const demoMinor =
    demoMinorStr !== null && Number.isSafeInteger(Number(demoMinorStr))
      ? Number(demoMinorStr)
      : null;

  async function onDemoPay() {
    if (demoMinor === null || demoMinor <= 0) return;
    setBusy(true);
    setRefusal(null);
    setDemoSuccess(null);
    try {
      const done = await demoDinerPay({ merchant_id: merchantId, amount_minor: demoMinor });
      setDemoSuccess(done);
    } catch (err) {
      if (err instanceof ApiError) {
        setRefusal({ code: err.code ?? "internal", message: err.message });
      } else {
        setRefusal({ code: "internal", message: t("error_generic") });
      }
    } finally {
      setBusy(false);
    }
  }

  function refusalCopy(code: string): { bn: string; en: string } | null {
    if (code in REFUSAL_COPY) {
      const bi = REFUSAL_COPY[code as keyof typeof REFUSAL_COPY];
      return { bn: bi.bn, en: bi.en };
    }
    return null;
  }

  if (loading) {
    return (
      <Shell session={null}>
        <p>{t("loading")}</p>
      </Shell>
    );
  }

  return (
    <Shell session={session}>
      {source === "osm" ? (
        <p className="notice demo-banner" role="status">
          {t("osm_catalog_banner")}
        </p>
      ) : source === "demo" ? (
        <p className="notice demo-banner" role="status">
          {t("demo_catalog_banner")}
        </p>
      ) : null}
      {loadError ? <p className="error-text">{loadError}</p> : null}
      {merchant ? (
        <>
          <h1>{lang === "bn" ? merchant.display_name_bn : merchant.display_name}</h1>
          <p className="subtle">
            {lang === "bn"
              ? `${merchant.area_bn} · ${merchant.cuisine_bn}`
              : `${merchant.area} · ${merchant.cuisine}`}
            {geo.position && merchant.lat != null && merchant.lng != null
              ? ` · ${formatDistanceKm(
                  haversineKm(geo.position, { lat: merchant.lat, lng: merchant.lng }),
                  lang,
                )} ${t("distance_away")}`
              : null}
          </p>
          {merchant.rating_avg != null ? (
            <p className="subtle">
              ★ {merchant.rating_avg.toFixed(1)} · {merchant.rating_count ?? 0}{" "}
              {t("rating_label")}
            </p>
          ) : null}

        </>
      ) : null}

      {/* ----- receipt (terminal panel) ----------------------------------- */}
      {receipt && receipt.offer ? (
        <section className="panel">
          <h2>{t("receipt_title")}</h2>
          <p className="notice notice-good">{t("receipt_applied")}</p>
          <dl className="kv">
            <dt>{t("receipt_merchant")}</dt>
            <dd>{lang === "bn" ? receipt.merchant_display_name_bn : receipt.merchant_display_name}</dd>
            <dt>{t("redeem_gross")}</dt>
            <dd>{money(lang, receipt.offer.gross_amount_minor)}</dd>
            <dt>{t("redeem_discount")}</dt>
            <dd className="discount-ink">-{money(lang, receipt.offer.discount_minor)}</dd>
            <dt>{t("redeem_net")}</dt>
            <dd className="total">{money(lang, receipt.amount_minor)}</dd>
            <dt>{t("history_state")}</dt>
            <dd>
              <span className={`state-badge state-${receipt.redemption_state ?? "RESERVED"}`}>
                {receipt.redemption_state === "APPLIED" ? t("state_APPLIED") : receipt.redemption_state}
              </span>
            </dd>
            <dt>{t("qr_payment_intent")}</dt>
            <dd className="mono">{shortId(receipt.payment_intent_id, 20)}</dd>
          </dl>
          <Link className="btn btn-block" href="/history">
            {t("receipt_view_history")}
          </Link>
        </section>
      ) : intent && qr ? (
        /* ----- QR handoff -------------------------------------------------- */
        <section className="panel">
          <h2>{t("qr_title")}</h2>
          <p className="subtle">{t("qr_note")}</p>
          {/* ENGINE-issued spec/13 dynamic TLV payload bound to the intent —
              never built client-side. */}
          <QrCode payload={qr.payload} label={t("qr_title")} />
          <details className="qr-payload-secondary">
            <summary className="subtle">EMVCo payload (engine-issued)</summary>
            <div className="qr-payload mono">{qr.payload}</div>
          </details>
          <dl className="kv">
            {intent.offer ? (
              <>
                <dt>{t("redeem_gross")}</dt>
                <dd>{money(lang, intent.offer.gross_amount_minor)}</dd>
                <dt>{t("redeem_discount")}</dt>
                <dd className="discount-ink">-{money(lang, intent.offer.discount_minor)}</dd>
              </>
            ) : null}
            <dt>{t("redeem_net")}</dt>
            <dd className="total">{money(lang, qr.amount_minor)}</dd>
            <dt>{t("qr_expires")}</dt>
            <dd>{formatTs(qr.expires_at)}</dd>
            <dt>{t("qr_payment_intent")}</dt>
            <dd className="mono">{shortId(intent.payment_intent_id, 20)}</dd>
          </dl>
          <p className="notice">
            <DualLabel k="redeem_hold_notice" />
          </p>
          {refusal ? (
            <p className="error-text">
              {refusalCopy(refusal.code)
                ? lang === "bn"
                  ? refusalCopy(refusal.code)!.bn
                  : refusalCopy(refusal.code)!.en
                : refusal.message}
            </p>
          ) : null}
          <button className="btn btn-primary btn-block" disabled={busy} onClick={() => void onConfirm()}>
            {busy ? t("qr_confirming") : t("qr_confirm_demo")}
          </button>
        </section>
      ) : (
        /* ----- offer list + reserve panel ---------------------------------- */
        <>
          <h2>{t("offers_title")}</h2>
          {offers === null ? (
            <p>{t("loading")}</p>
          ) : offers.length === 0 ? (
            <p className="notice">{t("offers_none")}</p>
          ) : (
            offers.map((offer) => (
              <div
                key={offer.offer_id}
                className={`offer-card ${selected?.offer_id === offer.offer_id ? "selected" : ""}`}
              >
                <div className="history-head">
                  <div>
                    <div className="offer-title" lang="bn">
                      {lang === "bn" ? offer.title_bn : offer.title}
                    </div>
                    <div className="offer-title-secondary">
                      {lang === "bn" ? offer.title : offer.title_bn}
                    </div>
                  </div>
                  {offer.percent_bps !== null ? (
                    <span className="offer-badge">
                      {Math.floor(offer.percent_bps / 100)}% {t("browse_discount_off")}
                    </span>
                  ) : null}
                </div>
                <div className="offer-facts">
                  <span>
                    {t("offer_min_spend")}: {money(lang, offer.min_spend_minor)}
                  </span>
                  {offer.max_discount_minor !== null ? (
                    <span>
                      {t("offer_max_discount")}: {money(lang, offer.max_discount_minor)}
                    </span>
                  ) : null}
                  <span>
                    {t("offer_valid_until")}: {formatTs(offer.valid_until)}
                  </span>
                </div>
                <div>
                  <span className="subtle">{t("offer_windows")}: </span>
                  {offer.windows.map((w, i) => (
                    <span key={i} className="window-tag">
                      {w.days.map((d) => t(`day_${d}` as CopyKey)).join(", ")} {w.start_local}–{w.end_local}
                    </span>
                  ))}
                </div>
                <div className="offer-facts">
                  <span>
                    {t("offer_methods")}:{" "}
                    {offer.allowed_methods.map((m) => t(METHOD_KEYS[m])).join(", ")}
                  </span>
                </div>
                {session ? (
                  <>
                    <button className="btn btn-primary btn-block" onClick={() => selectOffer(offer)}>
                      {t("offer_select")}
                    </button>
                    <Link
                      className="btn btn-block"
                      href={`/wallet?merchant_id=${encodeURIComponent(merchantId)}&offer_id=${encodeURIComponent(offer.offer_id)}`}
                    >
                      {t("wallet_add")}
                    </Link>
                  </>
                ) : (
                  <Link
                    className="btn btn-primary btn-block"
                    href={`/login?next=${encodeURIComponent(`/merchants/${merchantId}`)}`}
                  >
                    {t("sign_in_to_redeem")}
                  </Link>
                )}
              </div>
            ))
          )}

          
          {!session ? (
            <p className="notice">
              {t("sign_in_to_redeem_hint")}{" "}
              <Link href={`/login?next=${encodeURIComponent(`/merchants/${merchantId}`)}`}>
                {t("sign_in")}
              </Link>
            </p>
          ) : null}

          {selected && session ? (
            <section className="panel">
              <h2>{t("redeem_title")}</h2>
              <p className="subtle" lang="bn">
                {lang === "bn" ? selected.title_bn : selected.title}
              </p>
              <label className="field">
                <span>{t("redeem_gross_label")}</span>
                <input
                  value={amountRaw}
                  onChange={(e) => setAmountRaw(e.target.value)}
                  inputMode="decimal"
                />
              </label>
              <p className="hint">{t("redeem_gross_hint")}</p>

              <div className="method-row">
                {selected.allowed_methods.map((m) => (
                  <button
                    key={m}
                    className={`btn ${method === m ? "btn-primary" : ""}`}
                    aria-pressed={method === m}
                    onClick={() => setMethod(m)}
                  >
                    {t(METHOD_KEYS[m])}
                  </button>
                ))}
              </div>

              {amountRaw.trim() !== "" && grossMinor === null ? (
                <p className="error-text">{t("redeem_amount_invalid")}</p>
              ) : null}
              {preview?.kind === "min_spend" ? (
                <ul className="reason-list">
                  <li lang="bn">
                    {REASON_COPY.MIN_SPEND_NOT_MET.bn}
                    <span className="reason-en">{REASON_COPY.MIN_SPEND_NOT_MET.en}</span>
                  </li>
                </ul>
              ) : null}
              {preview?.kind === "ok" ? (
                <dl className="kv">
                  <dt>{t("redeem_gross")}</dt>
                  <dd>{money(lang, grossMinor ?? 0)}</dd>
                  <dt>{t("redeem_discount")}</dt>
                  <dd className="discount-ink">-{money(lang, preview.discount)}</dd>
                  <dt>{t("redeem_net")}</dt>
                  <dd className="total">{money(lang, preview.net)}</dd>
                </dl>
              ) : null}

              {refusal ? (
                <p className="error-text">
                  {refusalCopy(refusal.code)
                    ? lang === "bn"
                      ? refusalCopy(refusal.code)!.bn
                      : refusalCopy(refusal.code)!.en
                    : refusal.message}
                </p>
              ) : null}

              <p className="notice">
                <DualLabel k="redeem_hold_notice" />
              </p>
              <button
                className="btn btn-primary btn-block"
                disabled={busy || preview?.kind !== "ok"}
                onClick={() => void onReserve()}
              >
                {busy ? t("redeem_reserving") : t("redeem_reserve")}
              </button>
            </section>
          ) : null}

          {/* ----- sandbox demo one-tap pay ------------------------------------- */}
          <section className="panel">
            <h2>{t("demo_pay_title")}</h2>
            <p className="subtle">
              <DualLabel k="demo_pay_note" />
            </p>
            <label className="field">
              <span>{t("demo_pay_amount_label")}</span>
              <input
                value={demoAmountRaw}
                onChange={(e) => setDemoAmountRaw(e.target.value)}
                inputMode="decimal"
              />
            </label>
            {demoAmountRaw.trim() !== "" && demoMinor === null ? (
              <p className="error-text">{t("redeem_amount_invalid")}</p>
            ) : null}
            {demoSuccess ? (
              <div className="notice notice-good" ref={demoSuccessRef} role="status">
                <p>{t("demo_pay_success")}</p>
                <Link className="btn btn-block" href="/history">
                  {t("receipt_view_history")}
                </Link>
              </div>
            ) : null}
            {refusal && !selected ? (
              <p className="error-text">
                {refusalCopy(refusal.code)
                  ? lang === "bn"
                    ? refusalCopy(refusal.code)!.bn
                    : refusalCopy(refusal.code)!.en
                  : refusal.message}
              </p>
            ) : null}
            <button
              className="btn btn-primary btn-block"
              disabled={busy || demoMinor === null}
              onClick={() => void onDemoPay()}
            >
              {busy ? t("demo_pay_busy") : t("demo_pay_button")}
            </button>
          </section>
        </>
      )}
      {/* Venue context below offers — product CTA stays above the fold. */}
      {merchant ? (
        <>
          {(merchant.photo_urls ?? []).length > 0 ? (
            <section className="venue-photos" aria-label={t("photos_title")}>
              <h2 className="venue-section-title">{t("photos_title")}</h2>
              <div className="photo-strip">
                {(merchant.photo_urls ?? []).map((url) => (
                  // eslint-disable-next-line @next/next/no-img-element
                  <img
                    key={url}
                    src={url}
                    alt=""
                    className="venue-photo"
                    loading="lazy"
                    onError={(e) => {
                      e.currentTarget.style.display = "none";
                    }}
                  />
                ))}
              </div>
            </section>
          ) : null}

          {merchant.lat != null && merchant.lng != null ? (
            <section className="panel venue-map-panel">
              <h2 className="venue-section-title">{t("map_title")}</h2>
              <MerchantMap
                lat={merchant.lat}
                lng={merchant.lng}
                label={lang === "bn" ? merchant.display_name_bn : merchant.display_name}
              />
              {!geo.position ? (
                <button type="button" className="btn btn-block" onClick={() => geo.request()}>
                  {geo.status === "prompting" ? t("near_me_locating") : t("near_me")}
                </button>
              ) : null}
            </section>
          ) : null}

          {(merchant.menu_sections ?? []).length > 0 ? (
            <section className="panel">
              <h2 className="venue-section-title">{t("menu_title")}</h2>
              {(merchant.menu_sections ?? []).map((section) => (
                <div key={section.title} className="menu-section">
                  <h3 className="menu-section-title">
                    {lang === "bn" ? section.title_bn : section.title}
                  </h3>
                  <ul className="menu-list">
                    {section.items.map((item) => (
                      <li key={item.name} className="menu-item">
                        <span>{lang === "bn" ? item.name_bn : item.name}</span>
                        <span className="menu-price">{money(lang, item.price_minor)}</span>
                      </li>
                    ))}
                  </ul>
                </div>
              ))}
            </section>
          ) : null}

          {(merchant.reviews ?? []).length > 0 ? (
            <section className="panel">
              <h2 className="venue-section-title">{t("reviews_title")}</h2>
              <p className="subtle">{t("reviews_demo_note")}</p>
              {(merchant.reviews ?? []).map((r, i) => (
                <blockquote key={i} className="review-card">
                  <div className="review-head">
                    <strong>{lang === "bn" ? r.author_bn : r.author}</strong>
                    <span className="review-stars">{"★".repeat(r.stars)}{"☆".repeat(5 - r.stars)}</span>
                  </div>
                  <p>{lang === "bn" ? r.text_bn : r.text}</p>
                </blockquote>
              ))}
            </section>
          ) : null}
        </>
      ) : null}

    </Shell>
  );
}

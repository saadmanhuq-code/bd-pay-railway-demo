"use client";

// Dining-wedge offers (spec/18 merchant surface): list + create.
// Every economic parameter is merchant-set (opt-in, merchant-priced yield
// management — research/08); at least one redemption cap is mandatory.
// Create posts via mutate() with Idempotency-Key + TOTP step-up like every
// merchant mutation. Client-side validation is convenience only — the server
// re-validates every rule and is authoritative.

import React, { useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { ApiError, createOffer, listOffers } from "@/lib/api/client";
import {
  OFFER_METHODS,
  PERCENT_BPS_CEILING,
  PERCENT_BPS_FLOOR,
  WINDOW_DAY_CODES,
  type CreateOfferInput,
  type Offer,
  type OfferMethod,
  type OfferWindow,
  type WindowDay,
} from "@/lib/api/offerTypes";
import { validateCreateForm } from "@/lib/offers/validate";
import { StateBadge } from "@/components/Badges";
import { Shell } from "@/components/Shell";
import { TotpModal } from "@/components/TotpModal";
import { formatBdt, formatTs, parseAmountToMinor, shortId } from "@/lib/format";
import { useLang } from "@/lib/i18n/LangContext";

interface WindowDraft {
  days: WindowDay[];
  start_local: string;
  end_local: string;
}

const EMPTY_WINDOW: WindowDraft = { days: [], start_local: "", end_local: "" };

// Percent input ("25" or "12.5") -> integer bps via the same string math as
// BDT parsing (two "decimals" = bps); no floats on any numeric path.
function percentToBps(raw: string): number | null {
  const minor = parseAmountToMinor(raw);
  if (minor === null) return null;
  const n = Number(minor);
  return Number.isSafeInteger(n) ? n : null;
}

function bdtToMinorNumber(raw: string): number | null {
  const minor = parseAmountToMinor(raw);
  if (minor === null) return null;
  const n = Number(minor);
  return Number.isSafeInteger(n) ? n : null;
}

function intOrNull(raw: string): number | null {
  const t = raw.trim();
  if (t === "") return null;
  return /^\d+$/.test(t) ? Number(t) : null;
}

function capsCell(o: Offer): string {
  const parts: string[] = [];
  if (o.cap_per_day !== null) parts.push(`${o.cap_per_day}/day`);
  if (o.cap_total !== null) parts.push(`${o.cap_total} total`);
  if (o.cap_per_customer !== null) parts.push(`${o.cap_per_customer}/cust`);
  return parts.join(" · ");
}

function windowsCell(o: Offer): string {
  return o.windows.map((w) => `${w.days.join(",")} ${w.start_local}–${w.end_local}`).join(" | ");
}

export default function OffersPage() {
  const { t, lang } = useLang();
  const [offerRows, setOfferRows] = useState<Offer[]>([]);
  const [stateFilter, setStateFilter] = useState("");
  const [error, setError] = useState<string | null>(null);

  // Create form state — every economic parameter merchant-set.
  const [percentRaw, setPercentRaw] = useState("25");
  const [title, setTitle] = useState("");
  const [titleBn, setTitleBn] = useState("");
  const [windows, setWindows] = useState<WindowDraft[]>([{ ...EMPTY_WINDOW }]);
  const [validFrom, setValidFrom] = useState("");
  const [validUntil, setValidUntil] = useState("");
  const [minSpendRaw, setMinSpendRaw] = useState("");
  const [maxDiscountRaw, setMaxDiscountRaw] = useState("");
  const [capPerDayRaw, setCapPerDayRaw] = useState("");
  const [capTotalRaw, setCapTotalRaw] = useState("");
  const [capPerCustomerRaw, setCapPerCustomerRaw] = useState("");
  const [methods, setMethods] = useState<OfferMethod[]>(["BANGLA_QR"]);
  const [capRecredit, setCapRecredit] = useState(true);

  const [pendingCreate, setPendingCreate] = useState(false);
  const [busy, setBusy] = useState(false);
  const [modalError, setModalError] = useState<string | null>(null);
  const [created, setCreated] = useState<Offer | null>(null);

  const load = useCallback(() => {
    listOffers(stateFilter)
      .then((page) => {
        setOfferRows(page.data);
        setError(null);
      })
      .catch((err) => setError(err instanceof ApiError ? err.message : t("error_generic")));
  }, [stateFilter, t]);

  useEffect(() => {
    load();
  }, [load]);

  const percentBps = percentToBps(percentRaw);
  const minSpendMinor = minSpendRaw.trim() === "" ? null : bdtToMinorNumber(minSpendRaw);
  const maxDiscountMinor = maxDiscountRaw.trim() === "" ? null : bdtToMinorNumber(maxDiscountRaw);
  const capPerDay = intOrNull(capPerDayRaw);
  const capTotal = intOrNull(capTotalRaw);
  const capPerCustomer = intOrNull(capPerCustomerRaw);

  // RFC3339 wire timestamps from the date inputs: from-midnight to end-of-day.
  const validFromIso = validFrom === "" ? "" : `${validFrom}T00:00:00Z`;
  const validUntilIso = validUntil === "" ? "" : `${validUntil}T23:59:59Z`;

  const issues = useMemo(
    () =>
      validateCreateForm({
        percentBps,
        title,
        titleBn,
        windows: windows as OfferWindow[],
        validFrom: validFromIso,
        validUntil: validUntilIso,
        minSpendMinor,
        maxDiscountMinor,
        capPerDay,
        capTotal,
        capPerCustomer,
        allowedMethods: methods,
      }),
    [percentBps, title, titleBn, windows, validFromIso, validUntilIso, minSpendMinor, maxDiscountMinor, capPerDay, capTotal, capPerCustomer, methods],
  );
  const formInvalid = issues.length > 0;

  function toggleWindowDay(rowIdx: number, day: WindowDay) {
    setWindows((rows) =>
      rows.map((w, i) =>
        i === rowIdx ? { ...w, days: w.days.includes(day) ? w.days.filter((d) => d !== day) : [...w.days, day] } : w,
      ),
    );
  }

  function setWindowTime(rowIdx: number, field: "start_local" | "end_local", value: string) {
    setWindows((rows) => rows.map((w, i) => (i === rowIdx ? { ...w, [field]: value } : w)));
  }

  function toggleMethod(m: OfferMethod) {
    setMethods((ms) => (ms.includes(m) ? ms.filter((x) => x !== m) : [...ms, m]));
  }

  async function confirmCreate(totpCode: string) {
    setBusy(true);
    setModalError(null);
    const input: CreateOfferInput = {
      kind: "PERCENT_OFF", // D2: pilot is PERCENT_OFF-only; BOGO build-deferred
      percent_bps: percentBps,
      title,
      title_bn: titleBn,
      windows: windows as OfferWindow[],
      valid_from: validFromIso,
      valid_until: validUntilIso,
      min_spend_minor: minSpendMinor,
      max_discount_minor: maxDiscountMinor,
      cap_per_day: capPerDay,
      cap_total: capTotal,
      cap_per_customer: capPerCustomer,
      allowed_methods: methods,
      cap_recredit_on_refund: capRecredit,
    };
    try {
      const offer = await createOffer(input, totpCode);
      setCreated(offer);
      setTitle("");
      setTitleBn("");
      setWindows([{ ...EMPTY_WINDOW }]);
      setPendingCreate(false);
      load();
    } catch (err) {
      setModalError(err instanceof ApiError ? err.message : t("error_generic"));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Shell>
      <h1>{t("nav_offers")}</h1>
      {error ? <p className="error-text">{error}</p> : null}

      {created ? (
        <div className="notice">
          <strong className="mono">{created.offer_id}</strong> — <StateBadge value={created.state} />{" "}
          <Link href={`/offers/${encodeURIComponent(created.offer_id)}`}>{t("offer_analytics")} →</Link>
        </div>
      ) : null}

      <section className="panel">
        <h2>{t("create_offer")}</h2>
        <p className="subtle">{t("offer_d2_note")}</p>

        <div className="toolbar">
          <label className="field">
            <span>{t("offer_percent")}</span>
            <input value={percentRaw} onChange={(e) => setPercentRaw(e.target.value)} inputMode="decimal" />
          </label>
          <label className="field">
            <span>{t("offer_title_en")}</span>
            <input value={title} onChange={(e) => setTitle(e.target.value)} />
          </label>
          <label className="field">
            <span>{t("offer_title_bn")}</span>
            <input value={titleBn} onChange={(e) => setTitleBn(e.target.value)} lang="bn" />
          </label>
        </div>
        <p className="subtle">
          {PERCENT_BPS_FLOOR / 100}%–{PERCENT_BPS_CEILING / 100}% · {t("bn_numeral_note")}
        </p>

        <h3>{t("offer_windows")}</h3>
        <p className="subtle">{t("offer_window_tz_note")}</p>
        {windows.map((w, i) => (
          <div className="actions-row" key={i}>
            {WINDOW_DAY_CODES.map((day) => (
              <label className="subtle" key={day}>
                <input type="checkbox" checked={w.days.includes(day)} onChange={() => toggleWindowDay(i, day)} /> {day}
              </label>
            ))}
            <label className="field">
              <span>Start (HH:MM)</span>
              <input value={w.start_local} onChange={(e) => setWindowTime(i, "start_local", e.target.value)} />
            </label>
            <label className="field">
              <span>End (HH:MM)</span>
              <input value={w.end_local} onChange={(e) => setWindowTime(i, "end_local", e.target.value)} />
            </label>
            {windows.length > 1 ? (
              <button className="btn btn-small" onClick={() => setWindows((rows) => rows.filter((_, j) => j !== i))}>
                {t("offer_window_remove")}
              </button>
            ) : null}
          </div>
        ))}
        {windows.length < 7 ? (
          <button className="btn btn-small" onClick={() => setWindows((rows) => [...rows, { ...EMPTY_WINDOW }])}>
            {t("offer_window_add")}
          </button>
        ) : null}

        <div className="toolbar">
          <label className="field">
            <span>{t("offer_valid_from")}</span>
            <input type="date" value={validFrom} onChange={(e) => setValidFrom(e.target.value)} />
          </label>
          <label className="field">
            <span>{t("offer_valid_until")}</span>
            <input type="date" value={validUntil} onChange={(e) => setValidUntil(e.target.value)} />
          </label>
          <label className="field">
            <span>{t("offer_min_spend")}</span>
            <input value={minSpendRaw} onChange={(e) => setMinSpendRaw(e.target.value)} inputMode="decimal" />
          </label>
          <label className="field">
            <span>{t("offer_max_discount")}</span>
            <input value={maxDiscountRaw} onChange={(e) => setMaxDiscountRaw(e.target.value)} inputMode="decimal" />
          </label>
        </div>
        <p className="subtle">{t("offer_validity_note")}</p>

        <h3>{t("offer_caps")}</h3>
        <p className="subtle">{t("offer_cap_required_note")}</p>
        <div className="toolbar">
          <label className="field">
            <span>{t("offer_cap_per_day")}</span>
            <input value={capPerDayRaw} onChange={(e) => setCapPerDayRaw(e.target.value)} inputMode="numeric" />
          </label>
          <label className="field">
            <span>{t("offer_cap_total")}</span>
            <input value={capTotalRaw} onChange={(e) => setCapTotalRaw(e.target.value)} inputMode="numeric" />
          </label>
          <label className="field">
            <span>{t("offer_cap_per_customer")}</span>
            <input value={capPerCustomerRaw} onChange={(e) => setCapPerCustomerRaw(e.target.value)} inputMode="numeric" />
          </label>
        </div>
        <div className="actions-row">
          <label className="subtle">
            <input type="checkbox" checked={capRecredit} onChange={(e) => setCapRecredit(e.target.checked)} /> {t("offer_cap_recredit")}
          </label>
        </div>

        <h3>{t("offer_methods")}</h3>
        <div className="actions-row">
          {OFFER_METHODS.map((m) => (
            <label className="subtle" key={m}>
              <input type="checkbox" checked={methods.includes(m)} onChange={() => toggleMethod(m)} /> {m}
            </label>
          ))}
        </div>
        <p className="subtle">{t("offer_dynamic_qr_note")}</p>
        <p className="subtle">{t("offer_login_note")}</p>

        <button
          className="btn btn-primary"
          disabled={formInvalid}
          onClick={() => {
            setModalError(null);
            setPendingCreate(true);
          }}
        >
          {t("create_offer")}
        </button>
        {formInvalid && (title !== "" || titleBn !== "") ? (
          <p className="subtle">{issues.map((i) => `${i.field}: ${i.code}`).join(" · ")}</p>
        ) : null}
      </section>

      <section className="panel">
        <div className="panel-head">
          <h2>{t("nav_offers")}</h2>
          <label className="field">
            <span>{t("offer_state")}</span>
            <select value={stateFilter} onChange={(e) => setStateFilter(e.target.value)}>
              <option value="">—</option>
              {["DRAFT", "ACTIVE", "PAUSED", "EXHAUSTED", "EXPIRED", "ARCHIVED"].map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </select>
          </label>
        </div>
        <table className="data-table">
          <thead>
            <tr>
              <th>Offer</th>
              <th>{t("offer_title_en")}</th>
              <th>{t("offer_percent")}</th>
              <th>{t("offer_windows")}</th>
              <th>{t("offer_caps")}</th>
              <th>{t("offer_min_spend")}</th>
              <th>{t("offer_state")}</th>
              <th>{t("offer_version")}</th>
              <th>{t("offer_valid_until")}</th>
            </tr>
          </thead>
          <tbody>
            {offerRows.length === 0 ? (
              <tr>
                <td colSpan={9} className="empty-row">
                  {t("offer_none")}
                </td>
              </tr>
            ) : (
              offerRows.map((o) => (
                <tr key={o.offer_id}>
                  <td className="mono">
                    <Link href={`/offers/${encodeURIComponent(o.offer_id)}`}>{shortId(o.offer_id, 16)}</Link>
                  </td>
                  <td>
                    {lang === "bn" ? <span lang="bn">{o.title_bn}</span> : o.title}
                  </td>
                  <td>{o.percent_bps !== null ? `${o.percent_bps / 100}%` : o.kind}</td>
                  <td className="mono">{windowsCell(o)}</td>
                  <td>{capsCell(o)}</td>
                  <td>{o.min_spend_minor > 0 ? formatBdt(o.min_spend_minor) : "—"}</td>
                  <td>
                    <StateBadge value={o.state} />
                  </td>
                  <td>v{o.version}</td>
                  <td>{formatTs(o.valid_until)}</td>
                </tr>
              ))
            )}
          </tbody>
        </table>
        <p className="subtle">{t("offer_receipt_note")}</p>
      </section>

      {pendingCreate ? (
        <TotpModal
          titleKey="create_offer"
          messageKey="create_offer_confirm"
          busy={busy}
          error={modalError}
          onConfirm={(code) => void confirmCreate(code)}
          onClose={() => setPendingCreate(false)}
        />
      ) : null}
    </Shell>
  );
}

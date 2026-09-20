"use client";

// Payment links (spec/16 §C): merchant list + create. Create posts via
// mutate() with an Idempotency-Key (and TOTP step-up, like every merchant
// mutation in this portal). The description is PII-screened server-side —
// rejected, never redacted.

import React, { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { ApiError, createPaymentLink, listPaymentLinks } from "@/lib/api/client";
import type { PaymentLink } from "@/lib/api/launchTypes";
import { CopyButton } from "@/components/CopyButton";
import { EnvBadge, StateBadge } from "@/components/Badges";
import { Shell } from "@/components/Shell";
import { TotpModal } from "@/components/TotpModal";
import { formatBdt, formatTs, parseAmountToMinor, shortId } from "@/lib/format";
import { useLang } from "@/lib/i18n/LangContext";
import { useMode } from "@/lib/mode";

type AmountMode = "fixed" | "bounded";

function amountCell(l: PaymentLink): string {
  if (l.amount_minor !== null) return formatBdt(l.amount_minor);
  if (l.amount_min_minor !== null && l.amount_max_minor !== null) {
    return `${formatBdt(l.amount_min_minor)} – ${formatBdt(l.amount_max_minor)}`;
  }
  return "—";
}

export default function PaymentLinksPage() {
  const { t } = useLang();
  const { env } = useMode();
  const [links, setLinks] = useState<PaymentLink[]>([]);
  const [error, setError] = useState<string | null>(null);

  const [amountMode, setAmountMode] = useState<AmountMode>("fixed");
  const [amountRaw, setAmountRaw] = useState("");
  const [minRaw, setMinRaw] = useState("");
  const [maxRaw, setMaxRaw] = useState("");
  const [description, setDescription] = useState("");
  const [singleUse, setSingleUse] = useState(true);
  const [expiryHours, setExpiryHours] = useState("72");

  const [pendingCreate, setPendingCreate] = useState(false);
  const [busy, setBusy] = useState(false);
  const [modalError, setModalError] = useState<string | null>(null);
  const [created, setCreated] = useState<PaymentLink | null>(null);

  const load = useCallback(() => {
    listPaymentLinks(env)
      .then((page) => {
        setLinks(page.data);
        setError(null);
      })
      .catch((err) => setError(err instanceof ApiError ? err.message : t("error_generic")));
  }, [env, t]);

  useEffect(() => {
    load();
  }, [load]);

  const amountMinor = amountRaw.trim() === "" ? null : parseAmountToMinor(amountRaw);
  const minMinor = minRaw.trim() === "" ? null : parseAmountToMinor(minRaw);
  const maxMinor = maxRaw.trim() === "" ? null : parseAmountToMinor(maxRaw);
  const expiry = /^\d+$/.test(expiryHours.trim()) ? Number(expiryHours.trim()) : null;

  const formInvalid =
    description.trim() === "" ||
    expiry === null ||
    (amountMode === "fixed"
      ? amountMinor === null
      : minMinor === null || maxMinor === null || BigInt(minMinor) >= BigInt(maxMinor));

  async function confirmCreate(totpCode: string) {
    setBusy(true);
    setModalError(null);
    try {
      const link = await createPaymentLink(
        env,
        {
          amount_minor: amountMode === "fixed" ? amountMinor : null,
          amount_min_minor: amountMode === "bounded" ? minMinor : null,
          amount_max_minor: amountMode === "bounded" ? maxMinor : null,
          description,
          single_use: singleUse,
          expiry_hours: expiry,
        },
        totpCode,
      );
      setCreated(link);
      setDescription("");
      setAmountRaw("");
      setMinRaw("");
      setMaxRaw("");
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
      <h1>{t("nav_payment_links")}</h1>
      {error ? <p className="error-text">{error}</p> : null}

      {created ? (
        <div className="notice">
          <strong className="mono">{created.payment_link_id}</strong> — {t("link_public_url")}:{" "}
          <Link href={created.url} className="mono">
            {created.url}
          </Link>{" "}
          <CopyButton value={created.url} />
        </div>
      ) : null}

      <section className="panel">
        <h2>{t("create_link")}</h2>
        <div className="actions-row">
          <label className="subtle">
            <input type="radio" name="amount-mode" checked={amountMode === "fixed"} onChange={() => setAmountMode("fixed")} />{" "}
            {t("link_amount_fixed")}
          </label>
          <label className="subtle">
            <input
              type="radio"
              name="amount-mode"
              checked={amountMode === "bounded"}
              onChange={() => setAmountMode("bounded")}
            />{" "}
            {t("link_amount_bounded")}
          </label>
        </div>
        <div className="toolbar">
          {amountMode === "fixed" ? (
            <label className="field">
              <span>{t("checkout_amount")} (BDT)</span>
              <input value={amountRaw} onChange={(e) => setAmountRaw(e.target.value)} inputMode="decimal" />
            </label>
          ) : (
            <>
              <label className="field">
                <span>Min (BDT)</span>
                <input value={minRaw} onChange={(e) => setMinRaw(e.target.value)} inputMode="decimal" />
              </label>
              <label className="field">
                <span>Max (BDT)</span>
                <input value={maxRaw} onChange={(e) => setMaxRaw(e.target.value)} inputMode="decimal" />
              </label>
            </>
          )}
          <label className="field">
            <span>{t("link_description")}</span>
            <input value={description} onChange={(e) => setDescription(e.target.value)} />
          </label>
          <label className="field">
            <span>{t("link_expiry_hours")}</span>
            <input value={expiryHours} onChange={(e) => setExpiryHours(e.target.value)} inputMode="numeric" />
          </label>
          <EnvBadge env={env} />
        </div>
        <div className="actions-row">
          <label className="subtle">
            <input type="checkbox" checked={singleUse} onChange={(e) => setSingleUse(e.target.checked)} /> {t("link_single_use")}
          </label>
        </div>
        <p className="subtle">{t("bn_numeral_note")}</p>
        <button
          className="btn btn-primary"
          disabled={formInvalid}
          onClick={() => {
            setModalError(null);
            setPendingCreate(true);
          }}
        >
          {t("create_link")}
        </button>
      </section>

      <section className="panel">
        <table className="data-table">
          <thead>
            <tr>
              <th>Link</th>
              <th>{t("link_description")}</th>
              <th>{t("checkout_amount")}</th>
              <th>Status</th>
              <th>Expires</th>
              <th>{t("link_public_url")}</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {links.length === 0 ? (
              <tr>
                <td colSpan={7} className="empty-row">
                  No payment links in this environment
                </td>
              </tr>
            ) : (
              links.map((l) => (
                <tr key={l.payment_link_id}>
                  <td className="mono">
                    <Link href={`/payment-links/${encodeURIComponent(l.payment_link_id)}`}>{shortId(l.payment_link_id, 16)}</Link>
                  </td>
                  <td>{l.description}</td>
                  <td>{amountCell(l)}</td>
                  <td>
                    <StateBadge value={l.state} />
                  </td>
                  <td>{formatTs(l.expires_at)}</td>
                  <td className="mono">
                    <Link href={l.url}>{l.url}</Link>
                  </td>
                  <td>
                    <CopyButton value={l.url} />
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </section>

      {pendingCreate ? (
        <TotpModal
          titleKey="create_link"
          messageKey="create_link_confirm"
          busy={busy}
          error={modalError}
          onConfirm={(code) => void confirmCreate(code)}
          onClose={() => setPendingCreate(false)}
        />
      ) : null}
    </Shell>
  );
}

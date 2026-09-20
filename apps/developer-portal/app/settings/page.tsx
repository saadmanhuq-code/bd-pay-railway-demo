"use client";

// Settings: virtual-account display (per-store VA numbers, gated
// "pending sponsor-bank range" state), TOTP enrollment view, team members.

import React, { useEffect, useState } from "react";
import { ApiError, getMerchantSettings } from "@/lib/api/client";
import type { MerchantSettings } from "@/lib/api/types";
import { StateBadge } from "@/components/Badges";
import { Shell } from "@/components/Shell";
import { formatTs } from "@/lib/format";
import { useLang } from "@/lib/i18n/LangContext";

export default function SettingsPage() {
  const { t } = useLang();
  const [settings, setSettings] = useState<MerchantSettings | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    getMerchantSettings()
      .then((s) => {
        setSettings(s);
        setError(null);
      })
      .catch((err) => setError(err instanceof ApiError ? err.message : t("error_generic")));
  }, [t]);

  return (
    <Shell>
      <h1>{t("nav_settings")}</h1>
      {error ? <p className="error-text">{error}</p> : null}
      {!settings ? (
        <p>{t("loading")}</p>
      ) : (
        <>
          <section className="panel">
            <h2>Merchant profile</h2>
            <dl className="kv">
              <dt>Merchant</dt>
              <dd className="mono">{settings.merchant.merchant_id}</dd>
              <dt>Legal name</dt>
              <dd>{settings.merchant.legal_name}</dd>
              <dt>Trade name</dt>
              <dd>
                {settings.merchant.trade_name} · <span lang="bn">{settings.merchant.trade_name_bn}</span>
              </dd>
              <dt>Status</dt>
              <dd>
                <StateBadge value={settings.merchant.status} />
              </dd>
              <dt>MCC / City</dt>
              <dd>
                {settings.merchant.mcc} / {settings.merchant.city}
              </dd>
            </dl>
          </section>

          <section className="panel">
            <h2>Virtual accounts</h2>
            <table className="data-table">
              <thead>
                <tr>
                  <th>Store</th>
                  <th>VA number</th>
                  <th>State</th>
                  <th>Sponsor bank</th>
                  <th>Created</th>
                </tr>
              </thead>
              <tbody>
                {settings.virtual_accounts.map((va) => (
                  <tr key={va.va_id}>
                    <td>{va.store_label}</td>
                    <td className="mono">{va.va_number ?? "awaiting sponsor-bank range allocation"}</td>
                    <td>
                      <StateBadge value={va.state} />
                    </td>
                    <td>{va.bank_name}</td>
                    <td>{formatTs(va.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <p className="subtle">
              New per-store VA numbers are allocated from the sponsor bank&apos;s reserved range; a VA stays in
              PENDING_SPONSOR_RANGE until the next range tranche is confirmed by the sponsor bank.
            </p>
          </section>

          <section className="panel">
            <h2>TOTP enrollment</h2>
            <dl className="kv">
              <dt>Enrolled</dt>
              <dd>
                <StateBadge value={settings.totp.enrolled ? "ACTIVE" : "REQUIRED"} />
              </dd>
              <dt>Secret</dt>
              <dd className="mono">{settings.totp.secret_masked}</dd>
              <dt>otpauth URI</dt>
              <dd className="mono">{settings.totp.otpauth_uri}</dd>
              <dt>Enrolled at</dt>
              <dd>{formatTs(settings.totp.enrolled_at)}</dd>
            </dl>
            <p className="subtle">{t("totp_prompt")}</p>
          </section>

          <section className="panel">
            <h2>Team members</h2>
            <table className="data-table">
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Email</th>
                  <th>{t("persona")}</th>
                </tr>
              </thead>
              <tbody>
                {settings.members.map((m) => (
                  <tr key={m.member_id}>
                    <td>{m.display_name}</td>
                    <td className="mono">{m.email}</td>
                    <td>
                      <span className="badge">{m.persona}</span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </section>
        </>
      )}
    </Shell>
  );
}

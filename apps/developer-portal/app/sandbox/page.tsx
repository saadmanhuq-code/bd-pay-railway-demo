"use client";

// Sandbox explainer + simulator scenario trigger table (spec/10 DSL).
// Test-card-free: scenarios are driven entirely by tokenized payment
// references and amount match rules.

import React, { useState } from "react";
import Link from "next/link";
import { Shell } from "@/components/Shell";
import { formatBdt, normalizeBengaliDigits, parseAmountToMinor } from "@/lib/format";
import { useLang } from "@/lib/i18n/LangContext";

interface Scenario {
  name: string;
  rule: string;
  outcome: string;
  matches: (minor: bigint) => boolean;
}

const SCENARIOS: Scenario[] = [
  {
    name: "decline",
    rule: "amount_minor_mod { divisor: 100, remainder: 13 } — any amount ending in .13 BDT",
    outcome: "Synchronous rail decline → payment_intent.failed (failure_code: rail_declined)",
    matches: (m) => m % 100n === 13n,
  },
  {
    name: "pending_then_success",
    rule: 'amount_minor_in ["10100"] — exactly BDT 101.00',
    outcome: "PROCESSING for one poll cycle, then SUCCEEDED with a delayed webhook",
    matches: (m) => m === 10100n,
  },
  {
    name: "timeout",
    rule: 'amount_minor_in ["10200"] — exactly BDT 102.00',
    outcome: "Connector timeout → intent stays PROCESSING; query_status path exercised",
    matches: (m) => m === 10200n,
  },
  {
    name: "duplicate_callback",
    rule: 'amount_minor_in ["10300"] — exactly BDT 103.00',
    outcome: "Success webhook delivered twice — your handler must be idempotent on event id",
    matches: (m) => m === 10300n,
  },
  {
    name: "reversal",
    rule: 'amount_minor_in ["10400"] — exactly BDT 104.00',
    outcome: "Success, then an asynchronous rail reversal on the next recon cycle",
    matches: (m) => m === 10400n,
  },
  {
    name: "success",
    rule: "default — any other amount",
    outcome: "Immediate SUCCEEDED with payment_intent.succeeded webhook",
    matches: () => true,
  },
];

export default function SandboxPage() {
  const { t } = useLang();
  const [amountRaw, setAmountRaw] = useState("");

  const minorStr = parseAmountToMinor(amountRaw);
  const minor = minorStr === null ? null : BigInt(minorStr);
  const matched = minor === null ? null : SCENARIOS.find((s) => s.matches(minor)) ?? null;

  return (
    <Shell>
      <h1>{t("nav_sandbox")}</h1>
      <section className="panel">
        <h2>How the sandbox works</h2>
        <p>
          Sandbox keys (<code>bdpk_test_…</code>) hit the deterministic simulator instead of real rails. There are no test
          card numbers anywhere — payment methods are <strong>tokenized references</strong> (<code>pmt_tok_…</code>) and the
          scenario is selected purely by the intent&apos;s <code>amount_minor</code> against the match rules below (spec/10
          scenario DSL). The same request always produces the same outcome.
        </p>
        <p className="subtle">
          <Link href="/sandbox-signup">{t("sbx_from_sandbox_link")}</Link>
        </p>
      </section>

      <section className="panel">
        <h2>Scenario trigger table</h2>
        <table className="data-table">
          <thead>
            <tr>
              <th>Scenario</th>
              <th>Match rule</th>
              <th>Outcome</th>
            </tr>
          </thead>
          <tbody>
            {SCENARIOS.map((s) => (
              <tr key={s.name}>
                <td className="mono">{s.name}</td>
                <td className="subtle">{s.rule}</td>
                <td>{s.outcome}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <div className="scenario-check">
          <label className="field">
            <span>Try an amount (BDT — Bengali numerals accepted)</span>
            <input value={amountRaw} onChange={(e) => setAmountRaw(e.target.value)} inputMode="decimal" />
          </label>
          {amountRaw.trim() !== "" ? (
            minor === null ? (
              <span className="error-text">Not a valid amount</span>
            ) : (
              <span>
                {formatBdt(minor.toString())} (<code>amount_minor: &quot;{minor.toString()}&quot;</code>) →{" "}
                <strong className="mono">{matched?.name}</strong>
              </span>
            )
          ) : null}
        </div>
        {amountRaw !== normalizeBengaliDigits(amountRaw) ? <p className="subtle">{t("bn_numeral_note")}</p> : null}
      </section>

      <section className="panel">
        <h2>Recommended integration checks</h2>
        <ul className="checklist">
          <li>Drive one intent through each scenario above and assert your order state machine.</li>
          <li>Verify webhook signatures (X-BDPay-Signature) before trusting any event.</li>
          <li>Send the duplicate_callback amount and confirm your handler is idempotent on event id.</li>
          <li>Send the timeout amount and confirm you poll GET /v1/payment-intents/:id instead of assuming failure.</li>
          <li>Replay a delivery from the Webhooks page and confirm no double fulfilment.</li>
        </ul>
      </section>
    </Shell>
  );
}

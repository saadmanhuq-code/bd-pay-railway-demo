"use client";

// Rendered API reference from the typed in-app const (spec/01 + spec/02
// surface) with JSON samples, curl samples, the conventions §4 error
// envelope, and a Postman collection download.

import React from "react";
import {
  API_OPERATIONS,
  ERROR_ENVELOPE_SAMPLE,
  WEBHOOK_EVENT_DOCS,
  WEBHOOK_SIGNATURE_NOTE,
  buildPostmanCollection,
} from "@/lib/docs/apiReference";
import { Shell } from "@/components/Shell";
import { useLang } from "@/lib/i18n/LangContext";

function downloadPostman() {
  const blob = new Blob([buildPostmanCollection()], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = "bdpay-api.postman_collection.json";
  a.click();
  URL.revokeObjectURL(url);
}

export default function DocsPage() {
  const { t } = useLang();
  return (
    <Shell>
      <h1>{t("nav_docs")}</h1>
      <section className="panel">
        <div className="panel-head">
          <h2>BD-PAY merchant API</h2>
          <button className="btn btn-primary" onClick={downloadPostman}>
            {t("download")} Postman collection
          </button>
        </div>
        <p>
          All amounts are <strong>integer paisa serialized as strings</strong> (<code>amount_minor</code>): BDT 1,500.00 is{" "}
          <code>&quot;150000&quot;</code>. Every POST requires an <code>Idempotency-Key</code> header; retries with the same
          key return the original result. Errors use the conventions §4 envelope:
        </p>
        <pre className="code-block">{ERROR_ENVELOPE_SAMPLE}</pre>
      </section>

      {API_OPERATIONS.map((op) => (
        <div className="docs-op" key={op.operationId}>
          <div className="docs-op-head">
            <span className={`docs-method ${op.method === "GET" ? "docs-method-get" : "docs-method-post"}`}>{op.method}</span>
            <code className="mono">{op.path}</code>
            <span className="subtle">{op.summary}</span>
          </div>
          <div className="docs-op-body">
            <p>{op.description}</p>
            {op.requestSample ? (
              <>
                <h3>{op.requestSample.label}</h3>
                <pre className="code-block">{op.requestSample.json}</pre>
              </>
            ) : null}
            <h3>{op.responseSample.label}</h3>
            <pre className="code-block">{op.responseSample.json}</pre>
            <h3>curl</h3>
            <pre className="code-block">{op.curl}</pre>
          </div>
        </div>
      ))}

      <section className="panel">
        <h2>Webhook events</h2>
        <p className="notice">{WEBHOOK_SIGNATURE_NOTE}</p>
        {WEBHOOK_EVENT_DOCS.map((ev) => (
          <div key={ev.eventType}>
            <h3 className="mono">{ev.eventType}</h3>
            <p className="subtle">{ev.when}</p>
            <pre className="code-block">{ev.payloadSample}</pre>
          </div>
        ))}
      </section>
    </Shell>
  );
}

"use client";

// Webhook endpoints (spec/01 §I): create with display-once signing secret,
// delete (DualLabel confirm), signed test-fire showing the HMAC signature
// header, delivery log with replay.

import React, { useCallback, useEffect, useState } from "react";
import {
  ApiError,
  createWebhookEndpoint,
  deleteWebhookEndpoint,
  listWebhookDeliveries,
  listWebhookEndpoints,
  replayWebhookDelivery,
  testWebhookEndpoint,
} from "@/lib/api/client";
import type { WebhookDelivery, WebhookEndpoint } from "@/lib/api/types";
import { WEBHOOK_EVENT_TYPES } from "@/lib/api/types";
import { EnvBadge, StateBadge } from "@/components/Badges";
import { SecretOnce } from "@/components/SecretOnce";
import { Shell } from "@/components/Shell";
import { TotpModal } from "@/components/TotpModal";
import { formatTs, shortId } from "@/lib/format";
import { useLang } from "@/lib/i18n/LangContext";
import { useMode } from "@/lib/mode";

type PendingAction =
  | { kind: "create"; url: string; events: string[]; description: string }
  | { kind: "delete"; endpoint: WebhookEndpoint }
  | { kind: "test"; endpoint: WebhookEndpoint }
  | { kind: "replay"; delivery: WebhookDelivery };

export default function WebhooksPage() {
  const { t } = useLang();
  const { env } = useMode();
  const [endpoints, setEndpoints] = useState<WebhookEndpoint[]>([]);
  const [deliveries, setDeliveries] = useState<WebhookDelivery[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [url, setUrl] = useState("https://");
  const [events, setEvents] = useState<string[]>(["payment_intent.succeeded"]);
  const [description, setDescription] = useState("");
  const [pending, setPending] = useState<PendingAction | null>(null);
  const [busy, setBusy] = useState(false);
  const [modalError, setModalError] = useState<string | null>(null);
  const [secretView, setSecretView] = useState<{ label: string; secret: string } | null>(null);
  const [lastTestFire, setLastTestFire] = useState<WebhookDelivery | null>(null);

  const load = useCallback(() => {
    Promise.all([listWebhookEndpoints(env), listWebhookDeliveries(env)])
      .then(([eps, dels]) => {
        setEndpoints(eps.data);
        setDeliveries(dels.data);
        setError(null);
      })
      .catch((err) => setError(err instanceof ApiError ? err.message : t("error_generic")));
  }, [env, t]);

  useEffect(() => {
    load();
  }, [load]);

  function toggleEvent(ev: string) {
    setEvents((prev) => (prev.includes(ev) ? prev.filter((x) => x !== ev) : [...prev, ev]));
  }

  async function confirm(totpCode: string) {
    if (!pending) return;
    setBusy(true);
    setModalError(null);
    try {
      if (pending.kind === "create") {
        const created = await createWebhookEndpoint(env, pending.url, pending.events, pending.description, totpCode);
        setSecretView({ label: `Signing secret for ${created.url}`, secret: created.signing_secret });
        setUrl("https://");
        setDescription("");
      } else if (pending.kind === "delete") {
        await deleteWebhookEndpoint(pending.endpoint.webhook_id, totpCode);
      } else if (pending.kind === "test") {
        const delivery = await testWebhookEndpoint(pending.endpoint.webhook_id, totpCode);
        setLastTestFire(delivery);
      } else {
        await replayWebhookDelivery(pending.delivery.delivery_id, totpCode);
      }
      setPending(null);
      load();
    } catch (err) {
      setModalError(err instanceof ApiError ? err.message : t("error_generic"));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Shell>
      <h1>{t("nav_webhooks")}</h1>
      {error ? <p className="error-text">{error}</p> : null}
      {secretView ? <SecretOnce label={secretView.label} secret={secretView.secret} onDismiss={() => setSecretView(null)} /> : null}
      {lastTestFire ? (
        <div className="notice">
          <strong>Test event delivered.</strong> Signature header:
          <div className="mono">X-BDPay-Signature: {lastTestFire.signature_header}</div>
          <div className="subtle">Verify: v1 = HMAC-SHA256(signing_secret, `t.raw_body`), constant-time compare.</div>
        </div>
      ) : null}

      <section className="panel">
        <h2>{t("create_endpoint")}</h2>
        <div className="toolbar">
          <label className="field">
            <span>HTTPS URL</span>
            <input value={url} onChange={(e) => setUrl(e.target.value)} style={{ minWidth: 320 }} />
          </label>
          <label className="field">
            <span>Description</span>
            <input value={description} onChange={(e) => setDescription(e.target.value)} />
          </label>
          <EnvBadge env={env} />
        </div>
        <div className="actions-row">
          {WEBHOOK_EVENT_TYPES.map((ev) => (
            <label key={ev} className="subtle">
              <input type="checkbox" checked={events.includes(ev)} onChange={() => toggleEvent(ev)} /> {ev}
            </label>
          ))}
        </div>
        {url !== "https://" && url.trim() !== "" && !url.startsWith("https://") ? (
          <p className="error-text">{t("webhook_url_https_required")}</p>
        ) : null}
        {events.length === 0 ? <p className="error-text">{t("webhook_events_required")}</p> : null}
        <button
          className="btn btn-primary"
          disabled={!url.startsWith("https://") || url.length < 9 || events.length === 0}
          onClick={() => {
            setModalError(null);
            setPending({ kind: "create", url, events, description });
          }}
        >
          {t("create_endpoint")}
        </button>
      </section>

      <section className="panel">
        <h2>Endpoints</h2>
        <table className="data-table">
          <thead>
            <tr>
              <th>URL</th>
              <th>Events</th>
              <th>Status</th>
              <th>Created</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {endpoints.length === 0 ? (
              <tr>
                <td colSpan={5} className="empty-row">
                  No endpoints in this environment
                </td>
              </tr>
            ) : (
              endpoints.map((ep) => (
                <tr key={ep.webhook_id}>
                  <td>
                    <div className="mono">{ep.url}</div>
                    <div className="subtle">{ep.description}</div>
                  </td>
                  <td className="subtle">{ep.enabled_events.join(", ")}</td>
                  <td>
                    <StateBadge value={ep.status} />
                  </td>
                  <td>{formatTs(ep.created_at)}</td>
                  <td>
                    <span className="actions-row">
                      <button
                        className="btn btn-small"
                        onClick={() => {
                          setModalError(null);
                          setPending({ kind: "test", endpoint: ep });
                        }}
                      >
                        {t("test_fire")}
                      </button>
                      <button
                        className="btn btn-small btn-danger"
                        onClick={() => {
                          setModalError(null);
                          setPending({ kind: "delete", endpoint: ep });
                        }}
                      >
                        {t("delete_endpoint")}
                      </button>
                    </span>
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </section>

      <section className="panel">
        <h2>Delivery log</h2>
        <table className="data-table">
          <thead>
            <tr>
              <th>Delivery</th>
              <th>Event</th>
              <th>Status</th>
              <th>Attempt</th>
              <th>Response</th>
              <th>Signature header</th>
              <th>At</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {deliveries.length === 0 ? (
              <tr>
                <td colSpan={8} className="empty-row">
                  No deliveries yet
                </td>
              </tr>
            ) : (
              deliveries.map((d) => (
                <tr key={d.delivery_id}>
                  <td className="mono">{shortId(d.delivery_id, 16)}</td>
                  <td>{d.event_type}</td>
                  <td>
                    <StateBadge value={d.status} />
                  </td>
                  <td>{d.attempt}</td>
                  <td>{d.response_code ?? "—"}</td>
                  <td className="mono">{shortId(d.signature_header, 28)}</td>
                  <td>{formatTs(d.delivered_at)}</td>
                  <td>
                    <button
                      className="btn btn-small"
                      onClick={() => {
                        setModalError(null);
                        setPending({ kind: "replay", delivery: d });
                      }}
                    >
                      {t("replay_delivery")}
                    </button>
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </section>

      {pending ? (
        <TotpModal
          titleKey={
            pending.kind === "create"
              ? "create_endpoint"
              : pending.kind === "delete"
                ? "delete_endpoint"
                : pending.kind === "test"
                  ? "test_fire"
                  : "replay_delivery"
          }
          messageKey={
            pending.kind === "create"
              ? "secret_once"
              : pending.kind === "delete"
                ? "delete_endpoint_confirm"
                : pending.kind === "test"
                  ? "test_fire_confirm"
                  : "replay_confirm"
          }
          danger={pending.kind === "delete"}
          busy={busy}
          error={modalError}
          onConfirm={(code) => void confirm(code)}
          onClose={() => setPending(null)}
        />
      ) : null}
    </Shell>
  );
}

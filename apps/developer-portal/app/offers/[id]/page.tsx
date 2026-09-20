"use client";

// Offer detail (spec/18 merchant surface): full economic parameters, the live
// counter readout (redemption analytics — counts only, never diner
// identities), cap monitoring with the 80% warning line, FSM lifecycle
// actions (activate / pause / resume / archive) and edit-by-versioning.
// Every number rendered here is a server value — the portal renders, it
// never recomputes (presentation-only utilization bars from server ints).

import React, { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import {
  ApiError,
  activateOffer,
  archiveOffer,
  editOffer,
  getOffer,
  pauseOffer,
  resumeOffer,
} from "@/lib/api/client";
import {
  CAP_WARNING_BPS,
  utilizationBps,
  type EditOfferInput,
  type OfferDetail,
} from "@/lib/api/offerTypes";
import { StateBadge } from "@/components/Badges";
import { Shell } from "@/components/Shell";
import { TotpModal } from "@/components/TotpModal";
import { formatBdt, formatTs, parseAmountToMinor } from "@/lib/format";
import { useLang } from "@/lib/i18n/LangContext";
import type { CopyKey } from "@/lib/i18n/copy";

type LifecycleAction = "activate" | "pause" | "resume" | "archive";

const ACTION_COPY: Record<LifecycleAction, { titleKey: CopyKey; messageKey: CopyKey; danger: boolean }> = {
  activate: { titleKey: "activate_offer", messageKey: "activate_offer_confirm", danger: false },
  pause: { titleKey: "pause_offer", messageKey: "pause_offer_confirm", danger: false },
  resume: { titleKey: "resume_offer", messageKey: "resume_offer_confirm", danger: false },
  archive: { titleKey: "archive_offer", messageKey: "archive_offer_confirm", danger: true },
};

// FSM-aware button visibility (refusal-first mirror of spec/18 FSM 1 —
// the server is the arbiter; this only avoids offering DENIED triggers).
function availableActions(state: OfferDetail["state"]): LifecycleAction[] {
  switch (state) {
    case "DRAFT":
      return ["activate", "archive"];
    case "ACTIVE":
      return ["pause", "archive"];
    case "PAUSED":
      return ["resume", "archive"];
    case "EXHAUSTED":
      return ["archive"]; // reactivation is a cap_total raise via edit
    default:
      return []; // EXPIRED / ARCHIVED are terminal
  }
}

function Meter({ used, cap }: { used: number; cap: number | null }) {
  const bps = utilizationBps(used, cap);
  if (bps === null || cap === null) return <span className="subtle">—</span>;
  const pct = Math.min(100, Math.floor(bps / 100));
  const cls = bps >= 10_000 ? "meter-fill full" : bps >= CAP_WARNING_BPS ? "meter-fill warn" : "meter-fill";
  return (
    <span title={`${used} / ${cap}`}>
      <span className="meter">
        <span className={cls} style={{ width: `${pct}%`, display: "block" }} />
      </span>
      <span className="subtle">
        {" "}
        {used} / {cap}
      </span>
    </span>
  );
}

export default function OfferDetailPage() {
  const params = useParams<{ id: string }>();
  const offerId = typeof params.id === "string" ? params.id : "";
  const { t } = useLang();

  const [offer, setOffer] = useState<OfferDetail | null>(null);
  const [error, setError] = useState<string | null>(null);

  const [pendingAction, setPendingAction] = useState<LifecycleAction | null>(null);
  const [pendingEdit, setPendingEdit] = useState(false);
  const [busy, setBusy] = useState(false);
  const [modalError, setModalError] = useState<string | null>(null);

  // Edit form (prefilled on load; only changed fields are sent).
  const [editTitle, setEditTitle] = useState("");
  const [editTitleBn, setEditTitleBn] = useState("");
  const [editPercentRaw, setEditPercentRaw] = useState("");
  const [editCapPerDayRaw, setEditCapPerDayRaw] = useState("");
  const [editCapTotalRaw, setEditCapTotalRaw] = useState("");

  const load = useCallback(() => {
    if (offerId === "") {
      setError(t("error_generic"));
      return;
    }
    getOffer(offerId)
      .then((o) => {
        setOffer(o);
        setError(null);
        setEditTitle(o.title);
        setEditTitleBn(o.title_bn);
        setEditPercentRaw(o.percent_bps !== null ? String(o.percent_bps / 100) : "");
        setEditCapPerDayRaw(o.cap_per_day !== null ? String(o.cap_per_day) : "");
        setEditCapTotalRaw(o.cap_total !== null ? String(o.cap_total) : "");
      })
      .catch((err) => setError(err instanceof ApiError ? err.message : t("error_generic")));
  }, [offerId, t]);

  useEffect(() => {
    load();
  }, [load]);

  function buildEditChanges(): EditOfferInput {
    if (!offer) return {};
    const changes: EditOfferInput = {};
    if (editTitle !== offer.title) changes.title = editTitle;
    if (editTitleBn !== offer.title_bn) changes.title_bn = editTitleBn;
    const bpsMinor = parseAmountToMinor(editPercentRaw);
    if (bpsMinor !== null) {
      const bps = Number(bpsMinor);
      if (Number.isSafeInteger(bps) && bps !== offer.percent_bps) changes.percent_bps = bps;
    }
    if (/^\d+$/.test(editCapPerDayRaw.trim()) && Number(editCapPerDayRaw) !== offer.cap_per_day) {
      changes.cap_per_day = Number(editCapPerDayRaw);
    }
    if (/^\d+$/.test(editCapTotalRaw.trim()) && Number(editCapTotalRaw) !== offer.cap_total) {
      changes.cap_total = Number(editCapTotalRaw);
    }
    return changes;
  }

  const editChanges = buildEditChanges();
  const editDirty = Object.keys(editChanges).length > 0;

  async function confirmAction(totpCode: string) {
    if (!pendingAction) return;
    setBusy(true);
    setModalError(null);
    const fn =
      pendingAction === "activate"
        ? activateOffer
        : pendingAction === "pause"
          ? pauseOffer
          : pendingAction === "resume"
            ? resumeOffer
            : archiveOffer;
    try {
      await fn(offerId, totpCode);
      setPendingAction(null);
      load();
    } catch (err) {
      setModalError(err instanceof ApiError ? err.message : t("error_generic"));
    } finally {
      setBusy(false);
    }
  }

  async function confirmEdit(totpCode: string) {
    setBusy(true);
    setModalError(null);
    try {
      await editOffer(offerId, editChanges, totpCode);
      setPendingEdit(false);
      load();
    } catch (err) {
      setModalError(err instanceof ApiError ? err.message : t("error_generic"));
    } finally {
      setBusy(false);
    }
  }

  if (error) {
    return (
      <Shell>
        <h1>{t("nav_offers")}</h1>
        <p className="error-text">{error}</p>
        <Link href="/offers">← {t("nav_offers")}</Link>
      </Shell>
    );
  }
  if (!offer) {
    return <Shell>{t("loading")}</Shell>;
  }

  const terminal = offer.state === "EXPIRED" || offer.state === "ARCHIVED";

  return (
    <Shell>
      <h1>
        {offer.title} <span lang="bn">({offer.title_bn})</span>
      </h1>
      <p>
        <span className="mono">{offer.offer_id}</span> · <StateBadge value={offer.state} /> · {t("offer_version")} v{offer.version}
      </p>

      <section className="panel">
        <h2>{t("offer_analytics")}</h2>
        <div className="grid-3">
          <div className="kpi">
            <div className="kpi-label">{t("offer_redeemed_today")}</div>
            <div>{offer.counters.redeemed_today}</div>
          </div>
          <div className="kpi">
            <div className="kpi-label">{t("offer_redeemed_total")}</div>
            <div>{offer.counters.redeemed_total}</div>
          </div>
          <div className="kpi">
            <div className="kpi-label">{t("offer_reserved_now")}</div>
            <div>{offer.counters.reserved_now}</div>
          </div>
        </div>
        <p className="subtle">{t("offer_counts_only_note")}</p>
      </section>

      <section className="panel">
        <h2>{t("offer_cap_monitor")}</h2>
        <dl className="kv">
          <dt>{t("offer_cap_per_day")}</dt>
          <dd>
            <Meter used={offer.counters.redeemed_today} cap={offer.cap_per_day} />
          </dd>
          <dt>{t("offer_cap_total")}</dt>
          <dd>
            <Meter used={offer.counters.redeemed_total} cap={offer.cap_total} />
          </dd>
          <dt>{t("offer_cap_per_customer")}</dt>
          <dd>{offer.cap_per_customer ?? "—"}</dd>
          <dt>{t("offer_cap_recredit")}</dt>
          <dd>{offer.cap_recredit_on_refund ? "✓" : "—"}</dd>
        </dl>
        {(() => {
          const dayBps = utilizationBps(offer.counters.redeemed_today, offer.cap_per_day);
          const totalBps = utilizationBps(offer.counters.redeemed_total, offer.cap_total);
          const warn = (dayBps !== null && dayBps >= CAP_WARNING_BPS) || (totalBps !== null && totalBps >= CAP_WARNING_BPS);
          return warn ? <div className="notice-warn notice">{t("offer_cap_warning")}</div> : null;
        })()}
      </section>

      <section className="panel">
        <h2>{t("offer_kind")}</h2>
        <dl className="kv">
          <dt>{t("offer_kind")}</dt>
          <dd>{offer.kind}</dd>
          <dt>{t("offer_percent")}</dt>
          <dd>{offer.percent_bps !== null ? `${offer.percent_bps / 100}% (${offer.percent_bps} bps)` : "—"}</dd>
          <dt>{t("offer_windows")}</dt>
          <dd className="mono">
            {offer.windows.map((w, i) => (
              <div key={i}>
                {w.days.join(", ")} · {w.start_local}–{w.end_local}
              </div>
            ))}
          </dd>
          <dt>{t("offer_valid_from")}</dt>
          <dd>{formatTs(offer.valid_from)}</dd>
          <dt>{t("offer_valid_until")}</dt>
          <dd>{formatTs(offer.valid_until)}</dd>
          <dt>{t("offer_min_spend")}</dt>
          <dd>{offer.min_spend_minor > 0 ? formatBdt(offer.min_spend_minor) : "—"}</dd>
          <dt>{t("offer_max_discount")}</dt>
          <dd>{offer.max_discount_minor !== null ? formatBdt(offer.max_discount_minor) : "—"}</dd>
          <dt>{t("offer_methods")}</dt>
          <dd className="mono">{offer.allowed_methods.join(", ")}</dd>
        </dl>
        <p className="subtle">{t("offer_window_tz_note")}</p>
        <p className="subtle">{t("offer_dynamic_qr_note")}</p>
      </section>

      {!terminal ? (
        <section className="panel">
          <h2>{t("offer_state")}</h2>
          <div className="actions-row">
            {availableActions(offer.state).map((action) => (
              <button
                key={action}
                className={`btn ${action === "archive" ? "btn-danger" : "btn-primary"}`}
                onClick={() => {
                  setModalError(null);
                  setPendingAction(action);
                }}
              >
                {t(ACTION_COPY[action].titleKey)}
              </button>
            ))}
          </div>
        </section>
      ) : null}

      {!terminal ? (
        <section className="panel">
          <h2>{t("edit_offer")}</h2>
          <p className="subtle">{t("edit_offer_confirm")}</p>
          <div className="toolbar">
            <label className="field">
              <span>{t("offer_title_en")}</span>
              <input value={editTitle} onChange={(e) => setEditTitle(e.target.value)} />
            </label>
            <label className="field">
              <span>{t("offer_title_bn")}</span>
              <input value={editTitleBn} onChange={(e) => setEditTitleBn(e.target.value)} lang="bn" />
            </label>
            <label className="field">
              <span>{t("offer_percent")}</span>
              <input value={editPercentRaw} onChange={(e) => setEditPercentRaw(e.target.value)} inputMode="decimal" />
            </label>
            <label className="field">
              <span>{t("offer_cap_per_day")}</span>
              <input value={editCapPerDayRaw} onChange={(e) => setEditCapPerDayRaw(e.target.value)} inputMode="numeric" />
            </label>
            <label className="field">
              <span>{t("offer_cap_total")}</span>
              <input value={editCapTotalRaw} onChange={(e) => setEditCapTotalRaw(e.target.value)} inputMode="numeric" />
            </label>
          </div>
          <button
            className="btn btn-primary"
            disabled={!editDirty}
            onClick={() => {
              setModalError(null);
              setPendingEdit(true);
            }}
          >
            {t("edit_offer")}
          </button>
        </section>
      ) : null}

      <p>
        <Link href="/offers">← {t("nav_offers")}</Link>
      </p>

      {pendingAction ? (
        <TotpModal
          titleKey={ACTION_COPY[pendingAction].titleKey}
          messageKey={ACTION_COPY[pendingAction].messageKey}
          danger={ACTION_COPY[pendingAction].danger}
          busy={busy}
          error={modalError}
          onConfirm={(code) => void confirmAction(code)}
          onClose={() => setPendingAction(null)}
        />
      ) : null}
      {pendingEdit ? (
        <TotpModal
          titleKey="edit_offer"
          messageKey="edit_offer_confirm"
          busy={busy}
          error={modalError}
          onConfirm={(code) => void confirmEdit(code)}
          onClose={() => setPendingEdit(false)}
        />
      ) : null}
    </Shell>
  );
}

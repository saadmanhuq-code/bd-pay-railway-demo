"use client";

// Notification inbox — title/body rendered per active locale
// (title_en/title_bn, body_en/body_bn).

import React, { useEffect, useState } from "react";
import { ApiError, listNotifications } from "@/lib/api/client";
import type { PortalNotification } from "@/lib/api/types";
import { Shell } from "@/components/Shell";
import { formatTs } from "@/lib/format";
import { useLang } from "@/lib/i18n/LangContext";

export default function NotificationsPage() {
  const { t, lang } = useLang();
  const [rows, setRows] = useState<PortalNotification[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    listNotifications()
      .then((page) => {
        setRows(page.data);
        setError(null);
      })
      .catch((err) => setError(err instanceof ApiError ? err.message : t("error_generic")));
  }, [t]);

  return (
    <Shell>
      <h1>{t("nav_notifications")}</h1>
      {error ? <p className="error-text">{error}</p> : null}
      <section className="panel">
        <ul className="checklist">
          {rows.length === 0 ? (
            <li className="subtle">Inbox is empty</li>
          ) : (
            rows.map((n) => (
              <li key={n.notification_id}>
                <div>
                  <strong lang={lang}>{lang === "bn" ? n.title_bn : n.title_en}</strong>{" "}
                  {n.read_at === null ? <span className="badge badge-warn">new</span> : null}
                  <span className="badge">{n.kind}</span>
                  <div lang={lang}>{lang === "bn" ? n.body_bn : n.body_en}</div>
                  <div className="subtle">{formatTs(n.created_at)}</div>
                </div>
              </li>
            ))
          )}
        </ul>
      </section>
    </Shell>
  );
}

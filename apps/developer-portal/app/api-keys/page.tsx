"use client";

// API keys: list/create/rotate/revoke (spec/01 §B). Secret is shown ONCE on
// create/rotate; revoke is TOTP-gated with a DualLabel confirm.

import React, { useCallback, useEffect, useState } from "react";
import { ApiError, createApiKey, listApiKeys, revokeApiKey, rotateApiKey } from "@/lib/api/client";
import type { ApiKey } from "@/lib/api/types";
import { API_KEY_SCOPES } from "@/lib/api/types";
import { EnvBadge, StateBadge } from "@/components/Badges";
import { SecretOnce } from "@/components/SecretOnce";
import { Shell } from "@/components/Shell";
import { TotpModal } from "@/components/TotpModal";
import { formatTs, shortId } from "@/lib/format";
import { useLang } from "@/lib/i18n/LangContext";
import { useMode } from "@/lib/mode";

type PendingAction =
  | { kind: "create"; keyName: string; scopes: string[] }
  | { kind: "rotate"; key: ApiKey }
  | { kind: "revoke"; key: ApiKey };

export default function ApiKeysPage() {
  const { t } = useLang();
  const { env } = useMode();
  const [keys, setKeys] = useState<ApiKey[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [keyName, setKeyName] = useState("");
  const [scopes, setScopes] = useState<string[]>(["payment:write", "payment:read"]);
  const [pending, setPending] = useState<PendingAction | null>(null);
  const [busy, setBusy] = useState(false);
  const [modalError, setModalError] = useState<string | null>(null);
  const [secretView, setSecretView] = useState<{ label: string; secret: string } | null>(null);

  const load = useCallback(() => {
    listApiKeys(env)
      .then((page) => {
        setKeys(page.data);
        setError(null);
      })
      .catch((err) => setError(err instanceof ApiError ? err.message : t("error_generic")));
  }, [env, t]);

  useEffect(() => {
    load();
  }, [load]);

  function toggleScope(s: string) {
    setScopes((prev) => (prev.includes(s) ? prev.filter((x) => x !== s) : [...prev, s]));
  }

  async function confirm(totpCode: string) {
    if (!pending) return;
    setBusy(true);
    setModalError(null);
    try {
      if (pending.kind === "create") {
        const created = await createApiKey(env, pending.keyName, pending.scopes, totpCode);
        setSecretView({ label: `Secret for "${created.key_name}"`, secret: created.secret });
        setKeyName("");
      } else if (pending.kind === "rotate") {
        const rotated = await rotateApiKey(pending.key.key_id, totpCode);
        const oldNote =
          rotated.old_secret_expires_at === null
            ? "old secret no longer works"
            : `old secret valid until ${formatTs(rotated.old_secret_expires_at)}`;
        setSecretView({
          label: `New secret for "${pending.key.key_name}" (${oldNote})`,
          secret: rotated.new_secret,
        });
      } else {
        await revokeApiKey(pending.key.key_id, totpCode);
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
      <h1>{t("nav_api_keys")}</h1>
      {error ? <p className="error-text">{error}</p> : null}
      {secretView ? <SecretOnce label={secretView.label} secret={secretView.secret} onDismiss={() => setSecretView(null)} /> : null}

      <section className="panel">
        <h2>{t("create_key")}</h2>
        <div className="toolbar">
          <label className="field">
            <span>Key name</span>
            <input value={keyName} onChange={(e) => setKeyName(e.target.value)} />
          </label>
          <EnvBadge env={env} />
        </div>
        <div className="actions-row">
          {API_KEY_SCOPES.map((s) => (
            <label key={s} className="subtle">
              <input type="checkbox" checked={scopes.includes(s)} onChange={() => toggleScope(s)} /> {s}
            </label>
          ))}
        </div>
        <button
          className="btn btn-primary"
          disabled={keyName.trim() === "" || scopes.length === 0}
          onClick={() => {
            setModalError(null);
            setPending({ kind: "create", keyName, scopes });
          }}
        >
          {t("create_key")}
        </button>
      </section>

      <section className="panel">
        <table className="data-table">
          <thead>
            <tr>
              <th>Key</th>
              <th>Name</th>
              <th>Scopes</th>
              <th>Status</th>
              <th>Last used</th>
              <th>Created</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {keys.length === 0 ? (
              <tr>
                <td colSpan={7} className="empty-row">
                  No keys in this environment
                </td>
              </tr>
            ) : (
              keys.map((k) => (
                <tr key={k.key_id}>
                  <td className="mono">
                    {k.key_prefix}
                    {shortId(k.key_id.split("_")[1] ?? "", 8)}
                  </td>
                  <td>{k.key_name}</td>
                  <td className="subtle">{k.scopes.join(", ")}</td>
                  <td>
                    <StateBadge value={k.status} />
                  </td>
                  <td>{formatTs(k.last_used_at)}</td>
                  <td>{formatTs(k.created_at)}</td>
                  <td>
                    {k.status === "ACTIVE" ? (
                      <span className="actions-row">
                        <button
                          className="btn btn-small"
                          onClick={() => {
                            setModalError(null);
                            setPending({ kind: "rotate", key: k });
                          }}
                        >
                          {t("rotate_key")}
                        </button>
                        <button
                          className="btn btn-small btn-danger"
                          onClick={() => {
                            setModalError(null);
                            setPending({ kind: "revoke", key: k });
                          }}
                        >
                          {t("revoke_key")}
                        </button>
                      </span>
                    ) : null}
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </section>

      {pending ? (
        <TotpModal
          titleKey={pending.kind === "create" ? "create_key" : pending.kind === "rotate" ? "rotate_key" : "revoke_key"}
          messageKey={pending.kind === "create" ? "secret_once" : pending.kind === "rotate" ? "rotate_key_confirm" : "revoke_key_confirm"}
          danger={pending.kind === "revoke"}
          busy={busy}
          error={modalError}
          onConfirm={(code) => void confirm(code)}
          onClose={() => setPending(null)}
        />
      ) : null}
    </Shell>
  );
}

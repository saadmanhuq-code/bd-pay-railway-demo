#!/bin/sh
# ============================================================================
# BD-PAY container entrypoint — one image, three roles.
#
#   app     gateway composition root (uvicorn factory, spec/01..19 monolith)
#   vault   isolated CDE sub-process (spec/14; own listener, own stores;
#           never shares a container, network, or UID namespace with the app)
#   migrate one-shot migration runner (applies db/migrations in order, then
#           exits; compose gates the app services on its successful exit)
#
# Invoked under tini (PID 1) so signals propagate and children are reaped.
# `exec` replaces the shell so uvicorn/python IS the supervised process.
# No credentials appear here: all configuration arrives via environment.
# ============================================================================
set -eu

ROLE="${1:-app}"

case "$ROLE" in
  app)
    exec uvicorn bdpay.app:create_app \
      --factory \
      --host "${BDPAY_LISTEN_HOST:-0.0.0.0}" \
      --port "${BDPAY_LISTEN_PORT:-8000}" \
      --workers "${BDPAY_UVICORN_WORKERS:-1}" \
      --no-server-header
    ;;
  vault)
    # Listener host/port come from VAULT_LISTEN_HOST / VAULT_LISTEN_PORT
    # (bdpay.vault.settings.VaultSettings.from_env). The vault __main__ owns
    # process wiring and the SAD purge on exit.
    exec python -m bdpay.vault
    ;;
  migrate)
    exec python /app/docker/run_migrations.py
    ;;
  *)
    echo "entrypoint: unknown role '$ROLE' (expected: app | vault | migrate)" >&2
    exit 64
    ;;
esac

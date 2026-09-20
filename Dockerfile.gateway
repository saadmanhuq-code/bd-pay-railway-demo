# syntax=docker/dockerfile:1.7
# ============================================================================
# BD-PAY production image — one image, three roles (app | vault | migrate).
#
# Hardening baseline (arch (g) reuse-map "Docker hardening" row, veridyn-smoke
# pattern): multi-stage build, non-root UID 10001, tini as PID 1, container
# HEALTHCHECK, pinned multi-arch base digest, bytecode writes disabled so the
# image runs cleanly under a read-only root filesystem (docker-compose.prod.yml
# sets read_only + cap_drop ALL + no-new-privileges per service).
#
# Multi-arch: the digest below is the MANIFEST-LIST digest for
# python:3.12-slim-bookworm (covers linux/amd64 AND linux/arm64 — verified
# against registry-1.docker.io on 2026-06-13). Docker resolves the correct
# per-platform image from the index, so the same Dockerfile builds on x86
# CI runners and on the ARM rehearsal host.
#
# Roles (selected by the container command, dispatched in docker/entrypoint.sh):
#   app     -> uvicorn bdpay.app:create_app --factory   (gateway composition root)
#   vault   -> python -m bdpay.vault                    (isolated CDE sub-process)
#   migrate -> docker/run_migrations.py                 (one-shot, applies db/migrations)
# ============================================================================

ARG PYTHON_BASE=python:3.12-slim-bookworm@sha256:76d4b7b6305788c6b4c6a19d6a22a3921bf802e9af4d5e1e5bd771208dba74bf

# ── Stage 1: builder ─────────────────────────────────────────────────────────
FROM ${PYTHON_BASE} AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

# Manifest first (layer-cache friendly), then the package source.
COPY pyproject.toml ./
COPY bdpay ./bdpay

# Self-contained virtualenv; copied wholesale into the runtime stage so the
# runtime image carries no compilers, no pip cache, no build context.
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir .

# ── Stage 2: runtime ─────────────────────────────────────────────────────────
FROM ${PYTHON_BASE} AS runtime

# tini: proper PID-1 signal forwarding + zombie reaping for uvicorn workers.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tini \
    && rm -rf /var/lib/apt/lists/*

# Non-root runtime identity (veridyn-smoke baseline: UID 10001, no shell,
# no home, no login). Everything under /app stays root-owned and read-only
# to the runtime user — the process cannot modify its own code.
RUN groupadd --gid 10001 bdpay \
    && useradd --uid 10001 --gid 10001 \
        --shell /usr/sbin/nologin --no-create-home --home-dir /nonexistent \
        bdpay

ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv

# Migrations ship in the image so the one-shot migration-runner service uses
# the exact same artifact as the app containers (no bind-mounted source).
COPY db/migrations /app/db/migrations
COPY docker/entrypoint.sh docker/healthcheck.py docker/run_migrations.py /app/docker/
RUN chmod 0555 /app/docker/entrypoint.sh /app/docker/healthcheck.py /app/docker/run_migrations.py

# Durable checkpoint-trust custody (F01 / review finding
# ledger-checkpoint-custody-not-deployable): build_services() bootstraps
# var/ledger/checkpoint_trust.json unconditionally, but /app is root-owned
# and the compose file runs it read_only. The directory must exist AND be
# owned by the runtime UID before it becomes a mounted named-volume
# mountpoint (docker-compose.prod.yml mounts ledger-custody here on every
# role that calls build_services), so the volume inherits a writable owner
# on first mount instead of docker defaulting the empty mountpoint to root.
RUN mkdir -p /app/var/ledger && chown -R 10001:10001 /app/var/ledger

USER 10001

# Gateway (role: app) and vault listener (role: vault).
EXPOSE 8000 9614

# Role-aware probe: defaults to the gateway /healthz; the vault service
# overrides BDPAY_HEALTH_URL to its loopback /vault/v1/health (which the
# vault API serves unauthenticated for loopback callers only).
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "/app/docker/healthcheck.py"]

ENTRYPOINT ["/usr/bin/tini", "--", "/app/docker/entrypoint.sh"]
CMD ["app"]

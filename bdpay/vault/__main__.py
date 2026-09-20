"""Entrypoint for the isolated vault sub-process (spec/14 topology).

Run as ``python -m bdpay.vault``. The vault is the only component outside
the single deployable: its own listener, its own stores, its own audit
chain; it never imports kernel/ledger/compliance/connectors/qr. Production
hardening (systemd unit, unshared UID, read-only rootfs, VLAN rules) is
deployment configuration documented in spec/14; this entrypoint owns the
process wiring only.
"""

from __future__ import annotations

import os

import uvicorn

from bdpay.platform.clock import SystemClock
from bdpay.vault.api import create_app
from bdpay.vault.settings import VaultSettings
from bdpay.vault.wiring import build_vault


def main() -> None:
    settings = VaultSettings.from_env(os.environ)
    runtime = build_vault(settings, SystemClock())
    app = create_app(runtime)
    try:
        uvicorn.run(app, host=settings.listen_host, port=settings.listen_port, log_level="warning")
    finally:
        # Process-exit SAD purge (spec/14: zeroized on process exit).
        runtime.service.sad.shutdown()


if __name__ == "__main__":
    main()

"""Container health probe (stdlib only — runs inside the hardened image).

Role-aware via ``BDPAY_HEALTH_URL``:

- app containers:   default ``http://127.0.0.1:8000/healthz`` (gateway
  system.health route — public, skip_auth, loopback).
- vault containers: compose sets ``BDPAY_HEALTH_URL`` to
  ``http://127.0.0.1:9614/vault/v1/health`` — the vault API serves that
  endpoint without channel auth for loopback callers only
  (``bdpay/vault/api.py`` ``_LOCAL_HOSTS``), so the probe never needs a
  caller secret.

Exit 0 on HTTP 200, exit 1 on anything else (connection refused, timeout,
non-200). No response body is printed — health payloads stay out of the
container event log.
"""

from __future__ import annotations

import os
import sys
import urllib.error
import urllib.request

DEFAULT_URL = "http://127.0.0.1:8000/healthz"
TIMEOUT_SECONDS = 4


def main() -> int:
    url = os.environ.get("BDPAY_HEALTH_URL", DEFAULT_URL)
    if not url.startswith("http://127.0.0.1") and not url.startswith("http://localhost"):
        # The probe is a loopback check by design; refuse anything else.
        print("healthcheck: BDPAY_HEALTH_URL must target loopback", file=sys.stderr)
        return 1
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310
            return 0 if response.status == 200 else 1
    except (urllib.error.URLError, OSError, TimeoutError):
        return 1


if __name__ == "__main__":
    sys.exit(main())

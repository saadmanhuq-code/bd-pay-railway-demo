"""Gateway entity IDs — format-parity shim for spec/01 prefixes.

spec/01 assigns ID prefixes ``akey`` (ApiKey), ``osess`` (OperatorSession),
``whe``/``whd`` (webhook endpoint/delivery) and ``req_`` request ids, none of
which are in the frozen spec/00 §3 table that ``bdpay.platform.ids.PREFIXES``
pins (errata G-3 in SPEC_ERRATA-LANE-A-gateway.md; same shim pattern as
errata E18 / PR-3). ``make_gateway_id`` produces the identical
``<prefix>_<sha256_canonical(payload)[:24]>`` scheme over the one binding
canonical-JSON implementation, so it collapses to a plain ``make_id`` call at
the next prefix fold-in.

Request ids are content-addressed over request method + path + a process
seed + a monotonic counter — never a random UUID, never a wall-clock read.
"""

from __future__ import annotations

import threading

from bdpay.platform.canonical import sha256_canonical
from bdpay.platform.ids import HASH_LENGTH

__all__ = ["GATEWAY_PREFIXES", "GatewayIdPrefixError", "RequestIdGenerator", "make_gateway_id"]

#: spec/01 additive prefixes pending fold-in to spec/00 §3 (errata G-3), plus
#: the spec/16 LR-4 ``plink`` PaymentLink and ``sbxs`` SandboxSignup prefixes
#: (same E18/E28 shim class).
GATEWAY_PREFIXES: frozenset[str] = frozenset(
    {"akey", "osess", "whe", "whd", "req", "plink", "sbxs"}
)


class GatewayIdPrefixError(ValueError):
    """Raised when a prefix outside the spec/01 gateway prefix set is used."""


def make_gateway_id(prefix: str, payload: dict) -> str:
    """Return ``<prefix>_<sha256_canonical(payload)[:24]>`` (format parity)."""
    if not isinstance(prefix, str) or prefix not in GATEWAY_PREFIXES:
        raise GatewayIdPrefixError(
            f"unknown gateway id prefix {prefix!r}; allowed: {sorted(GATEWAY_PREFIXES)}"
        )
    if not isinstance(payload, dict):
        raise TypeError(f"payload must be dict, got {type(payload).__name__}")
    return f"{prefix}_{sha256_canonical(payload)[:HASH_LENGTH]}"


class RequestIdGenerator:
    """Deterministic ``req_<...>`` ids: content + monotonic counter, no UUIDs.

    ``seed`` distinguishes processes (the app wiring passes entropy once at
    startup); tests pass a fixed seed for reproducible ids. The counter makes
    every id unique within a process even for identical method+path.
    """

    def __init__(self, *, seed: str = "") -> None:
        if not isinstance(seed, str):
            raise TypeError(f"seed must be str, got {type(seed).__name__}")
        self._seed = seed
        self._counter = 0
        self._lock = threading.Lock()

    def next_id(self, method: str, path: str) -> str:
        with self._lock:
            self._counter += 1
            counter = self._counter
        return make_gateway_id(
            "req",
            {"counter": counter, "method": method, "path": path, "seed": self._seed},
        )

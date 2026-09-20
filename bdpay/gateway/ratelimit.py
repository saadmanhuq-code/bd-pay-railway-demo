"""Fixed-window rate limiter — ported from portfolio-core ``rate-limit``.

Ported from portfolio-core/packages/rate-limit (src/types.ts, src/check.ts,
src/headers.ts) per PORTING-MAP.md (ADAPT: TS -> Python, Redis INCR +
in-memory stores). The proven shape is preserved exactly:

- The store owns ONE atomic operation: increment-and-return-new-count for a
  (key, window_start_ms) bucket. It never decides ``allowed``, never reads a
  clock, and must propagate failures (never fabricate a count).
- The core owns window-start math (``floor(now/window)*window``), the
  allow/deny decision (``count <= max`` — the request reaching ``max`` is the
  last allowed one), shadow mode (count but never block), and the
  fail-closed-on-store-error default.
- ``client_ip_from_headers`` reads the trusted ``x-real-ip`` header ONLY and
  deliberately ignores ``x-forwarded-for`` (client-appendable chain).

Divergence (documented in the source spec, spec/01 §Rate Limiting): the
gateway's payment path runs the limiter **fail-open** — blocking all payment
traffic on a Redis outage is the worse regulatory outcome — with the
degradation surfaced via the ``X-RateLimit-Mode: degraded`` header. The core
default stays fail-closed, exactly as the donor package ships it; spec/01
naming says "token bucket" while binding the fixed-window INCR algorithm —
the algorithm wins (errata G-4).

Naming note: this module avoids 13+ digit example literals; epoch values in
tests use small fixed instants.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from bdpay.platform.clock import Clock

__all__ = [
    "RATE_LIMIT_TIERS",
    "InMemoryRateLimitStore",
    "RateLimitConfigError",
    "RateLimitResult",
    "RateLimiter",
    "RateLimitStore",
    "RedisRateLimitStore",
    "check",
    "client_ip_from_headers",
    "window_start_ms",
]

#: spec/01 rate-limit tier table: route_group -> principal class -> req/min.
RATE_LIMIT_TIERS: Mapping[str, Mapping[str, int]] = {
    "payment_write": {
        "merchant_live": 300,
        "merchant_test": 60,
        "customer": 60,
        "operator": 600,
    },
    "payment_read": {
        "merchant_live": 600,
        "merchant_test": 120,
        "customer": 120,
        "operator": 1200,
    },
    "refund_write": {
        "merchant_live": 60,
        "merchant_test": 20,
        "customer": 10,
        "operator": 120,
    },
    "webhook_write": {"merchant_live": 30, "merchant_test": 10, "operator": 60},
    "auth": {"customer": 10, "operator": 60},
    "admin": {"operator": 120},
}

_DEFAULT_PRINCIPAL_LIMIT = 60
_WINDOW_SECONDS = 60


class RateLimitConfigError(ValueError):
    """Invalid limiter configuration (programming error -> raise)."""


@runtime_checkable
class RateLimitStore(Protocol):
    """The single atomic operation the donor package's adapter contract pins.

    Must atomically create-or-increment the counter for
    ``(key, window_start_ms)`` and return the post-increment count (>= 1).
    Failures propagate as exceptions; the core folds them into the
    fail-closed/fail-open result.
    """

    def increment(self, key: str, window_start_ms: int) -> int: ...


class InMemoryRateLimitStore:
    """Thread-safe in-memory counter store (unit tests / single process)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._buckets: dict[tuple[str, int], int] = {}

    def increment(self, key: str, window_start_ms: int) -> int:
        with self._lock:
            bucket = (key, window_start_ms)
            count = self._buckets.get(bucket, 0) + 1
            self._buckets[bucket] = count
            # opportunistic pruning of windows older than the current one
            stale = [b for b in self._buckets if b[0] == key and b[1] < window_start_ms]
            for b in stale:
                del self._buckets[b]
            return count


class RedisRateLimitStore:
    """Redis INCR + EXPIRE pipeline (atomic across gateway instances).

    The 120s TTL covers the spec/01 2x-window clock-skew note. The client is
    duck-typed (``pipeline()`` -> ``incr``/``expire``/``execute``) so tests
    can supply a fake without a live Redis.
    """

    def __init__(self, client: object, *, ttl_seconds: int = 120) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self._client = client
        self._ttl = ttl_seconds

    def close(self) -> None:
        close = getattr(self._client, "close", None)
        if callable(close):
            close()

    def increment(self, key: str, window_start_ms: int) -> int:
        pipe = self._client.pipeline()  # type: ignore[attr-defined]
        redis_key = f"rl:{key}:{window_start_ms}"
        pipe.incr(redis_key)
        pipe.expire(redis_key, self._ttl)
        count, _ = pipe.execute()
        return int(count)


@dataclass(frozen=True)
class RateLimitResult:
    """Every field always present (donor package result contract).

    ``degraded`` is an additive field over the donor shape: True only when
    the store failed and the policy (fail-open/fail-closed/shadow) decided
    the outcome — it feeds the spec/01 ``X-RateLimit-Mode: degraded`` header.
    """

    allowed: bool
    would_block: bool
    remaining: int
    reset_at_ms: int
    count: int
    degraded: bool = False


def window_start_ms(now_ms: int, window_ms: int) -> int:
    """``floor(now/window)*window`` — the donor package's window-start math."""
    return (now_ms // window_ms) * window_ms


def check(
    key: str,
    *,
    max_requests: int,
    window_seconds: int,
    store: RateLimitStore,
    now_ms: int,
    fail_closed: bool = True,
    shadow: bool = False,
) -> RateLimitResult:
    """Fixed-window check (direct port of the donor ``check`` semantics).

    ``count <= max`` allows; shadow mode never blocks (and wins over
    fail-closed on a store error); a store error otherwise folds into a
    fail-closed (deny, sentinel count ``max+1``) or fail-open (allow, count 1)
    result — it never raises out of here.
    """
    if isinstance(max_requests, bool) or not isinstance(max_requests, int) or max_requests <= 0:
        raise RateLimitConfigError(f"max_requests must be a positive integer, got {max_requests!r}")
    if (
        isinstance(window_seconds, bool)
        or not isinstance(window_seconds, int)
        or window_seconds <= 0
    ):
        raise RateLimitConfigError(
            f"window_seconds must be a positive integer, got {window_seconds!r}"
        )
    window_ms = window_seconds * 1000
    start = window_start_ms(now_ms, window_ms)
    reset_at = start + window_ms

    try:
        count = store.increment(key, start)
    except Exception:  # noqa: BLE001 - store failure folds into the policy result
        if shadow or not fail_closed:
            return RateLimitResult(
                allowed=True,
                would_block=False,
                remaining=max(0, max_requests - 1),
                reset_at_ms=reset_at,
                count=1,
                degraded=True,
            )
        return RateLimitResult(
            allowed=False,
            would_block=True,
            remaining=0,
            reset_at_ms=reset_at,
            count=max_requests + 1,
            degraded=True,
        )

    would_block = count > max_requests
    return RateLimitResult(
        allowed=True if shadow else not would_block,
        would_block=would_block,
        remaining=max(0, max_requests - count),
        reset_at_ms=reset_at,
        count=count,
    )


def client_ip_from_headers(headers: Mapping[str, str]) -> str | None:
    """Trusted ``x-real-ip`` only; ``x-forwarded-for`` is deliberately ignored."""
    raw = headers.get("x-real-ip")
    if raw is None:
        return None
    trimmed = raw.strip()
    return trimmed if trimmed else None


class RateLimiter:
    """Gateway policy layer over the ported core: tiers + per-IP buckets."""

    def __init__(
        self,
        store: RateLimitStore,
        *,
        clock: Clock,
        mode: str = "enforce",
        fail_open: bool = True,
        public_ip_limit_per_minute: int = 10,
        tiers: Mapping[str, Mapping[str, int]] = RATE_LIMIT_TIERS,
    ) -> None:
        if mode not in ("enforce", "shadow"):
            raise RateLimitConfigError(f"mode must be enforce|shadow, got {mode!r}")
        self._store = store
        self._clock = clock
        self._shadow = mode == "shadow"
        self._fail_open = fail_open
        self._public_ip_limit = public_ip_limit_per_minute
        self._tiers = tiers

    def _now_ms(self) -> int:
        return int(self._clock.now().timestamp() * 1000)

    @staticmethod
    def principal_class(kind: str, key_env: str | None) -> str:
        if kind == "merchant_key":
            return "merchant_test" if key_env == "test" else "merchant_live"
        return kind

    def limit_for(self, route_group: str, principal_class: str) -> int:
        return self._tiers.get(route_group, {}).get(principal_class, _DEFAULT_PRINCIPAL_LIMIT)

    def check_principal(
        self, *, rate_key: str, route_group: str, principal_class: str
    ) -> tuple[RateLimitResult, int]:
        limit = self.limit_for(route_group, principal_class)
        result = check(
            f"p:{rate_key}:{route_group}",
            max_requests=limit,
            window_seconds=_WINDOW_SECONDS,
            store=self._store,
            now_ms=self._now_ms(),
            fail_closed=not self._fail_open,
            shadow=self._shadow,
        )
        return result, limit

    def check_ip(
        self, *, ip_hash: str, route_group: str, limit: int | None = None
    ) -> tuple[RateLimitResult, int]:
        effective = self._public_ip_limit if limit is None else limit
        result = check(
            f"ip:{ip_hash}:{route_group}",
            max_requests=effective,
            window_seconds=_WINDOW_SECONDS,
            store=self._store,
            now_ms=self._now_ms(),
            fail_closed=not self._fail_open,
            shadow=self._shadow,
        )
        return result, effective

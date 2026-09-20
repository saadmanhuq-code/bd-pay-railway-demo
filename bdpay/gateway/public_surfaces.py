"""Public surfaces (spec/16 LR-4 §E) — status feed, certification matrix, badge.

Service layer behind the three unauthenticated read-only endpoints
(``GET /v1/public/status``, ``GET /v1/public/certification-matrix``,
``GET /v1/public/badges/{connector_id}.svg``). The HTTP route layer is wired
separately; this module exposes plain methods returning dicts / bytes.

Binding rules implemented here:

- Cached reads only: registry tables are never hit per-request. ``status()``
  and ``certification_matrix()`` cache for ``status_cache_seconds`` (60),
  ``badge_svg()`` for ``badge_cache_seconds`` (300); within the window the
  read port is not touched.
- Modes are shown verbatim — ``SIMULATOR`` / ``SANDBOX`` are labeled honestly.
- Report **hashes** are public; report files/pointers are NOT — only
  ``report_hash`` from the latest PASSED ``CertificationRun`` is exposed.
- Uptime is expressed as integer **basis points** (0..10000) computed with
  pure integer arithmetic (``healthy * 10000 // total``) — no floats exist
  anywhere in this module (canonical_json rejects floats by construction).
  An empty sample window yields ``None`` (refusal of fabrication: no samples
  means no uptime claim).
- PII-free by construction: built payloads are asserted to carry none of the
  known PII field names before they are cached.

Event consumers (``certification_run.completed``,
``connector_registration.health_changed`` / ``.circuit_*``) call
``consume()`` / ``invalidate()`` to refresh the caches.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from bdpay.connectors.registry import ConnectorRegistry
    from bdpay.connectors.stores import CertificationRunStore, HealthSampleStore
    from bdpay.platform.clock import Clock

__all__ = [
    "BADGE_COLOR_BY_STATUS",
    "CertificationView",
    "HealthSampleView",
    "PiiLeakError",
    "PublicReadPort",
    "PublicSurfaceService",
    "RegistrationView",
    "RegistryReadAdapter",
    "assert_pii_free",
]

#: Status -> badge fill color (pinned; unknown statuses render NEVER_RUN grey).
BADGE_COLOR_BY_STATUS: dict[str, str] = {
    "PASSED": "#1a7f37",
    "FAILED": "#b3261e",
    "STALE": "#9a6700",
    "NEVER_RUN": "#6b7280",
}
_BADGE_FALLBACK_COLOR = "#6b7280"

#: Field names that must never appear in a public payload (PII guard).
_FORBIDDEN_KEYS = frozenset(
    {
        "email",
        "phone",
        "msisdn",
        "nid",
        "national_id",
        "date_of_birth",
        "dob",
        "address",
        "customer_id",
        "account_number",
        "card_number",
        "pan",
    }
)

_DAY_S = 86_400
_WEEK_S = 7 * _DAY_S
_UPTIME_SCALE_BPS = 10_000

_EVENT_TYPES_CONSUMED = frozenset(
    {
        "certification_run.completed",
        "connector_registration.health_changed",
        "connector_registration.circuit_opened",
        "connector_registration.circuit_half_opened",
        "connector_registration.circuit_closed",
    }
)


class PiiLeakError(ValueError):
    """A public payload carried a forbidden (PII-adjacent) field name."""


def assert_pii_free(payload: object) -> None:
    """Recursively refuse any payload whose mapping keys include PII names."""
    if isinstance(payload, Mapping):
        for key, value in payload.items():
            if isinstance(key, str) and key.lower() in _FORBIDDEN_KEYS:
                raise PiiLeakError(f"public payload carries forbidden field {key!r}")
            assert_pii_free(value)
    elif isinstance(payload, list | tuple):
        for item in payload:
            assert_pii_free(item)


def _rfc3339(dt: datetime | None) -> str | None:
    """UTC RFC3339 with millisecond precision and ``Z`` suffix (or None)."""
    if dt is None:
        return None
    return dt.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _xml_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


# -- read port ---------------------------------------------------------------


@dataclass(frozen=True)
class RegistrationView:
    """Read-model row: the registry fields the public surfaces may see."""

    connector_id: str
    display_name: str
    active_mode: str
    health_status: str
    circuit_state: str
    last_health_at: datetime | None
    certification_status: str
    last_certified_at: datetime | None
    adapter_version: str


@dataclass(frozen=True)
class HealthSampleView:
    """One health sample: responsive (HEALTHY/DEGRADED) or not, and when."""

    healthy: bool
    sampled_at: datetime


@dataclass(frozen=True)
class CertificationView:
    """Latest PASSED certification run: per-check verdicts + report hash only.

    The report pointer / object-store key never crosses this boundary.
    """

    checks: tuple[tuple[str, str], ...]  # (check_id, verdict) pairs
    report_hash: str | None


@runtime_checkable
class PublicReadPort(Protocol):
    """Reads the public surfaces are allowed to make (cache-fill only)."""

    def list_registrations(self) -> list[RegistrationView]: ...

    def health_samples(self, connector_id: str, *, since: datetime) -> list[HealthSampleView]: ...

    def latest_passed_certification(self, connector_id: str) -> CertificationView | None: ...


# -- service -----------------------------------------------------------------


@dataclass
class _CacheEntry:
    built_at: datetime
    value: object


class PublicSurfaceService:
    """Cached read service behind the three LR-4 public endpoints."""

    def __init__(
        self,
        reads: PublicReadPort,
        clock: Clock,
        *,
        status_cache_seconds: int = 60,
        badge_cache_seconds: int = 300,
    ) -> None:
        self._reads = reads
        self._clock = clock
        self._status_ttl = timedelta(seconds=status_cache_seconds)
        self._badge_ttl = timedelta(seconds=badge_cache_seconds)
        self._status_cache: _CacheEntry | None = None
        self._matrix_cache: _CacheEntry | None = None
        self._badge_cache: dict[str, _CacheEntry] = {}

    # -- cache plumbing --------------------------------------------------------

    def _fresh(self, entry: _CacheEntry | None, ttl: timedelta, now: datetime) -> bool:
        return entry is not None and (now - entry.built_at) < ttl

    def invalidate(self, connector_id: str | None = None) -> None:
        """Bust caches; ``None`` busts everything, an id also keeps other badges."""
        self._status_cache = None
        self._matrix_cache = None
        if connector_id is None:
            self._badge_cache.clear()
        else:
            self._badge_cache.pop(connector_id, None)

    def consume(self, event: Mapping) -> bool:
        """Map a bus event to a cache invalidation; unknown events are ignored.

        Accepts both the sink shape ``{"type": ..., "payload": {...}}`` and a
        flat ``{"event_type": ..., "connector_id": ...}``. Returns True when
        the event caused an invalidation.
        """
        event_type = event.get("type") or event.get("event_type")
        if event_type not in _EVENT_TYPES_CONSUMED:
            return False
        payload = event.get("payload")
        connector_id = event.get("connector_id")
        if connector_id is None and isinstance(payload, Mapping):
            connector_id = payload.get("connector_id")
        self.invalidate(connector_id if isinstance(connector_id, str) else None)
        return True

    # -- uptime ----------------------------------------------------------------

    @staticmethod
    def _uptime_bps(samples: list[HealthSampleView]) -> int | None:
        """Uptime as integer basis points (0..10000).

        ``healthy * 10000 // total`` — floor division, pure integers, no
        floats. A sample is "up" when its ``healthy`` flag is True (HEALTHY
        and DEGRADED are both responsive states). Empty window -> ``None``
        (no samples means no uptime claim — fabrication refused).
        """
        if not samples:
            return None
        up = sum(1 for s in samples if s.healthy)
        return up * _UPTIME_SCALE_BPS // len(samples)

    # -- status feed (GET /v1/public/status) -----------------------------------

    def status(self) -> dict:
        """Connector-health status feed; cached for ``status_cache_seconds``."""
        now = self._clock.now()
        if self._fresh(self._status_cache, self._status_ttl, now):
            return self._status_cache.value  # type: ignore[union-attr, return-value]
        connectors: dict[str, dict] = {}
        for reg in sorted(self._reads.list_registrations(), key=lambda r: r.connector_id):
            day = self._reads.health_samples(
                reg.connector_id, since=now - timedelta(seconds=_DAY_S)
            )
            week = self._reads.health_samples(
                reg.connector_id, since=now - timedelta(seconds=_WEEK_S)
            )
            connectors[reg.connector_id] = {
                "display_name": reg.display_name,
                "active_mode": reg.active_mode,  # verbatim: SIMULATOR/SANDBOX shown honestly
                "health_status": reg.health_status,
                "circuit_state": reg.circuit_state,
                "uptime_24h_bps": self._uptime_bps(day),
                "uptime_7d_bps": self._uptime_bps(week),
                "last_health_at": _rfc3339(reg.last_health_at),
            }
        payload = {"connectors": connectors, "generated_at": _rfc3339(now)}
        assert_pii_free(payload)
        self._status_cache = _CacheEntry(built_at=now, value=payload)
        return payload

    # -- certification matrix (GET /v1/public/certification-matrix) ------------

    def certification_matrix(self) -> dict:
        """Per-connector certification verdicts; hashes public, files not."""
        now = self._clock.now()
        if self._fresh(self._matrix_cache, self._status_ttl, now):
            return self._matrix_cache.value  # type: ignore[union-attr, return-value]
        connectors: dict[str, dict] = {}
        for reg in sorted(self._reads.list_registrations(), key=lambda r: r.connector_id):
            run = self._reads.latest_passed_certification(reg.connector_id)
            connectors[reg.connector_id] = {
                "certification_status": reg.certification_status,
                "last_certified_at": _rfc3339(reg.last_certified_at),
                "adapter_version": reg.adapter_version,
                "checks": dict(run.checks) if run is not None else None,
                "report_hash": run.report_hash if run is not None else None,
            }
        payload = {"connectors": connectors, "generated_at": _rfc3339(now)}
        assert_pii_free(payload)
        self._matrix_cache = _CacheEntry(built_at=now, value=payload)
        return payload

    # -- SVG badge (GET /v1/public/badges/{connector_id}.svg) ------------------

    def badge_svg(self, connector_id: str) -> tuple[bytes, str] | None:
        """Deterministic SVG badge bytes + sha256 hex; None for unknown id."""
        now = self._clock.now()
        cached = self._badge_cache.get(connector_id)
        if self._fresh(cached, self._badge_ttl, now):
            return cached.value  # type: ignore[union-attr, return-value]
        registration = next(
            (r for r in self._reads.list_registrations() if r.connector_id == connector_id),
            None,
        )
        if registration is None:
            return None
        svg = _render_badge(registration)
        result = (svg, hashlib.sha256(svg).hexdigest())
        self._badge_cache[connector_id] = _CacheEntry(built_at=now, value=result)
        return result


def _render_badge(registration: RegistrationView) -> bytes:
    """Two-tone text badge: connector id | certification status + date."""
    status = registration.certification_status
    color = BADGE_COLOR_BY_STATUS.get(status, _BADGE_FALLBACK_COLOR)
    certified = _rfc3339(registration.last_certified_at) or "never"
    label = _xml_escape(registration.connector_id)
    value = _xml_escape(f"{status} {certified}")
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="420" height="20" role="img" '
        f'aria-label="{label}: {value}">'
        '<rect width="180" height="20" fill="#374151"/>'
        f'<rect x="180" width="240" height="20" fill="{color}"/>'
        '<g font-family="Verdana,DejaVu Sans,sans-serif" font-size="11" fill="#ffffff">'
        f'<text x="8" y="14">{label}</text>'
        f'<text x="188" y="14">{value}</text>'
        "</g></svg>"
    )
    return svg.encode("utf-8")


# -- registry adapter ----------------------------------------------------------


class RegistryReadAdapter:
    """Adapts the spec/10 connectors registry + stores onto ``PublicReadPort``.

    ``breaker`` is anything exposing ``state(connector_id) -> str`` (the
    spec/10 ``CircuitBreaker``); the registry row itself does not carry the
    circuit state. When no breaker is wired, ``circuit_state`` reports
    ``"UNKNOWN"`` rather than inventing a value.
    """

    def __init__(
        self,
        registry: ConnectorRegistry,
        health_samples: HealthSampleStore,
        certification_runs: CertificationRunStore,
        *,
        breaker: object | None = None,
    ) -> None:
        self._registry = registry
        self._health = health_samples
        self._runs = certification_runs
        self._breaker = breaker

    def _circuit_state(self, connector_id: str) -> str:
        if self._breaker is None:
            return "UNKNOWN"
        return self._breaker.state(connector_id)  # type: ignore[attr-defined]

    def list_registrations(self) -> list[RegistrationView]:
        return [
            RegistrationView(
                connector_id=reg.connector_id,
                display_name=reg.display_name,
                active_mode=str(reg.active_mode.value),
                health_status=reg.health_status,
                circuit_state=self._circuit_state(reg.connector_id),
                last_health_at=reg.last_health_at,
                certification_status=reg.certification_status,
                last_certified_at=reg.last_certified_at,
                adapter_version=reg.adapter_version,
            )
            for reg in self._registry.list_all()
        ]

    def health_samples(self, connector_id: str, *, since: datetime) -> list[HealthSampleView]:
        return [
            HealthSampleView(healthy=row.healthy, sampled_at=row.sampled_at)
            for row in self._health.list_for(connector_id)
            if row.sampled_at >= since
        ]

    def latest_passed_certification(self, connector_id: str) -> CertificationView | None:
        for record in reversed(self._runs.list_for(connector_id)):
            if record.status == "PASSED":
                checks = tuple(
                    (str(check.get("check_id")), str(check.get("verdict")))
                    for check in record.checks
                )
                return CertificationView(checks=checks, report_hash=record.report_hash)
        return None

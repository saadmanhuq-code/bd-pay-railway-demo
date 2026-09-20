"""Per-connector CircuitBreaker FSM — CLOSED / OPEN / HALF_OPEN (spec/10 §FSM 2).

Ported from dse-profit-engine/src/dse_engine/pod/circuit_breaker.py (ADAPT):
keyed by ``connector_id`` instead of account; the loss-bps thresholds are
replaced by consecutive-failure count within a rolling window. Retained
verbatim from the source: FAIL-CLOSED — if the breaker state cannot be read
from the store, the breaker reports OPEN (the source's ``tripped=True`` on a
DB read error); the evaluation path of ``state()`` is a pure read; ``now`` is
injected for deterministic tests.

Trip condition: >= ``failure_threshold`` (default 5) consecutive failures
within ``window_s`` (default 60s). FAILED, TIMED_OUT and transport errors
count; REJECTED does NOT (a hard rail decline is a healthy rail saying no).
OPEN -> HALF_OPEN after ``cooldown_s`` (default 30s) or operator reset.
HALF_OPEN -> CLOSED after ``probe_quota`` (default 3) probe successes; any
probe failure -> OPEN (``opened_at := now``). ``submit()`` is NOT admitted in
HALF_OPEN — only health_check and at most ``probe_quota`` query_status probes.
Per-connector overrides come from the spec/10 budget table via
``bdpay.connectors.registry.breaker_config_for``.

Pure-read evaluation (retained verbatim from the port source): ``admit`` and
``state`` never write for an unknown connector — a missing record evaluates
as a fresh CLOSED default without being persisted; rows are first persisted
by ``observe``/``reset`` (the FSM writers). ``observe`` is fail-soft on a
store read error: the outcome is dropped silently because the matching
``admit`` fail-closed path already denies every call while the store is
unreadable (the source's ``tripped=True`` on a DB read error).
"""

from __future__ import annotations

from dataclasses import dataclass

from bdpay.connectors.ports import AuditSink, EventSink, InMemoryAuditSink, InMemoryEventSink
from bdpay.connectors.sdk import ConnectorStatus
from bdpay.connectors.stores import (
    BreakerRecord,
    BreakerStateStore,
    InMemoryBreakerStateStore,
    make_breaker_record,
)
from bdpay.platform.clock import Clock
from bdpay.platform.errors import ConflictError

__all__ = ["BreakerConfig", "CircuitBreaker", "BreakerResetDeniedError"]

_FAILURE_STATUSES = frozenset({ConnectorStatus.FAILED.value, ConnectorStatus.TIMED_OUT.value})
_PROBE_KINDS = frozenset({"HEALTH", "QUERY_STATUS"})
_SYSTEM_ACTOR = "system:circuit-breaker"


class BreakerResetDeniedError(ConflictError):
    """Operator reset is only legal from OPEN (refusal-first FSM)."""

    default_code = "breaker_reset_denied"


@dataclass(frozen=True)
class BreakerConfig:
    """Threshold set per connector (spec/10 budget table, breaker overrides)."""

    failure_threshold: int = 5
    window_s: int = 60
    cooldown_s: int = 30
    probe_quota: int = 3
    informational: bool = False  # goaml_reporter_v1: breaker never blocks filings


class CircuitBreaker:
    """Failure-count breaker keyed by connector_id; fail-closed on store errors."""

    def __init__(
        self,
        store: BreakerStateStore | None = None,
        *,
        clock: Clock,
        audit: AuditSink | None = None,
        events: EventSink | None = None,
        configs: dict[str, BreakerConfig] | None = None,
        production_connectors: frozenset[str] | None = None,
    ) -> None:
        self._store = store if store is not None else InMemoryBreakerStateStore()
        self._clock = clock
        self._audit = audit if audit is not None else InMemoryAuditSink()
        self._events = events if events is not None else InMemoryEventSink()
        self._configs = dict(configs or {})
        self._production = production_connectors or frozenset()

    # -- record access --------------------------------------------------------

    def _config(self, connector_id: str) -> BreakerConfig:
        return self._configs.get(connector_id, BreakerConfig())

    def _default_record(self, connector_id: str) -> BreakerRecord:
        cfg = self._config(connector_id)
        record = make_breaker_record(
            connector_id,
            failure_threshold=cfg.failure_threshold,
            window_s=cfg.window_s,
            cooldown_s=cfg.cooldown_s,
            probe_quota=cfg.probe_quota,
        )
        record.updated_at = self._clock.now()
        return record

    def _load_or_default(self, connector_id: str) -> BreakerRecord:
        """Read path: missing record evaluates as fresh CLOSED, NOT persisted."""
        record = self._store.load(connector_id)
        return record if record is not None else self._default_record(connector_id)

    def _load_or_create(self, connector_id: str) -> BreakerRecord:
        """Write path: missing record is created and persisted (FSM writers)."""
        record = self._store.load(connector_id)
        if record is None:
            record = self._default_record(connector_id)
            self._store.save(record)
        return record

    def _save(self, record: BreakerRecord) -> None:
        record.updated_at = self._clock.now()
        self._store.save(record)

    # -- transitions ----------------------------------------------------------

    def _to_open(self, record: BreakerRecord, *, reason: str) -> None:
        record.state = "OPEN"
        record.opened_at = self._clock.now()
        record.half_open_successes = 0
        record.half_open_probes = 0
        self._save(record)
        self._audit.record(
            "CONNECTOR_CIRCUIT_OPENED",
            actor_id=_SYSTEM_ACTOR,
            occurred_at=self._clock.now(),
            detail={"connector_id": record.connector_id, "reason": reason},
        )
        self._events.emit(
            "connector_registration.circuit_opened",
            {
                "connector_id": record.connector_id,
                "consecutive_failures": record.consecutive_failures,
                "window_s": record.window_s,
            },
        )

    def _to_half_open(self, record: BreakerRecord, *, actor_id: str) -> None:
        record.state = "HALF_OPEN"
        record.half_open_successes = 0
        record.half_open_probes = 0
        self._save(record)
        self._audit.record(
            "CONNECTOR_CIRCUIT_HALF_OPENED",
            actor_id=actor_id,
            occurred_at=self._clock.now(),
            detail={"connector_id": record.connector_id},
        )
        self._events.emit(
            "connector_registration.circuit_half_opened",
            {"connector_id": record.connector_id},
        )

    def _to_closed(self, record: BreakerRecord) -> None:
        record.state = "CLOSED"
        record.consecutive_failures = 0
        record.window_started_at = None
        record.opened_at = None
        record.half_open_successes = 0
        record.half_open_probes = 0
        self._save(record)
        self._audit.record(
            "CONNECTOR_CIRCUIT_CLOSED",
            actor_id=_SYSTEM_ACTOR,
            occurred_at=self._clock.now(),
            detail={"connector_id": record.connector_id},
        )
        self._events.emit(
            "connector_registration.circuit_closed",
            {"connector_id": record.connector_id},
        )

    def _maybe_cooldown(self, record: BreakerRecord) -> None:
        if record.state != "OPEN" or record.opened_at is None:
            return
        elapsed = (self._clock.now() - record.opened_at).total_seconds()
        if elapsed >= record.cooldown_s:
            self._to_half_open(record, actor_id=_SYSTEM_ACTOR)

    # -- public surface -------------------------------------------------------

    async def admit(self, connector_id: str, *, kind: str = "SUBMIT") -> bool:
        """May this call touch the rail? Fail-closed: store error denies all."""
        try:
            record = self._load_or_default(connector_id)
        except Exception:
            return False  # fail-closed: unknowable health is OPEN (port source semantics)
        if self._config(connector_id).informational:
            return True
        self._maybe_cooldown(record)
        if record.state == "CLOSED":
            return True
        if record.state == "OPEN":
            return False
        # HALF_OPEN: no new money on a suspect rail.
        if kind == "HEALTH":
            return True
        if kind in _PROBE_KINDS:
            if record.half_open_probes >= record.probe_quota:
                return False
            record.half_open_probes += 1
            self._save(record)
            return True
        return False

    async def observe(
        self,
        connector_id: str,
        status: ConnectorStatus | str,
        *,
        transport_error: bool = False,
    ) -> None:
        """Feed a call outcome into the FSM (REJECTED is a healthy outcome).

        Fail-soft on a store read error: the observation is dropped because
        ``admit`` already fail-closes (denies everything) while the store is
        unreadable, so no unsafe dispatch can slip through unobserved.
        """
        try:
            record = self._load_or_create(connector_id)
        except Exception:
            return
        status_value = status.value if isinstance(status, ConnectorStatus) else str(status)
        failure = transport_error or status_value in _FAILURE_STATUSES
        now = self._clock.now()
        if record.state == "OPEN":
            return  # in-flight completions while OPEN carry no signal
        if record.state == "HALF_OPEN":
            if failure:
                self._to_open(record, reason="probe_failed")
                return
            record.half_open_successes += 1
            if record.half_open_successes >= record.probe_quota:
                self._to_closed(record)
            else:
                self._save(record)
            return
        # CLOSED
        if not failure:
            record.consecutive_failures = 0
            record.window_started_at = None
            self._save(record)
            return
        window_expired = (
            record.window_started_at is None
            or (now - record.window_started_at).total_seconds() > record.window_s
        )
        if window_expired:
            record.window_started_at = now
            record.consecutive_failures = 1
        else:
            record.consecutive_failures += 1
        if record.consecutive_failures >= record.failure_threshold:
            self._to_open(record, reason="failure_threshold")
        else:
            self._save(record)

    def reset(self, connector_id: str, *, actor_id: str) -> None:
        """Operator reset: OPEN -> HALF_OPEN only (refusal-first)."""
        record = self._load_or_default(connector_id)
        if record.state != "OPEN":
            raise BreakerResetDeniedError(
                f"breaker for {connector_id} is {record.state}; reset is legal only from OPEN"
            )
        self._to_half_open(record, actor_id=actor_id)

    def state(self, connector_id: str) -> str:
        """Current state; reports OPEN when the store is unreadable (fail-closed)."""
        try:
            record = self._load_or_default(connector_id)
        except Exception:
            return "OPEN"
        self._maybe_cooldown(record)
        return record.state

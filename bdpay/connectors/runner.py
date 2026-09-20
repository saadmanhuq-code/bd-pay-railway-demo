"""ConnectorRunner — the sole dispatch path kernel -> connector (spec/10).

``dispatch`` implements the spec dispatch algorithm exactly: registry resolve,
mode/enabled check (``connector_disabled``), breaker admit (``circuit_open``),
adapter lookup by ``(connector_id, active_mode)``, hard timeout from the
registry -> TIMED_OUT, result persisted (append-only), breaker observation.
Additionally (SDK idempotency invariant): a write-ahead ``DispatchIntent`` is
recorded for the ``connector_ref`` BEFORE the adapter is invoked, and a
replay-safe recorded outcome (success/pending/rejected/reversed) is returned
without touching the rail again.

Breaker fail-closed posture: while the circuit denies, ``dispatch`` returns
``ConnectorStatus.FAILED`` with ``error_code="circuit_open"`` WITHOUT touching
the rail; kernel policy (spec/02) keeps the PaymentIntent in PROCESSING — the
payment is never hard-failed by the breaker (ARCHITECTURE §(d) rule 6).

Retry rule (binding): retries reuse the SAME ``connector_ref``; ``attempt_no``
increments in ``connector_results``; transport-only retries take a bounded
exponential backoff through the injected sleep. A retry after a TIMED_OUT
submit is FORBIDDEN for financial messages — the runner must ``query_status``
first; only a definitive "not received" permits resubmission with the same
``connector_ref`` (``resubmit_after_timeout``). ``settle_after_timeout`` is
the status-poll-then-reverse path: at most 3 polls, then a reversal if the
rail outcome is still unresolved. The poll loop runs ``poll_attempts`` x
``poll_interval_ms`` through the injected sleep so tests fast-forward.
``asyncio.wait_for`` is injectable for the same reason.

Simulator-only enforcement (spec/16 LR-4 §API F invariant 2, runner layer):
a runner constructed with ``simulator_only=True`` (the SANDBOX_PUBLIC
deployment) refuses BOTH adapter registration for any non-SIMULATOR mode
(:class:`SimulatorOnlyViolationError`) AND dispatch/query/reverse/health to
any registration whose ``active_mode`` is not SIMULATOR — the call returns a
synthetic FAILED ``sandbox_isolation_violation`` result without ever touching
an adapter. This is defense-in-depth under the registry boot assert + sweep
(``bdpay.gateway.sandbox.SandboxPublicGuard``): even if a registry row flips
between sweeps, no live-mode connector is reachable through the only dispatch
path the kernel has.
"""

from __future__ import annotations

import asyncio

from bdpay.connectors.breaker import CircuitBreaker
from bdpay.connectors.idempotency import IdempotencyStore, InMemoryIdempotencyStore
from bdpay.connectors.ports import (
    EventSink,
    InMemoryEventSink,
    InMemoryObjectStore,
    ObjectStore,
    raw_object_key,
)
from bdpay.connectors.registry import (
    ConnectorMode,
    ConnectorRegistration,
    ConnectorRegistry,
    UnknownConnectorError,
)
from bdpay.connectors.sdk import ConnectorResult, ConnectorStatus, Money, PaymentInstruction
from bdpay.connectors.stores import (
    ConnectorResultRow,
    ConnectorResultStore,
    InMemoryConnectorResultStore,
)
from bdpay.platform.canonical import canonical_json, sha256_canonical
from bdpay.platform.clock import Clock
from bdpay.platform.errors import ConflictError

__all__ = [
    "ConnectorRunner",
    "MAX_TIMEOUT_SETTLE_POLLS",
    "RetryForbiddenError",
    "SANDBOX_ISOLATION_CODE",
    "SimulatorOnlyViolationError",
    "TRANSPORT_ERROR_CODE",
]

TRANSPORT_ERROR_CODE = "transport_error"
NOT_RECEIVED_CODE = "not_received"

#: Error code carried by every simulator-only refusal (spec/16 §API F inv 2).
SANDBOX_ISOLATION_CODE = "sandbox_isolation_violation"

#: Hard cap on status polls in the poll-then-reverse settlement path.
MAX_TIMEOUT_SETTLE_POLLS = 3

#: Bounded-backoff defaults for transport-only submit retries (overridable
#: per connector via retry_budget backoff_initial_ms / backoff_cap_ms).
DEFAULT_BACKOFF_INITIAL_MS = 200
DEFAULT_BACKOFF_CAP_MS = 5_000

_DEFINITIVE_STATUSES = frozenset(
    {
        ConnectorStatus.SUCCESS,
        ConnectorStatus.REJECTED,
        ConnectorStatus.REVERSED,
        ConnectorStatus.FAILED,
    }
)

#: Recorded outcomes that replay WITHOUT touching the rail again. FAILED and
#: TIMED_OUT are excluded: FAILED-retryable must reach the rail again via the
#: retry engine; TIMED_OUT is guarded by the query-first rule.
_REPLAY_SAFE_STATUSES = frozenset(
    {
        ConnectorStatus.SUCCESS,
        ConnectorStatus.PENDING,
        ConnectorStatus.REJECTED,
        ConnectorStatus.REVERSED,
    }
)


class RetryForbiddenError(ConflictError):
    """Resubmitting a financial message after a TIMED_OUT submit is forbidden."""

    default_code = "retry_after_timeout_forbidden"


class SimulatorOnlyViolationError(ConflictError):
    """A simulator-only runner refused a non-SIMULATOR adapter or mode."""

    default_code = SANDBOX_ISOLATION_CODE


class ConnectorRunner:
    def __init__(
        self,
        *,
        registry: ConnectorRegistry,
        breaker: CircuitBreaker,
        clock: Clock,
        result_store: ConnectorResultStore | None = None,
        object_store: ObjectStore | None = None,
        idempotency: IdempotencyStore | None = None,
        events: EventSink | None = None,
        sleep=None,
        wait_for=None,
        simulator_only: bool = False,
    ) -> None:
        self._registry = registry
        self._breaker = breaker
        self._clock = clock
        self._store = result_store if result_store is not None else InMemoryConnectorResultStore()
        self._objects = object_store if object_store is not None else InMemoryObjectStore()
        self._idem = idempotency if idempotency is not None else InMemoryIdempotencyStore()
        self._events = events if events is not None else InMemoryEventSink()
        self._sleep = sleep if sleep is not None else asyncio.sleep
        self._wait_for = wait_for if wait_for is not None else asyncio.wait_for
        self._simulator_only = bool(simulator_only)
        self._adapters: dict[tuple[str, str], object] = {}

    @property
    def result_store(self) -> ConnectorResultStore:
        return self._store

    @property
    def idempotency_store(self) -> IdempotencyStore:
        return self._idem

    @property
    def breaker(self) -> CircuitBreaker:
        return self._breaker

    @property
    def simulator_only(self) -> bool:
        return self._simulator_only

    def register_adapter(self, connector_id: str, mode: ConnectorMode | str, adapter) -> None:
        mode_value = mode.value if isinstance(mode, ConnectorMode) else str(mode)
        if self._simulator_only and mode_value != ConnectorMode.SIMULATOR.value:
            raise SimulatorOnlyViolationError(
                f"simulator-only runner refuses a {mode_value} adapter for "
                f"{connector_id!r}; only SIMULATOR adapters may exist in a "
                "SANDBOX_PUBLIC deployment (spec/16 invariant 2)"
            )
        self._adapters[(connector_id, mode_value)] = adapter

    def _simulator_only_refusal(
        self, registration: ConnectorRegistration, instruction_id: str, connector_ref: str
    ) -> ConnectorResult | None:
        """Synthetic FAILED refusal when a simulator-only runner is asked to
        reach a non-SIMULATOR registration — the rail is never touched."""
        if not self._simulator_only:
            return None
        if registration.active_mode is ConnectorMode.SIMULATOR:
            return None
        return self._synthetic(
            registration.connector_id,
            instruction_id,
            connector_ref,
            ConnectorStatus.FAILED,
            SANDBOX_ISOLATION_CODE,
        )

    def _adapter(self, registration: ConnectorRegistration):
        key = (registration.connector_id, registration.active_mode.value)
        adapter = self._adapters.get(key)
        if adapter is None:
            raise UnknownConnectorError(
                f"no adapter registered for {key[0]} in mode {key[1]}"
            )
        return adapter

    # -- synthetic results ----------------------------------------------------------

    def _now_iso(self) -> str:
        return self._clock.now().strftime("%Y-%m-%dT%H:%M:%SZ")

    def _synthetic(
        self,
        connector_id: str,
        instruction_id: str,
        connector_ref: str,
        status: ConnectorStatus,
        error_code: str,
    ) -> ConnectorResult:
        raw = {
            "synthetic": True,
            "connector_id": connector_id,
            "connector_ref": connector_ref,
            "status": status.value,
            "error_code": error_code,
        }
        raw_hash = sha256_canonical(raw)
        self._objects.put(raw_object_key(connector_id, raw_hash), canonical_json(raw))
        return ConnectorResult(
            instruction_id=instruction_id,
            connector_ref=connector_ref,
            status=status,
            rail_transaction_id=None,
            responded_at=self._now_iso(),
            error_code=error_code,
            raw_response_hash=raw_hash,
        )

    # -- recording -------------------------------------------------------------------

    def _record(
        self,
        registration: ConnectorRegistration,
        result: ConnectorResult,
        call_kind: str,
    ) -> ConnectorResultRow:
        row = self._store.record(
            result,
            connector_id=registration.connector_id,
            call_kind=call_kind,
            attempt_no=self._store.next_attempt_no(
                registration.connector_id, result.connector_ref, call_kind
            ),
            mode_at_call=registration.active_mode.value,
            raw_response_pointer=raw_object_key(
                registration.connector_id, result.raw_response_hash
            ),
            produced_at=self._clock.now(),
        )
        self._events.emit(
            "connector_result.recorded",
            {
                "result_id": row.result_id,
                "connector_id": row.connector_id,
                "connector_ref": row.connector_ref,
                "call_kind": row.call_kind,
                "status": row.status,
                "error_code": row.error_code,
                "mode_at_call": row.mode_at_call,
            },
        )
        return row

    # -- the spec dispatch algorithm ---------------------------------------------------

    async def dispatch(self, instruction: PaymentInstruction) -> ConnectorResult:
        reg = self._registry.get(instruction.rail)  # raises UnknownConnector
        recorded = self._idem.get(reg.connector_id, instruction.connector_ref, "SUBMIT")
        if recorded is not None and recorded.status in _REPLAY_SAFE_STATUSES:
            return recorded  # idempotent replay: no second rail touch
        if reg.active_mode is ConnectorMode.DISABLED or not reg.enabled:
            return self._synthetic(
                reg.connector_id,
                instruction.instruction_id,
                instruction.connector_ref,
                ConnectorStatus.FAILED,
                "connector_disabled",
            )
        refusal = self._simulator_only_refusal(
            reg, instruction.instruction_id, instruction.connector_ref
        )
        if refusal is not None:
            return refusal  # spec/16 inv 2: no rail touch, structurally unreachable
        if not await self._breaker.admit(reg.connector_id, kind="SUBMIT"):
            # Fail-closed: no rail touch; kernel keeps the intent PROCESSING.
            return self._synthetic(
                reg.connector_id,
                instruction.instruction_id,
                instruction.connector_ref,
                ConnectorStatus.FAILED,
                "circuit_open",
            )
        adapter = self._adapter(reg)
        # Write-ahead intent BEFORE the rail call: a crash after this point
        # leaves proof that this connector_ref may have reached the rail.
        self._idem.begin(
            reg.connector_id,
            instruction.connector_ref,
            "SUBMIT",
            instruction_id=instruction.instruction_id,
            recorded_at=self._clock.now(),
        )
        transport_error = False
        try:
            result = await self._wait_for(
                adapter.submit(instruction), timeout=reg.timeout_ms / 1000
            )
        except TimeoutError:
            result = self._synthetic(
                reg.connector_id,
                instruction.instruction_id,
                instruction.connector_ref,
                ConnectorStatus.TIMED_OUT,
                "hard_timeout",
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            transport_error = True
            result = self._synthetic(
                reg.connector_id,
                instruction.instruction_id,
                instruction.connector_ref,
                ConnectorStatus.FAILED,
                TRANSPORT_ERROR_CODE,
            )
        self._record(reg, result, "SUBMIT")
        if result.status in _REPLAY_SAFE_STATUSES:
            self._idem.put(reg.connector_id, instruction.connector_ref, "SUBMIT", result)
        await self._breaker.observe(
            reg.connector_id, result.status, transport_error=transport_error
        )
        return result

    # -- retry engine -----------------------------------------------------------------

    async def submit(self, instruction: PaymentInstruction) -> ConnectorResult:
        """Dispatch with the retry budget: transport-only retries, same ref,
        bounded exponential backoff through the injected sleep."""
        reg = self._registry.get(instruction.rail)
        last = self._store.last_for(
            reg.connector_id, instruction.connector_ref, "SUBMIT"
        )
        if last is not None and last.status == ConnectorStatus.TIMED_OUT.value:
            raise RetryForbiddenError(
                "submit after TIMED_OUT is forbidden for financial messages; "
                "use resubmit_after_timeout (query_status first)"
            )
        retries = int(reg.retry_budget.get("submit_retries", 0))
        backoff_ms = int(reg.retry_budget.get("backoff_initial_ms", DEFAULT_BACKOFF_INITIAL_MS))
        backoff_cap_ms = int(reg.retry_budget.get("backoff_cap_ms", DEFAULT_BACKOFF_CAP_MS))
        result = await self.dispatch(instruction)
        attempts = 0
        while (
            result.status is ConnectorStatus.FAILED
            and result.error_code == TRANSPORT_ERROR_CODE
            and attempts < retries
        ):
            attempts += 1
            await self._sleep(backoff_ms / 1000)
            backoff_ms = min(backoff_ms * 2, backoff_cap_ms)
            result = await self.dispatch(instruction)
        return result

    async def resubmit_after_timeout(self, instruction: PaymentInstruction) -> ConnectorResult:
        """The only legal resubmission path after a TIMED_OUT submit: query first.

        Polls ``query_status``; only a definitive not-received from the rail
        permits resubmission with the same ``connector_ref``; any other
        definitive answer is the recorded truth and is returned instead.
        """
        reg = self._registry.get(instruction.rail)
        last = self._store.last_for(reg.connector_id, instruction.connector_ref, "SUBMIT")
        if last is None or last.status != ConnectorStatus.TIMED_OUT.value:
            raise RetryForbiddenError(
                "resubmit_after_timeout requires a recorded TIMED_OUT submit"
            )
        polled = await self.poll_status(reg.connector_id, instruction.connector_ref)
        if (
            polled is not None
            and polled.status is ConnectorStatus.FAILED
            and polled.error_code == NOT_RECEIVED_CODE
        ):
            return await self.dispatch(instruction)
        if polled is None:
            return self._synthetic(
                reg.connector_id,
                instruction.instruction_id,
                instruction.connector_ref,
                ConnectorStatus.TIMED_OUT,
                "status_unresolved",
            )
        return polled

    async def settle_after_timeout(
        self, instruction: PaymentInstruction, *, reason: str = "timeout_reversal"
    ) -> ConnectorResult:
        """Status-poll-then-reverse after a TIMED_OUT submit (max 3 polls).

        Definitive poll answers win: SUCCESS/REJECTED/REVERSED is the recorded
        rail truth and is returned as-is; a definitive not-received means the
        rail never saw the instruction (nothing to reverse — kernel may then
        ``resubmit_after_timeout``). Only an UNRESOLVED outcome after the poll
        budget triggers the reversal, so money is never left in limbo.
        """
        reg = self._registry.get(instruction.rail)
        last = self._store.last_for(reg.connector_id, instruction.connector_ref, "SUBMIT")
        if last is None or last.status != ConnectorStatus.TIMED_OUT.value:
            raise RetryForbiddenError(
                "settle_after_timeout requires a recorded TIMED_OUT submit"
            )
        attempts = min(int(reg.retry_budget.get("poll_attempts", 3)), MAX_TIMEOUT_SETTLE_POLLS)
        interval_ms = int(reg.retry_budget.get("poll_interval_ms", 10_000))
        rail_transaction_id = last.rail_transaction_id
        for _ in range(attempts):
            await self._sleep(interval_ms / 1000)
            polled = await self.query_status(
                reg.connector_id, instruction.connector_ref, rail_transaction_id
            )
            if polled.status in (
                ConnectorStatus.SUCCESS,
                ConnectorStatus.REJECTED,
                ConnectorStatus.REVERSED,
            ):
                return polled
            if (
                polled.status is ConnectorStatus.FAILED
                and polled.error_code == NOT_RECEIVED_CODE
            ):
                return polled  # rail never received it; nothing to reverse
        return await self.reverse(
            reg.connector_id,
            instruction.connector_ref,
            rail_transaction_id,
            instruction.amount,
            reason,
        )

    # -- non-submit call paths -----------------------------------------------------------

    async def _call(
        self,
        connector_id: str,
        connector_ref: str,
        call_kind: str,
        breaker_kind: str,
        invoke,
    ) -> ConnectorResult:
        reg = self._registry.get(connector_id)
        if reg.active_mode is ConnectorMode.DISABLED or not reg.enabled:
            return self._synthetic(
                reg.connector_id, "", connector_ref, ConnectorStatus.FAILED, "connector_disabled"
            )
        refusal = self._simulator_only_refusal(reg, "", connector_ref)
        if refusal is not None:
            return refusal  # spec/16 inv 2: no rail touch
        if not await self._breaker.admit(reg.connector_id, kind=breaker_kind):
            return self._synthetic(
                reg.connector_id, "", connector_ref, ConnectorStatus.FAILED, "circuit_open"
            )
        adapter = self._adapter(reg)
        transport_error = False
        try:
            result = await self._wait_for(invoke(adapter), timeout=reg.timeout_ms / 1000)
        except TimeoutError:
            result = self._synthetic(
                reg.connector_id, "", connector_ref, ConnectorStatus.TIMED_OUT, "hard_timeout"
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            transport_error = True
            result = self._synthetic(
                reg.connector_id, "", connector_ref, ConnectorStatus.FAILED, TRANSPORT_ERROR_CODE
            )
        self._record(reg, result, call_kind)
        await self._breaker.observe(
            reg.connector_id, result.status, transport_error=transport_error
        )
        return result

    async def query_status(
        self,
        connector_id: str,
        connector_ref: str,
        rail_transaction_id: str | None = None,
    ) -> ConnectorResult:
        return await self._call(
            connector_id,
            connector_ref,
            "QUERY_STATUS",
            "QUERY_STATUS",
            lambda adapter: adapter.query_status(connector_ref, rail_transaction_id),
        )

    async def reverse(
        self,
        connector_id: str,
        connector_ref: str,
        rail_transaction_id: str | None,
        reverse_amount: Money,
        reason: str,
    ) -> ConnectorResult:
        return await self._call(
            connector_id,
            connector_ref,
            "REVERSE",
            "REVERSE",
            lambda adapter: adapter.reverse(
                connector_ref, rail_transaction_id, reverse_amount, reason
            ),
        )

    async def poll_status(
        self,
        connector_id: str,
        connector_ref: str,
        rail_transaction_id: str | None = None,
    ) -> ConnectorResult | None:
        """``poll_attempts`` x ``poll_interval_ms`` via the injected sleep."""
        reg = self._registry.get(connector_id)
        attempts = int(reg.retry_budget.get("poll_attempts", 3))
        interval_ms = int(reg.retry_budget.get("poll_interval_ms", 10_000))
        last: ConnectorResult | None = None
        for _ in range(attempts):
            await self._sleep(interval_ms / 1000)
            last = await self.query_status(connector_id, connector_ref, rail_transaction_id)
            if last.status in _DEFINITIVE_STATUSES and last.error_code != TRANSPORT_ERROR_CODE:
                return last
        return last

    # -- health sweep --------------------------------------------------------------------

    async def health_check(self, connector_id: str) -> bool:
        """One health sweep step: probe, sample, breaker observation.

        Disabled connectors and breaker-denied probes record an UNHEALTHY
        sample without touching the rail (unknowable health is not healthy).
        """
        reg = self._registry.get(connector_id)
        if reg.active_mode is ConnectorMode.DISABLED or not reg.enabled:
            self._registry.record_health(
                connector_id, healthy=False, detail_code="connector_disabled"
            )
            return False
        if self._simulator_only and reg.active_mode is not ConnectorMode.SIMULATOR:
            # spec/16 inv 2: an unreachable connector's health is unknowable.
            self._registry.record_health(
                connector_id, healthy=False, detail_code=SANDBOX_ISOLATION_CODE
            )
            return False
        if not await self._breaker.admit(reg.connector_id, kind="HEALTH"):
            self._registry.record_health(
                connector_id, healthy=False, detail_code="circuit_open"
            )
            return False
        adapter = self._adapter(reg)
        started = self._clock.now()
        detail_code: str | None = None
        transport_error = False
        try:
            healthy = bool(
                await self._wait_for(adapter.health_check(), timeout=reg.timeout_ms / 1000)
            )
            if not healthy:
                detail_code = "unhealthy_response"
        except TimeoutError:
            healthy = False
            detail_code = "health_timeout"
        except asyncio.CancelledError:
            raise
        except Exception:
            healthy = False
            transport_error = True
            detail_code = TRANSPORT_ERROR_CODE
        latency_ms = int((self._clock.now() - started).total_seconds() * 1000)
        self._registry.record_health(
            connector_id, healthy=healthy, latency_ms=latency_ms, detail_code=detail_code
        )
        await self._breaker.observe(
            reg.connector_id,
            ConnectorStatus.SUCCESS if healthy else ConnectorStatus.FAILED,
            transport_error=transport_error,
        )
        return healthy

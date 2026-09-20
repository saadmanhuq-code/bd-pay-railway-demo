"""Deterministic simulator scenario engine + SimulatorConnector (spec/10 §7).

The SIMULATOR-mode substitute for live rails: no randomness, all branching
from ``sha256(connector_ref)``, delays via the injected clock/sleep, idempotent
replay per ``connector_ref`` returns the recorded outcome. Callbacks are
emitted through the REAL ingestion pipeline, HMAC-signed with the simulator
key registered as the connector's SIMULATOR-mode ``webhook_verify_ref``.
``SimulatorConnector`` implements the frozen ``sdk.PaymentConnector`` protocol
so rails/MFS teams can wrap it per connector.

Rail truth + effect are recorded at the moment a ``respond`` step executes
(SPEC_ERRATA-LANE-B LB4): a script that responds and THEN times out models a
rail that processed the instruction but lost the reply in transit, so a later
``query_status`` finds the recorded truth and a re-submit with the same
``connector_ref`` is deduped rail-side (recorded truth returned, no second
effect).

``SimulatorBatchConnector`` is the file-rail (``SettlementFileConnector``)
simulator: deterministic PARTIAL-batch acceptance branching on
``sha256(entry_ref)`` buckets, idempotent on ``batch_id``
(SPEC_ERRATA-LANE-B LB3).
"""

from __future__ import annotations

import asyncio
import hmac
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256

from bdpay.connectors.idempotency import IdempotencyStore, InMemoryIdempotencyStore
from bdpay.connectors.ports import InMemoryObjectStore, ObjectStore, raw_object_key
from bdpay.connectors.scenarios import (
    SimulatorScenario,
    ref_hash,
    select_scenario,
)
from bdpay.connectors.sdk import ConnectorResult, ConnectorStatus, Money, PaymentInstruction
from bdpay.connectors.webhooks import normalize_numeric_fields
from bdpay.platform.canonical import canonical_json, sha256_canonical
from bdpay.platform.clock import Clock

__all__ = [
    "ScenarioEngine",
    "SimulatorBatchConnector",
    "SimulatorConnector",
    "SimulatorWebhookHandler",
    "sign_callback",
]

SIGNATURE_HEADER = "x-sim-signature"
TIMESTAMP_HEADER = "x-sim-timestamp"

_NUMERIC_CALLBACK_FIELDS = ("amount_minor", "responded_at", "rail_transaction_id")


def _rfc3339(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def sign_callback(key: bytes, body: bytes, timestamp: str) -> dict:
    """Headers for a simulator-signed callback (HMAC-SHA256 over ts + body)."""
    mac = hmac.new(key, timestamp.encode("utf-8") + b"." + body, sha256).hexdigest()
    return {SIGNATURE_HEADER: mac, TIMESTAMP_HEADER: timestamp}


class SimulatorWebhookHandler:
    """``ConnectorWebhookHandler`` for simulator callbacks — fail-closed."""

    def __init__(
        self, connector_id: str, key: bytes, *, clock: Clock, max_skew_s: int = 300
    ) -> None:
        self.connector_id = connector_id
        self._key = key
        self._clock = clock
        self._max_skew_s = max_skew_s

    def verify_signature(self, headers: dict, body: bytes) -> bool:
        signature = headers.get(SIGNATURE_HEADER)
        timestamp = headers.get(TIMESTAMP_HEADER)
        if not signature or not timestamp:
            return False
        try:
            stamped = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except ValueError:
            return False
        if stamped.tzinfo is None:
            return False
        skew = abs((self._clock.now() - stamped).total_seconds())
        if skew > self._max_skew_s:
            return False
        expected = hmac.new(
            self._key, timestamp.encode("utf-8") + b"." + body, sha256
        ).hexdigest()
        return hmac.compare_digest(expected, signature)

    def parse_event(self, headers: dict, body: bytes) -> ConnectorResult:
        payload = json.loads(body.decode("utf-8"))
        normalized = normalize_numeric_fields(payload, _NUMERIC_CALLBACK_FIELDS)
        return ConnectorResult(
            instruction_id=str(normalized["instruction_id"]),
            connector_ref=str(normalized["connector_ref"]),
            status=ConnectorStatus(normalized["status"]),
            rail_transaction_id=normalized.get("rail_transaction_id"),
            responded_at=normalized.get("responded_at"),
            error_code=normalized.get("error_code"),
            raw_response_hash=sha256_canonical(payload),  # over the raw, pre-normalization form
        )


@dataclass
class _DueCallback:
    due_at: datetime
    sequence: int
    headers: dict
    body: bytes


class ScenarioEngine:
    """Executes scenario scripts once per unique ``connector_ref``."""

    def __init__(
        self,
        connector_id: str,
        scenarios: list[SimulatorScenario],
        *,
        clock: Clock,
        signing_key: bytes,
        object_store: ObjectStore | None = None,
        idempotency: IdempotencyStore | None = None,
        sleep=None,
        callback_sink=None,
    ) -> None:
        self.connector_id = connector_id
        self._scenarios = list(scenarios)
        self._clock = clock
        self._key = signing_key
        self._objects = object_store if object_store is not None else InMemoryObjectStore()
        self._idem = idempotency if idempotency is not None else InMemoryIdempotencyStore()
        self._sleep = sleep if sleep is not None else asyncio.sleep
        self._callback_sink = callback_sink
        self._attempts: dict[str, int] = {}
        self._effects: dict[str, int] = {}
        self._instructions: dict[str, PaymentInstruction] = {}
        self._reversal_status: dict[str, str] = {}
        self._truth: dict[str, ConnectorResult] = {}
        self._queue: list[_DueCallback] = []
        self._sequence = 0

    # -- wiring ------------------------------------------------------------------

    def bind_callback_sink(self, sink) -> None:
        """``sink(connector_id, headers, body)`` — usually ``pipeline.ingest``."""
        self._callback_sink = sink

    @property
    def scenarios(self) -> list[SimulatorScenario]:
        return list(self._scenarios)

    @property
    def object_store(self) -> ObjectStore:
        return self._objects

    def effect_count(self, connector_ref: str) -> int:
        """Rail-side effects recorded for CERT-01 (exactly one per unique ref)."""
        return self._effects.get(connector_ref, 0)

    def error_taxonomy(self) -> frozenset[str]:
        codes = {"not_received"}
        for scenario in self._scenarios:
            for step in scenario.script:
                code = step.get("error_code")
                if code:
                    codes.add(code)
        return frozenset(codes)

    # -- internals -----------------------------------------------------------------

    @staticmethod
    async def _never() -> None:
        await asyncio.Event().wait()

    def _template(self, value: str | None, connector_ref: str) -> str | None:
        if value is None:
            return None
        return value.replace("{ref_hash8}", ref_hash(connector_ref)[:8])

    def _archive(self, raw: dict) -> str:
        raw_hash = sha256_canonical(raw)
        self._objects.put(raw_object_key(self.connector_id, raw_hash), canonical_json(raw))
        return raw_hash

    def _result_from_raw(self, instruction_id: str, connector_ref: str, raw: dict):
        return ConnectorResult(
            instruction_id=instruction_id,
            connector_ref=connector_ref,
            status=ConnectorStatus(raw["status"]),
            rail_transaction_id=raw.get("rail_transaction_id"),
            responded_at=raw.get("responded_at"),
            error_code=raw.get("error_code"),
            raw_response_hash=self._archive(raw),
        )

    def _enqueue_callback(self, headers: dict, body: bytes, due_at: datetime) -> _DueCallback:
        self._sequence += 1
        due = _DueCallback(due_at=due_at, sequence=self._sequence, headers=headers, body=body)
        self._queue.append(due)
        return due

    def _build_callback(self, instruction: PaymentInstruction, step: dict) -> _DueCallback:
        ref = instruction.connector_ref
        due_at = self._clock.now() + timedelta(milliseconds=int(step.get("after_ms", 0)))
        payload = {
            "instruction_id": instruction.instruction_id,
            "connector_ref": ref,
            "status": step["status"],
            "rail_transaction_id": self._template(step.get("rail_transaction_id"), ref),
            "responded_at": _rfc3339(due_at),
            "error_code": step.get("error_code"),
        }
        body = canonical_json(payload)
        headers = (
            sign_callback(self._key, body, _rfc3339(due_at))
            if step.get("sign", True)
            else {TIMESTAMP_HEADER: _rfc3339(due_at)}
        )
        return self._enqueue_callback(headers, body, due_at)

    # -- PaymentConnector-facing operations ------------------------------------------

    async def submit(self, instruction: PaymentInstruction) -> ConnectorResult:
        ref = instruction.connector_ref
        recorded = self._idem.get(self.connector_id, ref, "SUBMIT")
        if recorded is not None:
            return recorded  # idempotent replay; effect count unchanged
        truth = self._truth.get(ref)
        if truth is not None:
            # Rail-side dedupe: the rail already processed this ref (the
            # response was lost in transit) — return the recorded truth
            # without a second effect (LB4).
            return self._idem.put(self.connector_id, ref, "SUBMIT", truth)
        self._attempts[ref] = self._attempts.get(ref, 0) + 1
        self._instructions[ref] = instruction
        scenario = select_scenario(self._scenarios, instruction)
        result: ConnectorResult | None = None
        last_callback: _DueCallback | None = None
        for step in scenario.script:
            kind = step["step"]
            if kind == "drop_then_succeed":
                if self._attempts[ref] < int(step["on_attempt"]):
                    raise ConnectionError("simulated transport drop (drop_then_succeed)")
            elif kind == "timeout":
                await self._never()
            elif kind == "respond":
                await self._sleep(int(step.get("delay_ms", 0)) / 1000)
                raw = {
                    "connector_id": self.connector_id,
                    "connector_ref": ref,
                    "instruction_id": instruction.instruction_id,
                    "status": step["status"],
                    "rail_transaction_id": self._template(step.get("rail_transaction_id"), ref),
                    "error_code": step.get("error_code"),
                    "amount_minor": instruction.amount.amount_minor,
                    "responded_at": _rfc3339(self._clock.now()),
                }
                result = self._result_from_raw(instruction.instruction_id, ref, raw)
                # Rail truth + effect exist the moment the rail responds —
                # even if a later step loses the reply in transit (LB4).
                self._effects[ref] = self._effects.get(ref, 0) + 1
                self._truth[ref] = result
            elif kind == "callback":
                last_callback = self._build_callback(instruction, step)
            elif kind == "duplicate_callback":
                if last_callback is None:
                    raise ValueError("duplicate_callback requires a preceding callback step")
                due_at = self._clock.now() + timedelta(milliseconds=int(step.get("after_ms", 0)))
                for _ in range(int(step.get("count", 1))):
                    self._enqueue_callback(last_callback.headers, last_callback.body, due_at)
            elif kind == "reversal_ack":
                self._reversal_status[ref] = step.get("status", "reversed")
            else:
                raise ValueError(f"unknown script step {kind!r} (closed DSL)")
        if result is None:
            raise ValueError(f"scenario {scenario.name!r} produced no synchronous respond step")
        return self._idem.put(self.connector_id, ref, "SUBMIT", result)

    async def query_status(
        self, connector_ref: str, rail_transaction_id: str | None
    ) -> ConnectorResult:
        truth = self._truth.get(connector_ref)
        if truth is None:
            raw = {
                "connector_id": self.connector_id,
                "connector_ref": connector_ref,
                "instruction_id": "",
                "status": "failed",
                "rail_transaction_id": None,
                "error_code": "not_received",
                "responded_at": _rfc3339(self._clock.now()),
            }
            return self._result_from_raw("", connector_ref, raw)
        raw = {
            "connector_id": self.connector_id,
            "connector_ref": connector_ref,
            "instruction_id": truth.instruction_id,
            "status": truth.status.value,
            "rail_transaction_id": truth.rail_transaction_id,
            "error_code": truth.error_code,
            "responded_at": _rfc3339(self._clock.now()),
            "query": True,
        }
        return self._result_from_raw(truth.instruction_id, connector_ref, raw)

    async def reverse(
        self,
        connector_ref: str,
        rail_transaction_id: str | None,
        reverse_amount: Money,
        reason: str,
    ) -> ConnectorResult:
        recorded = self._idem.get(self.connector_id, connector_ref, "REVERSE")
        if recorded is not None:
            return recorded
        status = self._reversal_status.get(connector_ref, "reversed")
        truth = self._truth.get(connector_ref)
        instruction_id = truth.instruction_id if truth is not None else ""
        raw = {
            "connector_id": self.connector_id,
            "connector_ref": connector_ref,
            "instruction_id": instruction_id,
            "status": status,
            "rail_transaction_id": rail_transaction_id
            or (truth.rail_transaction_id if truth else None),
            "error_code": None,
            "reverse_amount_minor": reverse_amount.amount_minor,
            "reason": reason,
            "responded_at": _rfc3339(self._clock.now()),
        }
        result = self._result_from_raw(instruction_id, connector_ref, raw)
        if result.status is ConnectorStatus.REVERSED:
            self._truth[connector_ref] = result
        return self._idem.put(self.connector_id, connector_ref, "REVERSE", result)

    async def health_check(self) -> bool:
        return True

    # -- callback delivery ------------------------------------------------------------

    async def deliver_due_callbacks(self) -> int:
        """Deliver queued callbacks with ``due_at <= clock.now()`` via the sink."""
        if self._callback_sink is None:
            raise RuntimeError("no callback sink bound; call bind_callback_sink(pipeline.ingest)")
        now = self._clock.now()
        due = sorted(
            (cb for cb in self._queue if cb.due_at <= now),
            key=lambda cb: (cb.due_at, cb.sequence),
        )
        for callback in due:
            self._queue.remove(callback)
            await self._callback_sink(self.connector_id, callback.headers, callback.body)
        return len(due)

    def pending_callbacks(self) -> int:
        return len(self._queue)


class SimulatorConnector:
    """Frozen ``sdk.PaymentConnector`` implementation driven by the engine."""

    def __init__(
        self,
        connector_id: str,
        engine: ScenarioEngine,
        *,
        supported_methods: tuple[str, ...] = ("BKASH",),
    ) -> None:
        self.connector_id = connector_id
        self.supported_methods = supported_methods
        self._engine = engine

    @property
    def engine(self) -> ScenarioEngine:
        return self._engine

    async def submit(self, instruction: PaymentInstruction) -> ConnectorResult:
        return await self._engine.submit(instruction)

    async def query_status(
        self, connector_ref: str, rail_transaction_id: str | None
    ) -> ConnectorResult:
        return await self._engine.query_status(connector_ref, rail_transaction_id)

    async def reverse(
        self,
        connector_ref: str,
        rail_transaction_id: str | None,
        reverse_amount: Money,
        reason: str,
    ) -> ConnectorResult:
        return await self._engine.reverse(
            connector_ref, rail_transaction_id, reverse_amount, reason
        )

    async def health_check(self) -> bool:
        return await self._engine.health_check()


class SimulatorBatchConnector:
    """Deterministic ``SettlementFileConnector`` simulator for file rails.

    Partial-batch acceptance (LB3): each entry MUST carry a non-empty string
    ``entry_ref``; an entry is rejected when ``sha256(entry_ref)`` falls in a
    reject bucket (default bucket 7 of 10) or when the entry carries
    ``"sim_reject": "true"``. No randomness anywhere — acceptance is a pure
    function of the entry refs. Idempotent on ``batch_id``: re-submitting the
    same batch id returns the recorded outcome with exactly one rail-side
    effect. ``batch_id`` and ``session`` are echoed unchanged; every outcome
    carries ``raw_response_hash`` over the archived canonical raw response.
    """

    def __init__(
        self,
        connector_id: str,
        *,
        clock: Clock,
        object_store: ObjectStore | None = None,
        buckets: int = 10,
        reject_buckets: frozenset[int] = frozenset({7}),
    ) -> None:
        if buckets < 1:
            raise ValueError("buckets must be >= 1")
        self.connector_id = connector_id
        self._clock = clock
        self._objects = object_store if object_store is not None else InMemoryObjectStore()
        self._buckets = buckets
        self._reject_buckets = frozenset(reject_buckets)
        self._recorded: dict[str, dict] = {}
        self._effects: dict[str, int] = {}

    @property
    def object_store(self) -> ObjectStore:
        return self._objects

    def effect_count(self, batch_id: str) -> int:
        """Rail-side effects per batch_id (exactly one per unique batch)."""
        return self._effects.get(batch_id, 0)

    def entry_rejected(self, entry: dict) -> bool:
        """Deterministic accept/reject verdict for one batch entry."""
        ref = entry.get("entry_ref")
        if not isinstance(ref, str) or not ref:
            raise ValueError("batch entry requires a non-empty string 'entry_ref'")
        if entry.get("sim_reject") == "true":
            return True
        return int(ref_hash(ref), 16) % self._buckets in self._reject_buckets

    def _archive(self, raw: dict) -> str:
        raw_hash = sha256_canonical(raw)
        self._objects.put(raw_object_key(self.connector_id, raw_hash), canonical_json(raw))
        return raw_hash

    async def submit_batch(
        self, batch_id: str, session: str, entries: list[dict], effective_date: str
    ) -> dict:
        recorded = self._recorded.get(batch_id)
        if recorded is not None:
            return recorded  # idempotent replay; effect count unchanged
        accepted_refs: list[str] = []
        rejected: list[dict] = []
        for entry in entries:
            if self.entry_rejected(entry):
                rejected.append(
                    {"entry_ref": entry["entry_ref"], "error_code": "sim_entry_rejected"}
                )
            else:
                accepted_refs.append(entry["entry_ref"])
        if not rejected:
            status = "ACCEPTED"
        elif not accepted_refs:
            status = "REJECTED"
        else:
            status = "PARTIAL"
        raw = {
            "connector_id": self.connector_id,
            "batch_id": batch_id,
            "session": session,
            "effective_date": effective_date,
            "status": status,
            "accepted_count": len(accepted_refs),
            "accepted_refs": accepted_refs,
            "rejected": rejected,
            "responded_at": _rfc3339(self._clock.now()),
        }
        outcome = {
            "batch_id": batch_id,
            "session": session,
            "effective_date": effective_date,
            "status": status,
            "accepted_count": len(accepted_refs),
            "accepted_refs": accepted_refs,
            "rejected": rejected,
            "responded_at": raw["responded_at"],
            "raw_response_hash": self._archive(raw),
        }
        self._effects[batch_id] = self._effects.get(batch_id, 0) + 1
        self._recorded[batch_id] = outcome
        return outcome

    async def query_batch_status(self, batch_id: str) -> dict:
        recorded = self._recorded.get(batch_id)
        if recorded is not None:
            return recorded
        raw = {
            "connector_id": self.connector_id,
            "batch_id": batch_id,
            "status": "NOT_RECEIVED",
            "responded_at": _rfc3339(self._clock.now()),
        }
        return {
            "batch_id": batch_id,
            "status": "NOT_RECEIVED",
            "responded_at": raw["responded_at"],
            "raw_response_hash": self._archive(raw),
        }

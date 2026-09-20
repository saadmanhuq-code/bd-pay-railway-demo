"""Smart routing models and repositories (spec/17 VX2).

The module keeps VX2 policy/decision storage beside the routing engine while
following the repo's repository pattern: deterministic in-memory stores for
unit tests and psycopg3 stores matching the module migrations.  Routing
weights are data, never code constants; invalid policy data fails closed.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable

from bdpay.connectors.sdk import ConnectorResult
from bdpay.platform.canonical import canonical_json, sha256_canonical
from bdpay.platform.errors import ConflictError, InvalidRequestError
from bdpay.platform.ids import HASH_LENGTH, PREFIXES, IdPrefixError, make_id
from bdpay.platform.money import Money

__all__ = [
    "InMemoryRoutingPolicyStore",
    "InMemoryRoutingSignalStore",
    "PostgresRoutingPolicyStore",
    "PostgresRoutingSignalStore",
    "RoutingCandidateScore",
    "RoutingCandidateSignal",
    "RoutingDecisionRecord",
    "RoutingPolicyRecord",
    "RoutingPolicyStore",
    "RoutingSignalStore",
    "RoutingWeights",
    "make_routing_id",
    "make_routing_policy",
    "make_routing_policy_from_config",
]

ROUTING_PREFIXES: frozenset[str] = frozenset({"rpol", "rdec"})
POLICY_STATUSES: frozenset[str] = frozenset({"ACTIVE", "SUPERSEDED"})


def make_routing_id(prefix: str, payload: dict) -> str:
    """Content-addressed ID helper for spec/17 routing prefixes.

    The platform prefix registry has not yet folded in ``rpol``/``rdec``. This
    helper uses the same canonical bytes and hash length as ``make_id`` so the
    IDs collapse to the platform helper when the registry is extended.
    """

    if prefix in PREFIXES:
        return make_id(prefix, payload)
    if prefix not in ROUTING_PREFIXES:
        raise IdPrefixError(
            f"unknown routing id prefix {prefix!r}; allowed are {sorted(ROUTING_PREFIXES)}"
        )
    if not isinstance(payload, dict):
        raise TypeError(f"payload must be dict, got {type(payload).__name__}")
    return f"{prefix}_{sha256_canonical(payload)[:HASH_LENGTH]}"


def _require_bps(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidRequestError(f"{name} must be int basis points")
    if not 0 <= value <= 10_000:
        raise InvalidRequestError(f"{name} must be between 0 and 10000")
    return value


def _require_positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise InvalidRequestError(f"{name} must be a positive integer")
    return value


def _require_non_negative_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise InvalidRequestError(f"{name} must be a non-negative integer")
    return value


def _require_aware(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise InvalidRequestError(f"{name} must be a timezone-aware datetime")
    return value


def _require_non_empty(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise InvalidRequestError(f"{name} must be a non-empty string")
    return value


@dataclass(frozen=True, slots=True)
class RoutingWeights:
    """Integer-bps policy weights; the four weights must sum to 10000."""

    success_bps: int
    health_bps: int
    fee_bps: int
    latency_bps: int

    def __post_init__(self) -> None:
        success = _require_bps(self.success_bps, "success_bps")
        health = _require_bps(self.health_bps, "health_bps")
        fee = _require_bps(self.fee_bps, "fee_bps")
        latency = _require_bps(self.latency_bps, "latency_bps")
        if success + health + fee + latency != 10_000:
            raise InvalidRequestError(
                "routing weights must sum to exactly 10000 basis points",
                code="routing_weights_invalid",
            )

    def as_payload(self) -> dict[str, int]:
        return {
            "success_bps": self.success_bps,
            "health_bps": self.health_bps,
            "fee_bps": self.fee_bps,
            "latency_bps": self.latency_bps,
        }


@dataclass(frozen=True, slots=True)
class RoutingPolicyRecord:
    """One routing_policies row."""

    policy_id: str
    method: str
    weights: RoutingWeights
    version: int
    approval_request_id: str
    created_at: datetime
    window_hours: int = 24
    status: str = "ACTIVE"
    schema_version: int = 1

    def __post_init__(self) -> None:
        _require_non_empty(self.policy_id, "policy_id")
        _require_non_empty(self.method, "method")
        if not isinstance(self.weights, RoutingWeights):
            raise InvalidRequestError("weights must be a RoutingWeights instance")
        _require_positive_int(self.version, "version")
        _require_non_empty(self.approval_request_id, "approval_request_id")
        _require_aware(self.created_at, "created_at")
        if self.status not in POLICY_STATUSES:
            raise InvalidRequestError(f"unknown routing policy status {self.status!r}")
        if (
            isinstance(self.window_hours, bool)
            or not isinstance(self.window_hours, int)
            or not 1 <= self.window_hours <= 168
        ):
            raise InvalidRequestError("window_hours must be between 1 and 168")
        _require_positive_int(self.schema_version, "schema_version")


def make_routing_policy(
    *,
    method: str,
    weights: RoutingWeights,
    version: int,
    approval_request_id: str,
    created_at: datetime,
    window_hours: int = 24,
) -> RoutingPolicyRecord:
    """Build a deterministic ACTIVE policy record from config/admin inputs."""

    _require_aware(created_at, "created_at")
    policy_id = make_routing_id(
        "rpol",
        {
            "method": method,
            "weights": weights.as_payload(),
            "version": version,
            "created_at": created_at,
        },
    )
    return RoutingPolicyRecord(
        policy_id=policy_id,
        method=method,
        weights=weights,
        version=version,
        approval_request_id=approval_request_id,
        created_at=created_at,
        window_hours=window_hours,
    )


def make_routing_policy_from_config(
    method: str,
    config: Mapping[str, str],
    *,
    version: int,
    approval_request_id: str,
    created_at: datetime,
) -> RoutingPolicyRecord | None:
    """Create a policy from platform-style config keys.

    Keys are method-scoped, for example ``SMART_ROUTING_BKASH_ENABLED`` and
    ``SMART_ROUTING_BKASH_W_SUCCESS_BPS``.  Missing or false enabled config
    returns ``None``; weights have no defaults and must be supplied by config.
    """

    safe_method = method.upper().replace("-", "_")
    enabled = config.get(f"SMART_ROUTING_{safe_method}_ENABLED", "").strip().lower()
    if enabled not in {"1", "true", "yes", "on"}:
        return None

    def required_int(suffix: str) -> int:
        key = f"SMART_ROUTING_{safe_method}_{suffix}"
        raw = config.get(key)
        if raw is None:
            raise InvalidRequestError(f"{key} is required when smart routing is enabled")
        try:
            return int(raw.strip(), 10)
        except ValueError as exc:
            raise InvalidRequestError(f"{key} must be an integer") from exc

    return make_routing_policy(
        method=method,
        weights=RoutingWeights(
            success_bps=required_int("W_SUCCESS_BPS"),
            health_bps=required_int("W_HEALTH_BPS"),
            fee_bps=required_int("W_FEE_BPS"),
            latency_bps=required_int("W_LATENCY_BPS"),
        ),
        version=version,
        approval_request_id=approval_request_id,
        created_at=created_at,
        window_hours=required_int("WINDOW_HOURS"),
    )


@dataclass(frozen=True, slots=True)
class RoutingCandidateSignal:
    """Input signals for one connector inside a policy window."""

    connector_id: str
    success_rate_bps: int
    health_bps: int
    fee_minor: int
    latency_ms: int
    observed_at: datetime

    def __post_init__(self) -> None:
        _require_non_empty(self.connector_id, "connector_id")
        _require_bps(self.success_rate_bps, "success_rate_bps")
        _require_bps(self.health_bps, "health_bps")
        _require_non_negative_int(self.fee_minor, "fee_minor")
        _require_non_negative_int(self.latency_ms, "latency_ms")
        _require_aware(self.observed_at, "observed_at")

    def as_snapshot(self) -> dict[str, object]:
        return {
            "connector_id": self.connector_id,
            "success_rate_bps": self.success_rate_bps,
            "health_bps": self.health_bps,
            "fee_minor": self.fee_minor,
            "latency_ms": self.latency_ms,
            "observed_at": self.observed_at,
        }


@dataclass(frozen=True, slots=True)
class RoutingCandidateScore:
    """Output score for one ranked connector."""

    connector_id: str
    score_bps: int
    success_rate_bps: int
    health_bps: int
    fee_minor: int
    latency_ms: int
    fee_rank_bps: int
    latency_rank_bps: int

    def __post_init__(self) -> None:
        _require_non_empty(self.connector_id, "connector_id")
        _require_bps(self.score_bps, "score_bps")
        _require_bps(self.success_rate_bps, "success_rate_bps")
        _require_bps(self.health_bps, "health_bps")
        _require_non_negative_int(self.fee_minor, "fee_minor")
        _require_non_negative_int(self.latency_ms, "latency_ms")
        _require_bps(self.fee_rank_bps, "fee_rank_bps")
        _require_bps(self.latency_rank_bps, "latency_rank_bps")

    def as_json(self) -> dict[str, int | str]:
        return {
            "connector_id": self.connector_id,
            "score_bps": self.score_bps,
            "success_rate_bps": self.success_rate_bps,
            "health_bps": self.health_bps,
            "fee_minor": self.fee_minor,
            "latency_ms": self.latency_ms,
            "fee_rank_bps": self.fee_rank_bps,
            "latency_rank_bps": self.latency_rank_bps,
        }


@dataclass(frozen=True, slots=True)
class RoutingDecisionRecord:
    """One routing_decisions row."""

    decision_id: str
    payment_attempt_id: str
    policy_id: str
    candidates: tuple[RoutingCandidateScore, ...]
    selected_connector_id: str
    inputs_hash: str
    decided_at: datetime
    fallback: bool = False
    fallback_reason: str | None = None
    schema_version: int = 1

    def __post_init__(self) -> None:
        _require_non_empty(self.decision_id, "decision_id")
        _require_non_empty(self.payment_attempt_id, "payment_attempt_id")
        _require_non_empty(self.policy_id, "policy_id")
        if not self.candidates:
            raise InvalidRequestError("routing decision needs at least one candidate")
        if self.selected_connector_id not in {c.connector_id for c in self.candidates}:
            raise InvalidRequestError("selected_connector_id must be in candidates")
        _require_non_empty(self.inputs_hash, "inputs_hash")
        _require_aware(self.decided_at, "decided_at")
        if self.fallback and not self.fallback_reason:
            raise InvalidRequestError("fallback decisions need fallback_reason")
        _require_positive_int(self.schema_version, "schema_version")

    def candidates_json(self) -> list[dict[str, int | str]]:
        return [candidate.as_json() for candidate in self.candidates]


def make_routing_decision(
    *,
    payment_attempt_id: str,
    policy: RoutingPolicyRecord,
    candidates: tuple[RoutingCandidateScore, ...],
    selected_connector_id: str,
    inputs_snapshot: Mapping[str, object],
    decided_at: datetime,
    fallback: bool = False,
    fallback_reason: str | None = None,
) -> RoutingDecisionRecord:
    inputs_hash = sha256_canonical(dict(inputs_snapshot))
    decision_id = make_routing_id(
        "rdec",
        {
            "payment_attempt_id": payment_attempt_id,
            "policy_id": policy.policy_id,
            "inputs_hash": inputs_hash,
        },
    )
    return RoutingDecisionRecord(
        decision_id=decision_id,
        payment_attempt_id=payment_attempt_id,
        policy_id=policy.policy_id,
        candidates=candidates,
        selected_connector_id=selected_connector_id,
        inputs_hash=inputs_hash,
        decided_at=decided_at,
        fallback=fallback,
        fallback_reason=fallback_reason,
    )


@runtime_checkable
class RoutingPolicyStore(Protocol):
    """Repository for routing policy history and decision records."""

    def activate_policy(
        self, record: RoutingPolicyRecord, *, conn: Any | None = None
    ) -> None: ...

    def get_active_policy(
        self, method: str, *, conn: Any | None = None
    ) -> RoutingPolicyRecord | None: ...

    def record_decision(
        self, record: RoutingDecisionRecord, *, conn: Any | None = None
    ) -> None: ...

    def get_decision(
        self, payment_attempt_id: str, *, conn: Any | None = None
    ) -> RoutingDecisionRecord | None: ...


@runtime_checkable
class RoutingSignalStore(Protocol):
    """Read signals derived from connector results, health samples, and fee config."""

    def signal_for(
        self,
        connector_id: str,
        *,
        method: str,
        amount_minor: int,
        window_start: datetime,
        now: datetime,
        conn: Any | None = None,
    ) -> RoutingCandidateSignal | None: ...


class InMemoryRoutingPolicyStore:
    """Deterministic in-memory routing policy + decision store."""

    def __init__(self) -> None:
        self._policies: dict[str, RoutingPolicyRecord] = {}
        self._decisions: dict[str, RoutingDecisionRecord] = {}

    def activate_policy(
        self, record: RoutingPolicyRecord, *, conn: Any | None = None
    ) -> None:
        if record.status != "ACTIVE":
            raise InvalidRequestError("only ACTIVE policies can be activated")
        if record.policy_id in self._policies:
            raise ConflictError("routing policy already exists", code="routing_policy_exists")
        for policy_id, existing in list(self._policies.items()):
            if existing.method == record.method and existing.status == "ACTIVE":
                self._policies[policy_id] = replace(existing, status="SUPERSEDED")
        self._policies[record.policy_id] = record

    def get_active_policy(
        self, method: str, *, conn: Any | None = None
    ) -> RoutingPolicyRecord | None:
        active = [
            record
            for record in self._policies.values()
            if record.method == method and record.status == "ACTIVE"
        ]
        if not active:
            return None
        return max(active, key=lambda record: record.version)

    def record_decision(
        self, record: RoutingDecisionRecord, *, conn: Any | None = None
    ) -> None:
        existing = self._decisions.get(record.payment_attempt_id)
        if existing is not None:
            if existing.decision_id == record.decision_id:
                return
            raise ConflictError(
                "routing decision already exists for payment attempt",
                code="routing_decision_exists",
            )
        self._decisions[record.payment_attempt_id] = record

    def get_decision(
        self, payment_attempt_id: str, *, conn: Any | None = None
    ) -> RoutingDecisionRecord | None:
        return self._decisions.get(payment_attempt_id)

    def list_policies(self) -> list[RoutingPolicyRecord]:
        return sorted(self._policies.values(), key=lambda record: (record.method, record.version))


@dataclass(frozen=True, slots=True)
class _ResultSample:
    connector_id: str
    status: str
    recorded_at: datetime


@dataclass(frozen=True, slots=True)
class _HealthSample:
    connector_id: str
    health_bps: int
    latency_ms: int
    sampled_at: datetime


class InMemoryRoutingSignalStore:
    """In-memory signals derived from connector results and health samples."""

    def __init__(self) -> None:
        self._results: list[_ResultSample] = []
        self._health: list[_HealthSample] = []
        self._fees_minor: dict[str, int] = {}

    def record_connector_result(
        self,
        connector_id: str,
        result: ConnectorResult,
        *,
        recorded_at: datetime,
    ) -> None:
        _require_non_empty(connector_id, "connector_id")
        _require_aware(recorded_at, "recorded_at")
        self._results.append(
            _ResultSample(
                connector_id=connector_id,
                status=result.status.value,
                recorded_at=recorded_at,
            )
        )

    def record_health_sample(
        self,
        connector_id: str,
        *,
        health_bps: int,
        latency_ms: int,
        sampled_at: datetime,
    ) -> None:
        _require_non_empty(connector_id, "connector_id")
        _require_bps(health_bps, "health_bps")
        _require_non_negative_int(latency_ms, "latency_ms")
        _require_aware(sampled_at, "sampled_at")
        self._health.append(
            _HealthSample(
                connector_id=connector_id,
                health_bps=health_bps,
                latency_ms=latency_ms,
                sampled_at=sampled_at,
            )
        )

    def set_connector_fee(self, connector_id: str, *, fee_minor: int) -> None:
        _require_non_empty(connector_id, "connector_id")
        self._fees_minor[connector_id] = _require_non_negative_int(fee_minor, "fee_minor")

    def clear_connector_fee(self, connector_id: str) -> None:
        self._fees_minor.pop(connector_id, None)

    def signal_for(
        self,
        connector_id: str,
        *,
        method: str,
        amount_minor: int,
        window_start: datetime,
        now: datetime,
        conn: Any | None = None,
    ) -> RoutingCandidateSignal | None:
        _require_non_empty(method, "method")
        _require_positive_int(amount_minor, "amount_minor")
        _require_aware(window_start, "window_start")
        _require_aware(now, "now")
        results = [
            row
            for row in self._results
            if row.connector_id == connector_id and window_start <= row.recorded_at <= now
        ]
        if not results:
            return None
        health_samples = [
            row
            for row in self._health
            if row.connector_id == connector_id and window_start <= row.sampled_at <= now
        ]
        if not health_samples or connector_id not in self._fees_minor:
            return None
        latest_health = max(health_samples, key=lambda row: row.sampled_at)
        successes = sum(1 for row in results if row.status == "success")
        return RoutingCandidateSignal(
            connector_id=connector_id,
            success_rate_bps=(successes * 10_000) // len(results),
            health_bps=latest_health.health_bps,
            fee_minor=self._fees_minor[connector_id],
            latency_ms=latest_health.latency_ms,
            observed_at=max(latest_health.sampled_at, max(row.recorded_at for row in results)),
        )


def _jsonb(payload: object) -> Any:
    from psycopg.types.json import Jsonb

    return Jsonb(payload, dumps=lambda obj: canonical_json(obj).decode("utf-8"))


class PostgresRoutingPolicyStore:
    """psycopg3 store for routing_policies and routing_decisions."""

    def __init__(self, connection_factory: Callable[[], Any]) -> None:
        self._connect = connection_factory

    def activate_policy(
        self, record: RoutingPolicyRecord, *, conn: Any | None = None
    ) -> None:
        if conn is not None:
            self._activate_on(conn, record)
            return
        with self._connect() as owned:
            self._activate_on(owned, record)
            owned.commit()

    def _activate_on(self, conn: Any, record: RoutingPolicyRecord) -> None:
        conn.execute(
            "UPDATE routing_policies SET status = 'SUPERSEDED' "
            "WHERE method = %s AND status = 'ACTIVE'",
            (record.method,),
        )
        conn.execute(
            "INSERT INTO routing_policies ("
            "policy_id, method, version, w_success_bps, w_health_bps, w_fee_bps, "
            "w_latency_bps, window_hours, status, approval_request_id, created_at, "
            "schema_version"
            ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                record.policy_id,
                record.method,
                record.version,
                record.weights.success_bps,
                record.weights.health_bps,
                record.weights.fee_bps,
                record.weights.latency_bps,
                record.window_hours,
                record.status,
                record.approval_request_id,
                record.created_at,
                record.schema_version,
            ),
        )

    def get_active_policy(
        self, method: str, *, conn: Any | None = None
    ) -> RoutingPolicyRecord | None:
        row = self._fetchone(
            conn,
            "SELECT policy_id, method, version, w_success_bps, w_health_bps, "
            "w_fee_bps, w_latency_bps, window_hours, status, approval_request_id, "
            "created_at, schema_version FROM routing_policies "
            "WHERE method = %s AND status = 'ACTIVE' ORDER BY version DESC LIMIT 1",
            (method,),
        )
        return None if row is None else self._row_to_policy(row)

    def record_decision(
        self, record: RoutingDecisionRecord, *, conn: Any | None = None
    ) -> None:
        sql = (
            "INSERT INTO routing_decisions ("
            "decision_id, payment_attempt_id, policy_id, candidates, "
            "selected_connector_id, inputs_hash, decided_at, fallback, "
            "fallback_reason, schema_version"
            ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (decision_id) DO NOTHING"
        )
        params = (
            record.decision_id,
            record.payment_attempt_id,
            record.policy_id,
            _jsonb(record.candidates_json()),
            record.selected_connector_id,
            record.inputs_hash,
            record.decided_at,
            record.fallback,
            record.fallback_reason,
            record.schema_version,
        )
        if conn is not None:
            conn.execute(sql, params)
            return
        with self._connect() as owned:
            owned.execute(sql, params)
            owned.commit()

    def get_decision(
        self, payment_attempt_id: str, *, conn: Any | None = None
    ) -> RoutingDecisionRecord | None:
        row = self._fetchone(
            conn,
            "SELECT decision_id, payment_attempt_id, policy_id, candidates, "
            "selected_connector_id, inputs_hash, decided_at, fallback, "
            "fallback_reason, schema_version FROM routing_decisions "
            "WHERE payment_attempt_id = %s ORDER BY decided_at DESC LIMIT 1",
            (payment_attempt_id,),
        )
        return None if row is None else self._row_to_decision(row)

    def _fetchone(self, conn: Any | None, sql: str, params: tuple) -> Mapping[str, Any] | None:
        from psycopg.rows import dict_row

        if conn is not None:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(sql, params)
                return cur.fetchone()
        with self._connect() as owned:
            with owned.cursor(row_factory=dict_row) as cur:
                cur.execute(sql, params)
                return cur.fetchone()

    @staticmethod
    def _row_to_policy(row: Mapping[str, Any]) -> RoutingPolicyRecord:
        return RoutingPolicyRecord(
            policy_id=row["policy_id"],
            method=row["method"],
            weights=RoutingWeights(
                success_bps=row["w_success_bps"],
                health_bps=row["w_health_bps"],
                fee_bps=row["w_fee_bps"],
                latency_bps=row["w_latency_bps"],
            ),
            version=row["version"],
            window_hours=row["window_hours"],
            status=row["status"],
            approval_request_id=row["approval_request_id"],
            created_at=row["created_at"],
            schema_version=row["schema_version"],
        )

    @staticmethod
    def _row_to_decision(row: Mapping[str, Any]) -> RoutingDecisionRecord:
        candidates = tuple(
            RoutingCandidateScore(
                connector_id=item["connector_id"],
                score_bps=item["score_bps"],
                success_rate_bps=item["success_rate_bps"],
                health_bps=item["health_bps"],
                fee_minor=item["fee_minor"],
                latency_ms=item["latency_ms"],
                fee_rank_bps=item["fee_rank_bps"],
                latency_rank_bps=item["latency_rank_bps"],
            )
            for item in row["candidates"]
        )
        return RoutingDecisionRecord(
            decision_id=row["decision_id"],
            payment_attempt_id=row["payment_attempt_id"],
            policy_id=row["policy_id"],
            candidates=candidates,
            selected_connector_id=row["selected_connector_id"],
            inputs_hash=row["inputs_hash"],
            decided_at=row["decided_at"],
            fallback=row["fallback"],
            fallback_reason=row["fallback_reason"],
            schema_version=row["schema_version"],
        )


class PostgresRoutingSignalStore:
    """Signals read from connector_results, connector_health_samples, and fee_rules."""

    def __init__(self, connection_factory: Callable[[], Any]) -> None:
        self._connect = connection_factory

    def record_connector_result(
        self,
        connector_id: str,
        result: ConnectorResult,
        *,
        recorded_at: datetime,
        call_kind: str = "SUBMIT",
        attempt_no: int = 1,
        mode_at_call: str = "SIMULATOR",
        raw_response_pointer: str = "routing/success-rate-source",
        webhook_inbound_id: str | None = None,
        conn: Any | None = None,
    ) -> None:
        result_id = make_id(
            "cres",
            {
                "connector_id": connector_id,
                "connector_ref": result.connector_ref,
                "attempt_no": attempt_no,
                "raw_response_hash": result.raw_response_hash,
            },
        )
        sql = (
            "INSERT INTO connector_results ("
            "result_id, instruction_id, connector_ref, connector_id, attempt_no, "
            "call_kind, status, rail_transaction_id, responded_at, error_code, "
            "raw_response_hash, raw_response_pointer, mode_at_call, "
            "webhook_inbound_id, produced_at"
            ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (connector_id, connector_ref, call_kind, attempt_no) DO NOTHING"
        )
        params = (
            result_id,
            result.instruction_id,
            result.connector_ref,
            connector_id,
            attempt_no,
            call_kind,
            result.status.value,
            result.rail_transaction_id,
            result.responded_at,
            result.error_code,
            result.raw_response_hash,
            raw_response_pointer,
            mode_at_call,
            webhook_inbound_id,
            recorded_at,
        )
        if conn is not None:
            conn.execute(sql, params)
            return
        with self._connect() as owned:
            owned.execute(sql, params)
            owned.commit()

    def signal_for(
        self,
        connector_id: str,
        *,
        method: str,
        amount_minor: int,
        window_start: datetime,
        now: datetime,
        conn: Any | None = None,
    ) -> RoutingCandidateSignal | None:
        if conn is not None:
            return self._signal_on(conn, connector_id, method, amount_minor, window_start, now)
        with self._connect() as owned:
            return self._signal_on(owned, connector_id, method, amount_minor, window_start, now)

    def _signal_on(
        self,
        conn: Any,
        connector_id: str,
        method: str,
        amount_minor: int,
        window_start: datetime,
        now: datetime,
    ) -> RoutingCandidateSignal | None:
        from psycopg.rows import dict_row

        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT COUNT(*)::int AS total, "
                "COUNT(*) FILTER (WHERE status = 'success')::int AS successes, "
                "MAX(produced_at) AS latest_result_at "
                "FROM connector_results "
                "WHERE connector_id = %s AND produced_at >= %s AND produced_at <= %s",
                (connector_id, window_start, now),
            )
            result_row = cur.fetchone()
            if result_row is None or result_row["total"] == 0:
                return None

            cur.execute(
                "SELECT healthy, latency_ms, detail_code, sampled_at "
                "FROM connector_health_samples "
                "WHERE connector_id = %s AND sampled_at >= %s AND sampled_at <= %s "
                "ORDER BY sampled_at DESC LIMIT 1",
                (connector_id, window_start, now),
            )
            health_row = cur.fetchone()
            if health_row is None or health_row["latency_ms"] is None:
                return None

            cur.execute(
                "SELECT fee_type, rate_bps, flat_amount_minor "
                "FROM ledger.fee_rules "
                "WHERE method = %s AND valid_from <= %s "
                "AND (valid_to IS NULL OR %s < valid_to) "
                "ORDER BY valid_from DESC LIMIT 1",
                (method, now, now),
            )
            fee_row = cur.fetchone()
            if fee_row is None:
                return None

        return RoutingCandidateSignal(
            connector_id=connector_id,
            success_rate_bps=(result_row["successes"] * 10_000) // result_row["total"],
            health_bps=_health_bps(health_row["healthy"], health_row["detail_code"]),
            fee_minor=_fee_minor(
                amount_minor,
                fee_type=fee_row["fee_type"],
                rate_bps=fee_row["rate_bps"],
                flat_amount_minor=fee_row["flat_amount_minor"],
            ),
            latency_ms=health_row["latency_ms"],
            observed_at=max(result_row["latest_result_at"], health_row["sampled_at"]),
        )


def _health_bps(healthy: bool, detail_code: str | None) -> int:
    if not healthy:
        return 0
    if detail_code == "degraded":
        return 5_000
    return 10_000


def _fee_minor(
    amount_minor: int,
    *,
    fee_type: str,
    rate_bps: int | None,
    flat_amount_minor: int | None,
) -> int:
    if fee_type == "PERCENTAGE":
        if rate_bps is None:
            raise InvalidRequestError("percentage fee rule needs rate_bps")
        return Money(amount_minor).multiply(Decimal(rate_bps) / Decimal(10_000)).amount_minor
    if fee_type == "FLAT_PAISA":
        if flat_amount_minor is None:
            raise InvalidRequestError("flat fee rule needs flat_amount_minor")
        return flat_amount_minor
    if fee_type == "FLAT_PLUS_PERCENTAGE":
        if rate_bps is None or flat_amount_minor is None:
            raise InvalidRequestError("flat plus percentage fee rule is incomplete")
        percent = Money(amount_minor).multiply(Decimal(rate_bps) / Decimal(10_000)).amount_minor
        return flat_amount_minor + percent
    raise InvalidRequestError(f"unknown fee_type {fee_type!r}")

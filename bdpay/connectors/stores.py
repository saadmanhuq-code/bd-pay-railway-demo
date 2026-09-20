"""Repository Protocols + deterministic in-memory stores (spec/10 Data Model).

Append-only tables (``connector_results``, ``connector_mode_changes``,
``connector_health_samples``, ``webhook_inbound`` raw rows,
``certification_runs``) expose no delete API. The connector-result insert
follows the queue-drain INSERT-OR-IGNORE idempotency pattern from
dse-profit-engine/src/dse_engine/storage/store.py (ADAPT PATTERN —
``UNIQUE(connector_id, connector_ref, call_kind, attempt_no)`` resolves a
duplicate insert to the existing row instead of failing).

Postgres implementations land with the wiring pass; the unit suite runs with
zero infrastructure on these in-memory stores.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from bdpay.connectors.ids_ext import make_connector_id
from bdpay.connectors.sdk import ConnectorResult
from bdpay.platform.canonical import canonical_json
from bdpay.platform.ids import make_id

__all__ = [
    "BreakerRecord",
    "BreakerStateStore",
    "CertificationRunRecord",
    "CertificationRunStore",
    "ConnectorResultRow",
    "ConnectorResultStore",
    "HealthSampleRecord",
    "HealthSampleStore",
    "InMemoryBreakerStateStore",
    "InMemoryCertificationRunStore",
    "InMemoryConnectorResultStore",
    "InMemoryHealthSampleStore",
    "InMemoryModeChangeStore",
    "InMemoryScenarioStore",
    "InMemoryWebhookInboundStore",
    "ModeChangeRecord",
    "ModeChangeStore",
    "PostgresBreakerStateStore",
    "PostgresCertificationRunStore",
    "PostgresConnectorResultStore",
    "PostgresWebhookInboundStore",
    "ScenarioStore",
    "WebhookInboundRecord",
    "WebhookInboundStore",
]

CALL_KINDS = frozenset(
    {
        "SUBMIT",
        "QUERY_STATUS",
        "REVERSE",
        "WEBHOOK",
        "MANUAL",
        "HEALTH",
        "BATCH_SUBMIT",
        "BATCH_STATUS",
    }
)

ConnectionFactory = Callable[[], Any]
RegistrationLookup = Callable[[str], Any]


def _jsonb(payload: object) -> Any:
    from psycopg.types.json import Jsonb

    return Jsonb(payload, dumps=lambda o: canonical_json(o).decode("utf-8"))


def _enum_value(value: object) -> object:
    return getattr(value, "value", value)


# -- connector_results --------------------------------------------------------


@dataclass(frozen=True)
class ConnectorResultRow:
    """One ``connector_results`` row (append-only audit spine)."""

    result_id: str
    instruction_id: str
    connector_ref: str
    connector_id: str
    attempt_no: int
    call_kind: str
    status: str
    rail_transaction_id: str | None
    responded_at: str | None
    error_code: str | None
    raw_response_hash: str
    raw_response_pointer: str
    mode_at_call: str
    webhook_inbound_id: str | None
    produced_at: datetime
    schema_version: int = 1


@runtime_checkable
class ConnectorResultStore(Protocol):
    """Append-only connector_results repository."""

    def record(
        self,
        result: ConnectorResult,
        *,
        connector_id: str,
        call_kind: str,
        attempt_no: int,
        mode_at_call: str,
        raw_response_pointer: str,
        produced_at: datetime,
        webhook_inbound_id: str | None = None,
    ) -> ConnectorResultRow: ...

    def list_by_ref(self, connector_ref: str) -> list[ConnectorResultRow]: ...

    def next_attempt_no(self, connector_id: str, connector_ref: str, call_kind: str) -> int: ...

    def last_for(
        self, connector_id: str, connector_ref: str, call_kind: str
    ) -> ConnectorResultRow | None: ...


class InMemoryConnectorResultStore:
    """Deterministic in-memory connector_results (INSERT-OR-IGNORE semantics).

    Pass shared ``rows`` / ``unique`` to simulate process restart against the
    same backing (unit drills without Postgres).
    """

    def __init__(
        self,
        rows: list[ConnectorResultRow] | None = None,
        unique: dict[tuple[str, str, str, int], ConnectorResultRow] | None = None,
    ) -> None:
        self._rows: list[ConnectorResultRow] = rows if rows is not None else []
        self._unique: dict[tuple[str, str, str, int], ConnectorResultRow] = (
            unique if unique is not None else {}
        )

    def record(
        self,
        result: ConnectorResult,
        *,
        connector_id: str,
        call_kind: str,
        attempt_no: int,
        mode_at_call: str,
        raw_response_pointer: str,
        produced_at: datetime,
        webhook_inbound_id: str | None = None,
    ) -> ConnectorResultRow:
        if call_kind not in CALL_KINDS:
            raise ValueError(f"unknown call_kind {call_kind!r}")
        key = (connector_id, result.connector_ref, call_kind, attempt_no)
        existing = self._unique.get(key)
        if existing is not None:
            return existing
        row = ConnectorResultRow(
            result_id=make_id(
                "cres",
                {
                    "connector_id": connector_id,
                    "connector_ref": result.connector_ref,
                    "attempt_no": attempt_no,
                    "raw_response_hash": result.raw_response_hash,
                },
            ),
            instruction_id=result.instruction_id,
            connector_ref=result.connector_ref,
            connector_id=connector_id,
            attempt_no=attempt_no,
            call_kind=call_kind,
            status=result.status.value,
            rail_transaction_id=result.rail_transaction_id,
            responded_at=result.responded_at,
            error_code=result.error_code,
            raw_response_hash=result.raw_response_hash,
            raw_response_pointer=raw_response_pointer,
            mode_at_call=mode_at_call,
            webhook_inbound_id=webhook_inbound_id,
            produced_at=produced_at,
        )
        self._rows.append(row)
        self._unique[key] = row
        return row

    def list_by_ref(self, connector_ref: str) -> list[ConnectorResultRow]:
        return [row for row in self._rows if row.connector_ref == connector_ref]

    def list_all(self) -> list[ConnectorResultRow]:
        return list(self._rows)

    def next_attempt_no(self, connector_id: str, connector_ref: str, call_kind: str) -> int:
        attempts = [
            row.attempt_no
            for row in self._rows
            if row.connector_id == connector_id
            and row.connector_ref == connector_ref
            and row.call_kind == call_kind
        ]
        return (max(attempts) + 1) if attempts else 1

    def last_for(
        self, connector_id: str, connector_ref: str, call_kind: str
    ) -> ConnectorResultRow | None:
        for row in reversed(self._rows):
            if (
                row.connector_id == connector_id
                and row.connector_ref == connector_ref
                and row.call_kind == call_kind
            ):
                return row
        return None


# -- connector_mode_changes ---------------------------------------------------


@dataclass(frozen=True)
class ModeChangeRecord:
    """One ``connector_mode_changes`` row (append-only)."""

    mode_change_id: str
    connector_id: str
    from_mode: str
    to_mode: str
    reason: str
    actor_id: str
    approval_request_id: str | None
    occurred_at: datetime
    schema_version: int = 1


@runtime_checkable
class ModeChangeStore(Protocol):
    def append(self, record: ModeChangeRecord) -> None: ...

    def list_for(self, connector_id: str) -> list[ModeChangeRecord]: ...


class InMemoryModeChangeStore:
    def __init__(self) -> None:
        self._rows: list[ModeChangeRecord] = []

    def append(self, record: ModeChangeRecord) -> None:
        self._rows.append(record)

    def list_for(self, connector_id: str) -> list[ModeChangeRecord]:
        return [row for row in self._rows if row.connector_id == connector_id]


# -- connector_health_samples -------------------------------------------------


@dataclass(frozen=True)
class HealthSampleRecord:
    """One ``connector_health_samples`` row (append-only)."""

    sample_id: str
    connector_id: str
    healthy: bool
    latency_ms: int | None
    detail_code: str | None
    sampled_at: datetime
    schema_version: int = 1


@runtime_checkable
class HealthSampleStore(Protocol):
    def append(self, record: HealthSampleRecord) -> None: ...

    def list_for(self, connector_id: str) -> list[HealthSampleRecord]: ...


class InMemoryHealthSampleStore:
    def __init__(self) -> None:
        self._rows: list[HealthSampleRecord] = []

    def append(self, record: HealthSampleRecord) -> None:
        self._rows.append(record)

    def list_for(self, connector_id: str) -> list[HealthSampleRecord]:
        return [row for row in self._rows if row.connector_id == connector_id]


# -- circuit_breaker_states ---------------------------------------------------


@dataclass
class BreakerRecord:
    """Authoritative breaker state per connector (``circuit_breaker_states``).

    ``half_open_probes`` tracks admitted HALF_OPEN ``query_status`` probes
    against ``probe_quota``; the Postgres impl keeps it alongside
    ``half_open_successes``.
    """

    breaker_id: str
    connector_id: str
    state: str = "CLOSED"
    consecutive_failures: int = 0
    window_started_at: datetime | None = None
    opened_at: datetime | None = None
    half_open_successes: int = 0
    half_open_probes: int = 0
    failure_threshold: int = 5
    window_s: int = 60
    cooldown_s: int = 30
    probe_quota: int = 3
    updated_at: datetime | None = None
    schema_version: int = 1


@runtime_checkable
class BreakerStateStore(Protocol):
    """Breaker state repository. ``load`` raising means fail-closed (OPEN)."""

    def load(self, connector_id: str) -> BreakerRecord | None: ...

    def save(self, record: BreakerRecord) -> None: ...


class InMemoryBreakerStateStore:
    """In-memory breaker state; pass shared ``records`` for restart drills."""

    def __init__(self, records: dict[str, BreakerRecord] | None = None) -> None:
        self._records: dict[str, BreakerRecord] = records if records is not None else {}

    def load(self, connector_id: str) -> BreakerRecord | None:
        return self._records.get(connector_id)

    def save(self, record: BreakerRecord) -> None:
        self._records[record.connector_id] = record


# -- webhook_inbound ----------------------------------------------------------


@dataclass
class WebhookInboundRecord:
    """One ``webhook_inbound`` row; status advances solely via the pipeline FSM."""

    inbound_id: str
    connector_id: str
    content_hash: str
    headers_hash: str
    raw_pointer: str
    source_ip_hash: str
    status: str = "RECEIVED"
    reject_reason: str | None = None
    parsed_connector_ref: str | None = None
    result_id: str | None = None
    received_at: datetime | None = None
    processed_at: datetime | None = None
    schema_version: int = 1


@runtime_checkable
class WebhookInboundStore(Protocol):
    def insert(self, record: WebhookInboundRecord) -> tuple[WebhookInboundRecord, bool]: ...

    def get(self, inbound_id: str) -> WebhookInboundRecord | None: ...

    def list_by_status(self, status: str) -> list[WebhookInboundRecord]: ...

    def save(self, record: WebhookInboundRecord) -> None: ...


class InMemoryWebhookInboundStore:
    """In-memory webhook_inbound with the UNIQUE(connector_id, content_hash) key.

    Pass shared ``by_id`` / ``unique`` / ``order`` to simulate process restart
    against the same backing (unit drills without Postgres).
    """

    def __init__(
        self,
        by_id: dict[str, WebhookInboundRecord] | None = None,
        unique: dict[tuple[str, str], str] | None = None,
        order: list[str] | None = None,
    ) -> None:
        self._by_id: dict[str, WebhookInboundRecord] = by_id if by_id is not None else {}
        self._unique: dict[tuple[str, str], str] = unique if unique is not None else {}
        self._order: list[str] = order if order is not None else []

    def insert(self, record: WebhookInboundRecord) -> tuple[WebhookInboundRecord, bool]:
        key = (record.connector_id, record.content_hash)
        existing_id = self._unique.get(key)
        if existing_id is not None:
            return self._by_id[existing_id], False
        self._by_id[record.inbound_id] = record
        self._unique[key] = record.inbound_id
        self._order.append(record.inbound_id)
        return record, True

    def get(self, inbound_id: str) -> WebhookInboundRecord | None:
        return self._by_id.get(inbound_id)

    def list_by_status(self, status: str) -> list[WebhookInboundRecord]:
        return [self._by_id[i] for i in self._order if self._by_id[i].status == status]

    def list_all(self) -> list[WebhookInboundRecord]:
        return [self._by_id[i] for i in self._order]

    def save(self, record: WebhookInboundRecord) -> None:
        if record.inbound_id not in self._by_id:
            raise KeyError(f"unknown inbound_id {record.inbound_id}")
        self._by_id[record.inbound_id] = record


# -- simulator_scenarios ------------------------------------------------------


@runtime_checkable
class ScenarioStore(Protocol):
    def put(self, scenario: object) -> None: ...

    def list_enabled(self, connector_id: str) -> list[object]: ...


class InMemoryScenarioStore:
    """Scenario repository; ``list_enabled`` returns name-ordered scenarios."""

    def __init__(self) -> None:
        self._by_id: dict[str, object] = {}

    def put(self, scenario: object) -> None:
        self._by_id[scenario.scenario_id] = scenario  # type: ignore[attr-defined]

    def list_enabled(self, connector_id: str) -> list[object]:
        rows = [
            s
            for s in self._by_id.values()
            if s.connector_id == connector_id and s.enabled  # type: ignore[attr-defined]
        ]
        return sorted(rows, key=lambda s: s.name)  # type: ignore[attr-defined]


# -- certification_runs -------------------------------------------------------


@dataclass
class CertificationRunRecord:
    """One ``certification_runs`` row (append-only; FSM advances status)."""

    run_id: str
    connector_id: str
    adapter_version: str
    mode_under_test: str
    triggered_by: str
    created_at: datetime
    status: str = "QUEUED"
    checks: list[dict] = field(default_factory=list)
    report_pointer: str | None = None
    report_hash: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    schema_version: int = 1


@runtime_checkable
class CertificationRunStore(Protocol):
    def save(self, record: CertificationRunRecord) -> None: ...

    def get(self, run_id: str) -> CertificationRunRecord | None: ...

    def list_for(self, connector_id: str) -> list[CertificationRunRecord]: ...


class InMemoryCertificationRunStore:
    def __init__(self) -> None:
        self._by_id: dict[str, CertificationRunRecord] = {}
        self._order: list[str] = []

    def save(self, record: CertificationRunRecord) -> None:
        if record.run_id not in self._by_id:
            self._order.append(record.run_id)
        self._by_id[record.run_id] = record

    def get(self, run_id: str) -> CertificationRunRecord | None:
        return self._by_id.get(run_id)

    def list_for(self, connector_id: str) -> list[CertificationRunRecord]:
        return [
            self._by_id[i] for i in self._order if self._by_id[i].connector_id == connector_id
        ]


_CERTIFICATION_RUN_COLUMNS = """
    run_id, connector_id, adapter_version, mode_under_test, status, checks,
    report_pointer, report_hash, started_at, finished_at, triggered_by,
    created_at, schema_version
"""


def _certification_run_from_row(row: dict[str, Any]) -> CertificationRunRecord:
    return CertificationRunRecord(
        run_id=row["run_id"],
        connector_id=row["connector_id"],
        adapter_version=row["adapter_version"],
        mode_under_test=row["mode_under_test"],
        triggered_by=row["triggered_by"],
        created_at=row["created_at"],
        status=row["status"],
        checks=list(row["checks"] or []),
        report_pointer=row["report_pointer"],
        report_hash=row["report_hash"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        schema_version=row["schema_version"],
    )


class PostgresCertificationRunStore:
    """Postgres store for ``certification_runs`` in migration 0050."""

    def __init__(
        self,
        connection_factory: ConnectionFactory,
        *,
        registration_lookup: RegistrationLookup | None = None,
    ) -> None:
        self._connect = connection_factory
        self._registration_lookup = registration_lookup

    def _upsert_registration(self, conn: Any, registration: Any) -> None:
        conn.execute(
            """
            INSERT INTO connector_registry (
                registration_id, connector_id, display_name, protocol,
                capabilities, supported_methods, active_mode, previous_mode,
                enabled, adapter_version, sdk_version, health_status,
                last_health_at, certification_status, last_certified_at,
                config, config_schema, timeout_ms, retry_budget, created_at,
                updated_at, schema_version
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s
            )
            ON CONFLICT (connector_id) DO UPDATE SET
                display_name = EXCLUDED.display_name,
                protocol = EXCLUDED.protocol,
                capabilities = EXCLUDED.capabilities,
                supported_methods = EXCLUDED.supported_methods,
                active_mode = EXCLUDED.active_mode,
                previous_mode = EXCLUDED.previous_mode,
                enabled = EXCLUDED.enabled,
                adapter_version = EXCLUDED.adapter_version,
                sdk_version = EXCLUDED.sdk_version,
                health_status = EXCLUDED.health_status,
                last_health_at = EXCLUDED.last_health_at,
                certification_status = EXCLUDED.certification_status,
                last_certified_at = EXCLUDED.last_certified_at,
                config = EXCLUDED.config,
                config_schema = EXCLUDED.config_schema,
                timeout_ms = EXCLUDED.timeout_ms,
                retry_budget = EXCLUDED.retry_budget,
                updated_at = EXCLUDED.updated_at,
                schema_version = EXCLUDED.schema_version
            """,
            (
                registration.registration_id,
                registration.connector_id,
                registration.display_name,
                registration.protocol,
                list(registration.capabilities),
                list(registration.supported_methods),
                _enum_value(registration.active_mode),
                (
                    None
                    if registration.previous_mode is None
                    else _enum_value(registration.previous_mode)
                ),
                registration.enabled,
                registration.adapter_version,
                registration.sdk_version,
                registration.health_status,
                registration.last_health_at,
                registration.certification_status,
                registration.last_certified_at,
                _jsonb(registration.config),
                _jsonb(registration.config_schema),
                registration.timeout_ms,
                _jsonb(registration.retry_budget),
                registration.created_at,
                registration.updated_at,
                registration.schema_version,
            ),
        )

    def save(self, record: CertificationRunRecord) -> None:
        with self._connect() as conn:
            if self._registration_lookup is not None:
                self._upsert_registration(conn, self._registration_lookup(record.connector_id))
            conn.execute(
                """
                INSERT INTO certification_runs (
                    run_id, connector_id, adapter_version, mode_under_test, status,
                    checks, report_pointer, report_hash, started_at, finished_at,
                    triggered_by, created_at, schema_version
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (run_id) DO UPDATE SET
                    connector_id = EXCLUDED.connector_id,
                    adapter_version = EXCLUDED.adapter_version,
                    mode_under_test = EXCLUDED.mode_under_test,
                    status = EXCLUDED.status,
                    checks = EXCLUDED.checks,
                    report_pointer = EXCLUDED.report_pointer,
                    report_hash = EXCLUDED.report_hash,
                    started_at = EXCLUDED.started_at,
                    finished_at = EXCLUDED.finished_at,
                    triggered_by = EXCLUDED.triggered_by,
                    created_at = EXCLUDED.created_at,
                    schema_version = EXCLUDED.schema_version
                """,
                (
                    record.run_id,
                    record.connector_id,
                    record.adapter_version,
                    record.mode_under_test,
                    record.status,
                    _jsonb(record.checks),
                    record.report_pointer,
                    record.report_hash,
                    record.started_at,
                    record.finished_at,
                    record.triggered_by,
                    record.created_at,
                    record.schema_version,
                ),
            )
            conn.commit()

    def get(self, run_id: str) -> CertificationRunRecord | None:
        from psycopg.rows import dict_row

        with self._connect() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    f"SELECT {_CERTIFICATION_RUN_COLUMNS} FROM certification_runs "
                    "WHERE run_id = %s",
                    (run_id,),
                )
                row = cur.fetchone()
        return None if row is None else _certification_run_from_row(row)

    def list_for(self, connector_id: str) -> list[CertificationRunRecord]:
        from psycopg.rows import dict_row

        with self._connect() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    f"SELECT {_CERTIFICATION_RUN_COLUMNS} FROM certification_runs "
                    "WHERE connector_id = %s ORDER BY created_at, run_id",
                    (connector_id,),
                )
                rows = cur.fetchall()
        return [_certification_run_from_row(row) for row in rows]



_CONNECTOR_RESULT_COLUMNS = """
    result_id, instruction_id, connector_ref, connector_id, attempt_no, call_kind,
    status, rail_transaction_id, responded_at, error_code, raw_response_hash,
    raw_response_pointer, mode_at_call, webhook_inbound_id, produced_at, schema_version
"""


def _responded_at_to_db(value: str | None) -> datetime | None:
    if value is None:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return datetime.fromisoformat(text)


def _responded_at_from_db(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, datetime):
        iso = value.isoformat()
        if iso.endswith("+00:00"):
            return iso[:-6] + "Z"
        return iso
    return str(value)


def _connector_result_from_row(row: dict[str, Any]) -> ConnectorResultRow:
    return ConnectorResultRow(
        result_id=row["result_id"],
        instruction_id=row["instruction_id"],
        connector_ref=row["connector_ref"],
        connector_id=row["connector_id"],
        attempt_no=int(row["attempt_no"]),
        call_kind=row["call_kind"],
        status=row["status"],
        rail_transaction_id=row["rail_transaction_id"],
        responded_at=_responded_at_from_db(row["responded_at"]),
        error_code=row["error_code"],
        raw_response_hash=row["raw_response_hash"],
        raw_response_pointer=row["raw_response_pointer"],
        mode_at_call=row["mode_at_call"],
        webhook_inbound_id=row["webhook_inbound_id"],
        produced_at=row["produced_at"],
        schema_version=int(row["schema_version"]),
    )


class PostgresConnectorResultStore:
    """Postgres-backed connector_results (migration ``0050`` / ``0163``).

    First-write-wins on ``UNIQUE(connector_id, connector_ref, call_kind, attempt_no)``.
    """

    def __init__(self, connection_factory: ConnectionFactory) -> None:
        self._connect = connection_factory

    def record(
        self,
        result: ConnectorResult,
        *,
        connector_id: str,
        call_kind: str,
        attempt_no: int,
        mode_at_call: str,
        raw_response_pointer: str,
        produced_at: datetime,
        webhook_inbound_id: str | None = None,
    ) -> ConnectorResultRow:
        from psycopg.rows import dict_row

        if call_kind not in CALL_KINDS:
            raise ValueError(f"unknown call_kind {call_kind!r}")
        result_id = make_id(
            "cres",
            {
                "connector_id": connector_id,
                "connector_ref": result.connector_ref,
                "attempt_no": attempt_no,
                "raw_response_hash": result.raw_response_hash,
            },
        )
        with self._connect() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """
                    INSERT INTO connector_results (
                        result_id, instruction_id, connector_ref, connector_id,
                        attempt_no, call_kind, status, rail_transaction_id,
                        responded_at, error_code, raw_response_hash,
                        raw_response_pointer, mode_at_call, webhook_inbound_id,
                        produced_at, schema_version
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 1
                    )
                    ON CONFLICT (connector_id, connector_ref, call_kind, attempt_no)
                    DO NOTHING
                    RETURNING """
                    + _CONNECTOR_RESULT_COLUMNS,
                    (
                        result_id,
                        result.instruction_id,
                        result.connector_ref,
                        connector_id,
                        attempt_no,
                        call_kind,
                        result.status.value,
                        result.rail_transaction_id,
                        _responded_at_to_db(result.responded_at),
                        result.error_code,
                        result.raw_response_hash,
                        raw_response_pointer,
                        mode_at_call,
                        webhook_inbound_id,
                        produced_at,
                    ),
                )
                row = cur.fetchone()
                if row is None:
                    cur.execute(
                        f"""
                        SELECT {_CONNECTOR_RESULT_COLUMNS}
                        FROM connector_results
                        WHERE connector_id = %s AND connector_ref = %s
                          AND call_kind = %s AND attempt_no = %s
                        """,
                        (connector_id, result.connector_ref, call_kind, attempt_no),
                    )
                    row = cur.fetchone()
            conn.commit()
        assert row is not None
        return _connector_result_from_row(row)

    def list_by_ref(self, connector_ref: str) -> list[ConnectorResultRow]:
        from psycopg.rows import dict_row

        with self._connect() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    f"""
                    SELECT {_CONNECTOR_RESULT_COLUMNS}
                    FROM connector_results
                    WHERE connector_ref = %s
                    ORDER BY produced_at, result_id
                    """,
                    (connector_ref,),
                )
                rows = cur.fetchall()
        return [_connector_result_from_row(row) for row in rows]

    def next_attempt_no(self, connector_id: str, connector_ref: str, call_kind: str) -> int:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT COALESCE(MAX(attempt_no), 0)
                    FROM connector_results
                    WHERE connector_id = %s AND connector_ref = %s AND call_kind = %s
                    """,
                    (connector_id, connector_ref, call_kind),
                )
                (max_attempt,) = cur.fetchone()
        return int(max_attempt) + 1

    def last_for(
        self, connector_id: str, connector_ref: str, call_kind: str
    ) -> ConnectorResultRow | None:
        from psycopg.rows import dict_row

        with self._connect() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    f"""
                    SELECT {_CONNECTOR_RESULT_COLUMNS}
                    FROM connector_results
                    WHERE connector_id = %s AND connector_ref = %s AND call_kind = %s
                    ORDER BY attempt_no DESC, produced_at DESC, result_id DESC
                    LIMIT 1
                    """,
                    (connector_id, connector_ref, call_kind),
                )
                row = cur.fetchone()
        return None if row is None else _connector_result_from_row(row)


_BREAKER_COLUMNS = """
    breaker_id, connector_id, state, consecutive_failures, window_started_at,
    opened_at, half_open_successes, half_open_probes, failure_threshold,
    window_s, cooldown_s, probe_quota, updated_at, schema_version
"""


def _breaker_from_row(row: dict[str, Any]) -> BreakerRecord:
    return BreakerRecord(
        breaker_id=row["breaker_id"],
        connector_id=row["connector_id"],
        state=row["state"],
        consecutive_failures=int(row["consecutive_failures"]),
        window_started_at=row["window_started_at"],
        opened_at=row["opened_at"],
        half_open_successes=int(row["half_open_successes"]),
        half_open_probes=int(row["half_open_probes"]),
        failure_threshold=int(row["failure_threshold"]),
        window_s=int(row["window_s"]),
        cooldown_s=int(row["cooldown_s"]),
        probe_quota=int(row["probe_quota"]),
        updated_at=row["updated_at"],
        schema_version=int(row["schema_version"]),
    )


class PostgresBreakerStateStore:
    """Postgres-backed circuit_breaker_states (migration ``0050`` / ``0163``).

    ``load`` raising propagates so CircuitBreaker fail-closes (OPEN).
    """

    def __init__(self, connection_factory: ConnectionFactory) -> None:
        self._connect = connection_factory

    def load(self, connector_id: str) -> BreakerRecord | None:
        from psycopg.rows import dict_row

        with self._connect() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    f"""
                    SELECT {_BREAKER_COLUMNS}
                    FROM circuit_breaker_states
                    WHERE connector_id = %s
                    """,
                    (connector_id,),
                )
                row = cur.fetchone()
        return None if row is None else _breaker_from_row(row)

    def save(self, record: BreakerRecord) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO circuit_breaker_states (
                        breaker_id, connector_id, state, consecutive_failures,
                        window_started_at, opened_at, half_open_successes,
                        half_open_probes, failure_threshold, window_s, cooldown_s,
                        probe_quota, updated_at, schema_version
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                    )
                    ON CONFLICT (connector_id) DO UPDATE SET
                        breaker_id = EXCLUDED.breaker_id,
                        state = EXCLUDED.state,
                        consecutive_failures = EXCLUDED.consecutive_failures,
                        window_started_at = EXCLUDED.window_started_at,
                        opened_at = EXCLUDED.opened_at,
                        half_open_successes = EXCLUDED.half_open_successes,
                        half_open_probes = EXCLUDED.half_open_probes,
                        failure_threshold = EXCLUDED.failure_threshold,
                        window_s = EXCLUDED.window_s,
                        cooldown_s = EXCLUDED.cooldown_s,
                        probe_quota = EXCLUDED.probe_quota,
                        updated_at = EXCLUDED.updated_at,
                        schema_version = EXCLUDED.schema_version
                    """,
                    (
                        record.breaker_id,
                        record.connector_id,
                        record.state,
                        record.consecutive_failures,
                        record.window_started_at,
                        record.opened_at,
                        record.half_open_successes,
                        record.half_open_probes,
                        record.failure_threshold,
                        record.window_s,
                        record.cooldown_s,
                        record.probe_quota,
                        record.updated_at,
                        record.schema_version,
                    ),
                )
            conn.commit()


_WEBHOOK_INBOUND_COLUMNS = """
    inbound_id, connector_id, content_hash, headers_hash, raw_pointer, source_ip_hash,
    status, reject_reason, parsed_connector_ref, result_id, received_at, processed_at,
    schema_version
"""


def _webhook_inbound_from_row(row: dict[str, Any]) -> WebhookInboundRecord:
    return WebhookInboundRecord(
        inbound_id=row["inbound_id"],
        connector_id=row["connector_id"],
        content_hash=row["content_hash"],
        headers_hash=row["headers_hash"],
        raw_pointer=row["raw_pointer"],
        source_ip_hash=row["source_ip_hash"],
        status=row["status"],
        reject_reason=row["reject_reason"],
        parsed_connector_ref=row["parsed_connector_ref"],
        result_id=row["result_id"],
        received_at=row["received_at"],
        processed_at=row["processed_at"],
        schema_version=int(row["schema_version"]),
    )


class PostgresWebhookInboundStore:
    """Postgres-backed webhook_inbound (migration ``0050`` / ``0163``).

    First-write-wins on ``UNIQUE(connector_id, content_hash)`` (CERT dedupe).
    """

    def __init__(self, connection_factory: ConnectionFactory) -> None:
        self._connect = connection_factory

    def insert(self, record: WebhookInboundRecord) -> tuple[WebhookInboundRecord, bool]:
        from psycopg.rows import dict_row

        with self._connect() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """
                    INSERT INTO webhook_inbound (
                        inbound_id, connector_id, content_hash, headers_hash,
                        raw_pointer, source_ip_hash, status, reject_reason,
                        parsed_connector_ref, result_id, received_at, processed_at,
                        schema_version
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                    )
                    ON CONFLICT (connector_id, content_hash) DO NOTHING
                    RETURNING """
                    + _WEBHOOK_INBOUND_COLUMNS,
                    (
                        record.inbound_id,
                        record.connector_id,
                        record.content_hash,
                        record.headers_hash,
                        record.raw_pointer,
                        record.source_ip_hash,
                        record.status,
                        record.reject_reason,
                        record.parsed_connector_ref,
                        record.result_id,
                        record.received_at,
                        record.processed_at,
                        record.schema_version,
                    ),
                )
                row = cur.fetchone()
                inserted = row is not None
                if row is None:
                    cur.execute(
                        f"""
                        SELECT {_WEBHOOK_INBOUND_COLUMNS}
                        FROM webhook_inbound
                        WHERE connector_id = %s AND content_hash = %s
                        """,
                        (record.connector_id, record.content_hash),
                    )
                    row = cur.fetchone()
            conn.commit()
        assert row is not None
        return _webhook_inbound_from_row(row), inserted

    def get(self, inbound_id: str) -> WebhookInboundRecord | None:
        from psycopg.rows import dict_row

        with self._connect() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    f"""
                    SELECT {_WEBHOOK_INBOUND_COLUMNS}
                    FROM webhook_inbound
                    WHERE inbound_id = %s
                    """,
                    (inbound_id,),
                )
                row = cur.fetchone()
        return None if row is None else _webhook_inbound_from_row(row)

    def list_by_status(self, status: str) -> list[WebhookInboundRecord]:
        from psycopg.rows import dict_row

        with self._connect() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    f"""
                    SELECT {_WEBHOOK_INBOUND_COLUMNS}
                    FROM webhook_inbound
                    WHERE status = %s
                    ORDER BY received_at, inbound_id
                    """,
                    (status,),
                )
                rows = cur.fetchall()
        return [_webhook_inbound_from_row(row) for row in rows]

    def save(self, record: WebhookInboundRecord) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE webhook_inbound SET
                        status = %s,
                        reject_reason = %s,
                        parsed_connector_ref = %s,
                        result_id = %s,
                        processed_at = %s,
                        schema_version = %s
                    WHERE inbound_id = %s
                    """,
                    (
                        record.status,
                        record.reject_reason,
                        record.parsed_connector_ref,
                        record.result_id,
                        record.processed_at,
                        record.schema_version,
                        record.inbound_id,
                    ),
                )
                if cur.rowcount == 0:
                    raise KeyError(f"unknown inbound_id {record.inbound_id}")
            conn.commit()



def make_breaker_record(
    connector_id: str,
    *,
    failure_threshold: int = 5,
    window_s: int = 60,
    cooldown_s: int = 30,
    probe_quota: int = 3,
) -> BreakerRecord:
    """Fresh CLOSED breaker row with the spec/10 DDL identity scheme."""
    return BreakerRecord(
        breaker_id=make_connector_id("cbrk", {"connector_id": connector_id}),
        connector_id=connector_id,
        failure_threshold=failure_threshold,
        window_s=window_s,
        cooldown_s=cooldown_s,
        probe_quota=probe_quota,
    )

"""Certification harness — CERT-01..CERT-10 conformance suite (spec/10).

Runs against a connector in SIMULATOR mode and gates mode promotion. The
report is deterministic: ``dict -> canonical_json -> sha256`` (``report_hash``);
CERT-10 executes the whole suite twice on freshly-built environments and
asserts byte-identical reports — proving no wall clock or randomness leaked
into the adapter. CertificationRun FSM: QUEUED -> RUNNING -> PASSED | FAILED |
ERRORED (terminal), refusal-first.

CERT-03(b)/(c) (Postgres event trigger + grant audit) require live Postgres
and run with the integration wiring pass; the static AST scan (a) runs here.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
import logging
import sys
from dataclasses import dataclass
from datetime import datetime

from bdpay.connectors.breaker import CircuitBreaker
from bdpay.connectors.ids_ext import make_connector_id
from bdpay.connectors.ports import (
    AcceptAllKernelHandoff,
    AuditSink,
    EventSink,
    InMemoryAuditSink,
    InMemoryEventSink,
    InMemoryObjectStore,
    ObjectStore,
)
from bdpay.connectors.registry import ConnectorMode, ConnectorRegistry
from bdpay.connectors.runner import ConnectorRunner
from bdpay.connectors.scenarios import (
    BASELINE_SCENARIO_NAMES,
    build_baseline_scenarios,
    build_extended_scenarios,
)
from bdpay.connectors.sdk import (
    ConnectorStatus,
    Money,
    PaymentInstruction,
)
from bdpay.connectors.simulator import (
    TIMESTAMP_HEADER,
    ScenarioEngine,
    SimulatorConnector,
    SimulatorWebhookHandler,
    sign_callback,
)
from bdpay.connectors.stores import (
    CertificationRunRecord,
    CertificationRunStore,
    InMemoryCertificationRunStore,
    InMemoryConnectorResultStore,
)
from bdpay.platform.canonical import canonical_json, sha256_canonical
from bdpay.platform.clock import Clock, SteppingClock
from bdpay.platform.ids import make_id

__all__ = [
    "CertificationHarness",
    "SimulatorCertEnvironment",
    "build_simulator_environment",
    "certification_matrix",
    "constructor_db_handle_violations",
    "write_certification_matrix",
]

_FORBIDDEN_IMPORT_ROOTS = ("bdpay.kernel", "bdpay.ledger", "bdpay.compliance")

#: Constructor parameter names that smell like a database handle. The
#: no-business-DB-writes invariant is enforced structurally: an adapter that
#: cannot receive a DB handle cannot write business tables.
_FORBIDDEN_CTOR_PARAMS = frozenset(
    {
        "db",
        "database",
        "db_handle",
        "db_conn",
        "db_pool",
        "db_session",
        "connection",
        "conn",
        "session",
        "pool",
        "cursor",
        "dsn",
        "pg",
        "pg_pool",
        "postgres",
    }
)


def constructor_db_handle_violations(adapter: object) -> list[str]:
    """Constructor parameter names of ``adapter`` that look like DB handles.

    Used by CERT-03: the adapter's ``__init__`` signature must not accept any
    database handle — connectors never write business DB tables, and the
    structural guarantee is that nobody can hand them a connection.
    """
    signature = inspect.signature(type(adapter).__init__)
    return sorted(
        name
        for name in signature.parameters
        if name != "self" and name.lower() in _FORBIDDEN_CTOR_PARAMS
    )

_RUNNER_SYNTHETIC_CODES = frozenset(
    {"hard_timeout", "transport_error", "connector_disabled", "circuit_open", "status_unresolved"}
)


async def _instant_sleep(_seconds: float) -> None:
    return None


@dataclass
class SimulatorCertEnvironment:
    """One freshly-built SIMULATOR test bench (fresh clock per build)."""

    connector_id: str
    clock: SteppingClock
    registry: ConnectorRegistry
    breaker: CircuitBreaker
    runner: ConnectorRunner
    engine: ScenarioEngine
    adapter: SimulatorConnector
    pipeline: object
    handler: SimulatorWebhookHandler
    kernel: AcceptAllKernelHandoff
    object_store: InMemoryObjectStore
    result_store: InMemoryConnectorResultStore
    signing_key: bytes


def build_simulator_environment(
    connector_id: str,
    *,
    start: datetime,
    timeout_ms: int = 150,
    signing_key: bytes = b"sim-cert-verify-key",
    adapter_version: str = "1.0.0",
) -> SimulatorCertEnvironment:
    """Deterministic SIMULATOR environment factory (CERT-10 builds two)."""
    from bdpay.connectors.webhooks import WebhookPipeline

    clock = SteppingClock(start)
    object_store = InMemoryObjectStore()
    result_store = InMemoryConnectorResultStore()
    scenarios = build_baseline_scenarios(connector_id, created_at=clock.now())
    scenarios += build_extended_scenarios(connector_id, created_at=clock.now())
    engine = ScenarioEngine(
        connector_id,
        scenarios,
        clock=clock,
        signing_key=signing_key,
        object_store=object_store,
        sleep=_instant_sleep,
    )
    adapter = SimulatorConnector(connector_id, engine)
    registry = ConnectorRegistry(clock=clock)
    registry.register(
        connector_id,
        display_name=f"{connector_id} (simulator cert)",
        protocol="PAYMENT",
        capabilities=("submit", "query_status", "reverse", "health_check", "webhook"),
        supported_methods=adapter.supported_methods,
        adapter_version=adapter_version,
        active_mode=ConnectorMode.SIMULATOR,
        timeout_ms=timeout_ms,
        retry_budget={"submit_retries": 0, "poll_attempts": 3, "poll_interval_ms": 5_000},
    )
    breaker = CircuitBreaker(clock=clock)
    runner = ConnectorRunner(
        registry=registry,
        breaker=breaker,
        clock=clock,
        result_store=result_store,
        object_store=object_store,
        sleep=_instant_sleep,
    )
    runner.register_adapter(connector_id, ConnectorMode.SIMULATOR, adapter)
    kernel = AcceptAllKernelHandoff()
    handler = SimulatorWebhookHandler(connector_id, signing_key, clock=clock)
    pipeline = WebhookPipeline(
        clock=clock,
        kernel=kernel,
        handlers={connector_id: handler},
        result_store=result_store,
        object_store=object_store,
    )
    engine.bind_callback_sink(pipeline.ingest)
    return SimulatorCertEnvironment(
        connector_id=connector_id,
        clock=clock,
        registry=registry,
        breaker=breaker,
        runner=runner,
        engine=engine,
        adapter=adapter,
        pipeline=pipeline,
        handler=handler,
        kernel=kernel,
        object_store=object_store,
        result_store=result_store,
        signing_key=signing_key,
    )


class _LogCapture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


def _check(check_id: str, name: str, passed: bool, evidence: dict) -> dict:
    return {
        "check_id": check_id,
        "name": name,
        "verdict": "PASSED" if passed else "FAILED",
        "evidence_hash": sha256_canonical(evidence),
    }


def _rfc3339(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class CertificationHarness:
    """Executes the conformance suite and persists the CertificationRun."""

    def __init__(
        self,
        connector_id: str,
        environment_factory,
        *,
        clock: Clock,
        run_store: CertificationRunStore | None = None,
        object_store: ObjectStore | None = None,
        audit: AuditSink | None = None,
        events: EventSink | None = None,
        adapter_version: str = "1.0.0",
        mode_under_test: str = "SIMULATOR",
        triggered_by: str = "operator:certify",
    ) -> None:
        self._connector_id = connector_id
        self._factory = environment_factory
        self._clock = clock
        self._runs = run_store if run_store is not None else InMemoryCertificationRunStore()
        self._objects = object_store if object_store is not None else InMemoryObjectStore()
        self._audit = audit if audit is not None else InMemoryAuditSink()
        self._events = events if events is not None else InMemoryEventSink()
        self._adapter_version = adapter_version
        self._mode_under_test = mode_under_test
        self._triggered_by = triggered_by

    @property
    def run_store(self) -> CertificationRunStore:
        return self._runs

    # -- suite ------------------------------------------------------------------

    async def run(self) -> CertificationRunRecord:
        started_at = self._clock.now()
        record = CertificationRunRecord(
            run_id=make_connector_id(
                "certr",
                {
                    "connector_id": self._connector_id,
                    "adapter_version": self._adapter_version,
                    "started_at": started_at,
                },
            ),
            connector_id=self._connector_id,
            adapter_version=self._adapter_version,
            mode_under_test=self._mode_under_test,
            triggered_by=self._triggered_by,
            created_at=started_at,
        )
        self._runs.save(record)
        record.status = "RUNNING"
        record.started_at = started_at
        self._runs.save(record)
        try:
            checks_one = await self._run_checks(self._factory())
            checks_two = await self._run_checks(self._factory())
            bytes_one = canonical_json({"checks": checks_one})
            bytes_two = canonical_json({"checks": checks_two})
            checks = list(checks_one)
            checks.append(
                _check(
                    "CERT-10",
                    "determinism",
                    bytes_one == bytes_two,
                    {
                        "run_one_hash": sha256_canonical({"checks": checks_one}),
                        "run_two_hash": sha256_canonical({"checks": checks_two}),
                    },
                )
            )
            report = {
                "connector_id": self._connector_id,
                "adapter_version": self._adapter_version,
                "mode_under_test": self._mode_under_test,
                "checks": checks,
            }
            report_bytes = canonical_json(report)
            record.checks = checks
            record.report_hash = sha256_canonical(report)
            record.report_pointer = self._objects.put(
                f"certreports/{record.run_id}", report_bytes
            )
            record.status = (
                "PASSED" if all(c["verdict"] == "PASSED" for c in checks) else "FAILED"
            )
        except Exception as exc:
            record.checks = [
                {
                    "check_id": "SUITE",
                    "name": "suite_execution",
                    "verdict": "ERRORED",
                    "evidence_hash": sha256_canonical({"error_type": type(exc).__name__}),
                }
            ]
            record.status = "ERRORED"
        record.finished_at = self._clock.now()
        self._runs.save(record)
        self._audit.record(
            "CONNECTOR_CERTIFICATION_COMPLETED",
            actor_id=self._triggered_by,
            occurred_at=record.finished_at,
            detail={
                "run_id": record.run_id,
                "connector_id": self._connector_id,
                "status": record.status,
            },
        )
        self._events.emit(
            "certification_run.completed",
            {
                "run_id": record.run_id,
                "connector_id": self._connector_id,
                "status": record.status,
                "report_hash": record.report_hash,
            },
        )
        return record

    # -- individual checks ---------------------------------------------------------

    def _instruction(
        self, env: SimulatorCertEnvironment, scenario: str, n: int = 1
    ) -> PaymentInstruction:
        connector_ref = f"certref-{scenario}-{n}"
        return PaymentInstruction(
            instruction_id=make_id("ins", {"connector_ref": connector_ref}),
            connector_ref=connector_ref,
            amount=Money(amount_minor=10_100),
            method="BKASH",
            sender_ref="tok-sender",
            beneficiary_ref="tok-beneficiary",
            rail=env.connector_id,
            instruction_at=_rfc3339(env.clock.now()),
            metadata={"sim_scenario": scenario},
        )

    async def _run_checks(self, env: SimulatorCertEnvironment) -> list[dict]:
        capture = _LogCapture()
        root = logging.getLogger()
        root.addHandler(capture)
        try:
            checks: list[dict] = []
            checks.append(await self._cert_01(env))
            checks.append(await self._cert_02(env))
            checks.append(self._cert_03(env))
            scenario_evidence = await self._cert_08_exercise(env)
            checks.append(self._cert_05(env))
            checks.append(await self._cert_06(env))
            checks.append(await self._cert_07(env))
            checks.append(scenario_evidence)
            checks.append(self._cert_09(env))
            checks.append(self._cert_04(env, capture.lines))
            return sorted(checks, key=lambda c: c["check_id"])
        finally:
            root.removeHandler(capture)

    async def _cert_01(self, env: SimulatorCertEnvironment) -> dict:
        # Runner path: replay through the dispatch idempotency record.
        instruction = self._instruction(env, "success", 1)
        first = await env.runner.dispatch(instruction)
        second = await env.runner.dispatch(instruction)
        runner_ok = (
            (first.status, first.rail_transaction_id)
            == (second.status, second.rail_transaction_id)
            and env.engine.effect_count(instruction.connector_ref) == 1
        )
        # Adapter path: the CONNECTOR itself must dedupe on connector_ref.
        direct = self._instruction(env, "success", 3)
        direct_first = await env.adapter.submit(direct)
        direct_second = await env.adapter.submit(direct)
        adapter_ok = (
            (direct_first.status, direct_first.rail_transaction_id)
            == (direct_second.status, direct_second.rail_transaction_id)
            and env.engine.effect_count(direct.connector_ref) == 1
        )
        passed = runner_ok and adapter_ok
        return _check(
            "CERT-01",
            "idempotency",
            passed,
            {
                "first_status": first.status.value,
                "second_status": second.status.value,
                "effects": env.engine.effect_count(instruction.connector_ref),
                "adapter_first_status": direct_first.status.value,
                "adapter_second_status": direct_second.status.value,
                "adapter_effects": env.engine.effect_count(direct.connector_ref),
            },
        )

    async def _cert_02(self, env: SimulatorCertEnvironment) -> dict:
        instruction = self._instruction(env, "success", 2)
        result = await env.runner.dispatch(instruction)
        passed = (
            result.instruction_id == instruction.instruction_id
            and result.connector_ref == instruction.connector_ref
        )
        return _check(
            "CERT-02",
            "echo",
            passed,
            {"instruction_id_ok": passed, "connector_ref": instruction.connector_ref},
        )

    def _adapter_module_tree(self, env: SimulatorCertEnvironment) -> ast.Module:
        module = sys.modules[type(env.adapter).__module__]
        return ast.parse(inspect.getsource(module))

    def _cert_03(self, env: SimulatorCertEnvironment) -> dict:
        violations: list[str] = []
        for node in ast.walk(self._adapter_module_tree(env)):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                if any(
                    name == root or name.startswith(root + ".")
                    for root in _FORBIDDEN_IMPORT_ROOTS
                ):
                    violations.append(name)
        ctor_violations = constructor_db_handle_violations(env.adapter)
        return _check(
            "CERT-03",
            "no_business_db_writes",
            not violations and not ctor_violations,
            {
                "import_violations": sorted(violations),
                "constructor_db_params": ctor_violations,
            },
        )

    def _cert_04(self, env: SimulatorCertEnvironment, log_lines: list[str]) -> dict:
        recompute_failures = 0
        raw_texts: list[str] = []
        rows = env.result_store.list_all()
        for row in rows:
            if not row.raw_response_hash:
                recompute_failures += 1
                continue
            raw_bytes = env.object_store.get(row.raw_response_pointer)
            raw_texts.append(raw_bytes.decode("utf-8"))
            if sha256_canonical(json.loads(raw_bytes)) != row.raw_response_hash:
                recompute_failures += 1
        log_blob = "\n".join(log_lines)
        leaked = 0
        for text in raw_texts:
            for index in range(0, max(len(text) - 15, 0)):
                if text[index : index + 16] in log_blob:
                    leaked += 1
                    break
        passed = recompute_failures == 0 and leaked == 0 and len(rows) > 0
        return _check(
            "CERT-04",
            "raw_response_hash",
            passed,
            {"rows": len(rows), "recompute_failures": recompute_failures, "leaked": leaked},
        )

    def _cert_05(self, env: SimulatorCertEnvironment) -> dict:
        allowed_statuses = {status.value for status in ConnectorStatus}
        allowed_codes = set(env.engine.error_taxonomy()) | set(_RUNNER_SYNTHETIC_CODES)
        bad_statuses: list[str] = []
        bad_codes: list[str] = []
        for row in env.result_store.list_all():
            if row.status not in allowed_statuses:
                bad_statuses.append(row.status)
            if row.error_code is not None and row.error_code not in allowed_codes:
                bad_codes.append(row.error_code)
        passed = not bad_statuses and not bad_codes
        return _check(
            "CERT-05",
            "status_taxonomy",
            passed,
            {"bad_statuses": sorted(bad_statuses), "bad_codes": sorted(bad_codes)},
        )

    async def _cert_06(self, env: SimulatorCertEnvironment) -> dict:
        instruction = self._instruction(env, "timeout", 1)
        registration = env.registry.get(env.connector_id)
        loop = asyncio.get_running_loop()
        tasks_before = len([t for t in asyncio.all_tasks(loop) if not t.done()])
        elapsed_start = loop.time()
        result = await env.runner.dispatch(instruction)
        elapsed_ms = (loop.time() - elapsed_start) * 1000
        tasks_after = len([t for t in asyncio.all_tasks(loop) if not t.done()])
        within_grace = elapsed_ms <= registration.timeout_ms + 500
        passed = (
            result.status is ConnectorStatus.TIMED_OUT
            and within_grace
            and tasks_before == tasks_after
        )
        return _check(
            "CERT-06",
            "timeout_honor",
            passed,
            {
                "status": result.status.value,
                "within_grace": within_grace,
                "task_leak": tasks_after - tasks_before,
            },
        )

    async def _cert_07(self, env: SimulatorCertEnvironment) -> dict:
        payload = {
            "instruction_id": make_id("ins", {"connector_ref": "certref-webhook-1"}),
            "connector_ref": "certref-webhook-1",
            "status": "success",
            "rail_transaction_id": "SIM-CERT07",
            "responded_at": _rfc3339(env.clock.now()),
            "error_code": None,
        }
        body = canonical_json(payload)
        timestamp = _rfc3339(env.clock.now())
        valid_headers = sign_callback(env.signing_key, body, timestamp)
        valid = await env.pipeline.ingest(env.connector_id, valid_headers, body)
        redelivery = await env.pipeline.ingest(env.connector_id, valid_headers, body)
        tampered_body = body + b" "
        tampered = await env.pipeline.ingest(env.connector_id, valid_headers, tampered_body)
        missing = await env.pipeline.ingest(
            env.connector_id, {TIMESTAMP_HEADER: timestamp}, body + b"  "
        )
        stale_payload = dict(payload, rail_transaction_id="SIM-CERT07-STALE")
        stale_body = canonical_json(stale_payload)
        stale_ts = _rfc3339(
            env.clock.now().replace(hour=(env.clock.now().hour + 2) % 24)
        )
        stale_headers = sign_callback(env.signing_key, stale_body, stale_ts)
        stale = await env.pipeline.ingest(env.connector_id, stale_headers, stale_body)
        dispositions = {
            "valid": valid.status,
            "redelivery": redelivery.status,
            "tampered": tampered.status,
            "missing_signature": missing.status,
            "stale_timestamp": stale.status,
        }
        passed = dispositions == {
            "valid": "PROCESSED",
            "redelivery": "DUPLICATE",
            "tampered": "REJECTED",
            "missing_signature": "REJECTED",
            "stale_timestamp": "REJECTED",
        }
        return _check("CERT-07", "webhook_fail_closed", passed, dispositions)

    async def _cert_08_exercise(self, env: SimulatorCertEnvironment) -> dict:
        present = {s.name for s in env.engine.scenarios if s.enabled}
        missing = [n for n in BASELINE_SCENARIO_NAMES if n not in present]
        observed: dict[str, str] = {}
        decline = await env.runner.dispatch(self._instruction(env, "decline", 1))
        observed["decline"] = decline.status.value
        pending = await env.runner.dispatch(self._instruction(env, "pending_then_success", 1))
        observed["pending_then_success"] = pending.status.value
        reversal_ins = self._instruction(env, "reversal", 1)
        reversal_submit = await env.runner.dispatch(reversal_ins)
        reversal = await env.runner.reverse(
            env.connector_id,
            reversal_ins.connector_ref,
            reversal_submit.rail_transaction_id,
            Money(amount_minor=10_100),
            "cert reversal",
        )
        observed["reversal"] = reversal.status.value
        duplicate = await env.runner.dispatch(self._instruction(env, "duplicate_callback", 1))
        observed["duplicate_callback"] = duplicate.status.value
        env.clock.advance(10)
        await env.engine.deliver_due_callbacks()
        webhook_statuses = sorted(
            row.status for row in env.pipeline.inbound_store.list_all()
        )
        passed = (
            not missing
            and observed["decline"] == "rejected"
            and observed["pending_then_success"] == "pending"
            and observed["reversal"] == "reversed"
            and observed["duplicate_callback"] == "pending"
            and "PROCESSED" in webhook_statuses
        )
        return _check(
            "CERT-08",
            "scenario_coverage",
            passed,
            {
                "missing": missing,
                "observed": observed,
                "webhook_statuses": webhook_statuses,
            },
        )

    def _cert_09(self, env: SimulatorCertEnvironment) -> dict:
        violations: list[str] = []
        for node in ast.walk(self._adapter_module_tree(env)):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "float"
            ):
                arg_source = " ".join(ast.unparse(arg) for arg in node.args)
                if "amount" in arg_source:
                    violations.append(arg_source)
        return _check(
            "CERT-09",
            "money_type",
            not violations,
            {"violations": sorted(violations)},
        )


# -- machine-readable certification matrix --------------------------------------


def certification_matrix(records: list[CertificationRunRecord]) -> dict:
    """Connector x check matrix from certification runs (machine-readable).

    One entry per connector_id (the latest record in list order wins);
    ``checks`` maps every check_id to its verdict; ``status`` is the run
    verdict (PASSED/FAILED/ERRORED). Serializes to JSON via canonical_json.
    """
    connectors: dict[str, dict] = {}
    for record in records:
        connectors[record.connector_id] = {
            "run_id": record.run_id,
            "adapter_version": record.adapter_version,
            "mode_under_test": record.mode_under_test,
            "status": record.status,
            "report_hash": record.report_hash,
            "checks": {check["check_id"]: check["verdict"] for check in record.checks},
        }
    return {
        "schema_version": 1,
        "matrix_kind": "connector_certification",
        "connectors": connectors,
    }


def write_certification_matrix(
    object_store: ObjectStore,
    records: list[CertificationRunRecord],
    *,
    key: str = "certification/matrix",
) -> tuple[str, bytes]:
    """Archive the certification matrix as canonical JSON bytes; returns
    ``(object_key, json_bytes)``."""
    payload = canonical_json(certification_matrix(records))
    object_store.put(key, payload)
    return key, payload

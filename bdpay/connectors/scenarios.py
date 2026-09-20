"""SimulatorScenario documents + deterministic match/select rules (spec/10 DSL).

Determinism rules (binding): no randomness anywhere; all branching derives
from ``sha256(connector_ref)``; delays execute via the injected clock/sleep.
Selection resolution pinned by test: the spec orders matching by name with
``"success"`` as the mandatory fallback — since the baseline ``success``
scenario matches everything, selection considers non-``success`` scenarios in
name order first and falls back to ``success`` last (otherwise names sorting
after "success" would be unreachable).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime

from bdpay.connectors.ids_ext import make_connector_id
from bdpay.connectors.sdk import PaymentInstruction

__all__ = [
    "BASELINE_SCENARIO_NAMES",
    "EXTENDED_SCENARIO_NAMES",
    "SimulatorScenario",
    "STEP_VOCABULARY",
    "build_baseline_scenarios",
    "build_extended_scenarios",
    "make_scenario",
    "matches",
    "ref_hash",
    "select_scenario",
]

#: Closed step vocabulary (spec/10 Simulator DSL).
STEP_VOCABULARY = frozenset(
    {"respond", "callback", "duplicate_callback", "timeout", "reversal_ack", "drop_then_succeed"}
)

#: Mandatory baseline set every connector ships (CERT-08 asserts presence).
BASELINE_SCENARIO_NAMES = (
    "decline",
    "duplicate_callback",
    "pending_then_success",
    "reversal",
    "success",
    "timeout",
)

#: Additive timeout-settlement scenarios (spec/10 allows rail-specific
#: additions; SPEC_ERRATA-LANE-B LB4): the rail processed the instruction but
#: the synchronous response was lost in transit.
EXTENDED_SCENARIO_NAMES = (
    "timeout_then_reversal",
    "timeout_then_success",
)

_MATCH_KEYS = frozenset(
    {
        "amount_minor_in",
        "amount_minor_mod",
        "metadata_equals",
        "beneficiary_ref_prefix",
        "ref_hash_bucket",
    }
)


@dataclass(frozen=True)
class SimulatorScenario:
    """One ``simulator_scenarios`` row: ``{match_rules, script}``."""

    scenario_id: str
    connector_id: str
    name: str
    version: int
    match_rules: dict
    script: tuple[dict, ...]
    enabled: bool
    created_by: str
    created_at: datetime
    schema_version: int = 1


def make_scenario(
    connector_id: str,
    name: str,
    *,
    match_rules: dict,
    script: list[dict] | tuple[dict, ...],
    created_at: datetime,
    created_by: str = "system",
    version: int = 1,
    enabled: bool = True,
) -> SimulatorScenario:
    for key in match_rules:
        if key not in _MATCH_KEYS:
            raise ValueError(f"unknown match rule {key!r} (closed DSL)")
    for step in script:
        if step.get("step") not in STEP_VOCABULARY:
            raise ValueError(f"unknown script step {step.get('step')!r} (closed DSL)")
    return SimulatorScenario(
        scenario_id=make_connector_id(
            "simsc", {"connector_id": connector_id, "name": name, "version": version}
        ),
        connector_id=connector_id,
        name=name,
        version=version,
        match_rules=dict(match_rules),
        script=tuple(dict(step) for step in script),
        enabled=enabled,
        created_by=created_by,
        created_at=created_at,
    )


def ref_hash(connector_ref: str) -> str:
    """sha256 hexdigest of the connector_ref — the ONLY branching source."""
    return hashlib.sha256(connector_ref.encode("utf-8")).hexdigest()


def matches(scenario: SimulatorScenario, instruction: PaymentInstruction) -> bool:
    """True when ALL match_rules hold for the instruction (empty rules match)."""
    rules = scenario.match_rules
    amounts = rules.get("amount_minor_in")
    if amounts is not None and instruction.amount.amount_minor not in amounts:
        return False
    mod = rules.get("amount_minor_mod")
    if mod is not None and (
        instruction.amount.amount_minor % int(mod["divisor"]) != int(mod["remainder"])
    ):
        return False
    metadata_equals = rules.get("metadata_equals")
    if metadata_equals is not None:
        for key, value in metadata_equals.items():
            if instruction.metadata.get(key) != value:
                return False
    prefix = rules.get("beneficiary_ref_prefix")
    if prefix is not None and not instruction.beneficiary_ref.startswith(prefix):
        return False
    bucket = rules.get("ref_hash_bucket")
    if bucket is not None:
        value = int(ref_hash(instruction.connector_ref), 16) % int(bucket["buckets"])
        if value not in bucket["in"]:
            return False
    return True


def select_scenario(
    scenarios: list[SimulatorScenario], instruction: PaymentInstruction
) -> SimulatorScenario:
    """First enabled matching scenario by name; ``success`` is the fallback."""
    enabled = sorted((s for s in scenarios if s.enabled), key=lambda s: s.name)
    for scenario in enabled:
        if scenario.name == "success":
            continue
        if matches(scenario, instruction):
            return scenario
    for scenario in enabled:
        if scenario.name == "success" and matches(scenario, instruction):
            return scenario
    raise LookupError(
        "no scenario matched and no 'success' fallback exists "
        f"for connector {instruction.rail}"
    )


def build_baseline_scenarios(
    connector_id: str, *, created_at: datetime, created_by: str = "system"
) -> list[SimulatorScenario]:
    """The six mandatory baseline scenarios (spec/10, asserted by CERT-08)."""

    def scenario(name: str, script: list[dict], match_rules: dict | None = None):
        rules = (
            {"metadata_equals": {"sim_scenario": name}} if match_rules is None else match_rules
        )
        return make_scenario(
            connector_id,
            name,
            match_rules=rules,
            script=script,
            created_at=created_at,
            created_by=created_by,
        )

    return [
        scenario(
            "success",
            [
                {
                    "step": "respond",
                    "status": "success",
                    "delay_ms": 0,
                    "rail_transaction_id": "SIM-{ref_hash8}",
                }
            ],
            match_rules={},  # mandatory fallback: matches everything
        ),
        scenario(
            "pending_then_success",
            [
                {
                    "step": "respond",
                    "status": "pending",
                    "delay_ms": 0,
                    "rail_transaction_id": "SIM-{ref_hash8}",
                },
                {
                    "step": "callback",
                    "after_ms": 3000,
                    "status": "success",
                    "rail_transaction_id": "SIM-{ref_hash8}",
                    "sign": True,
                },
            ],
        ),
        scenario(
            "decline",
            [
                {
                    "step": "respond",
                    "status": "rejected",
                    "delay_ms": 0,
                    "error_code": "sim_insufficient_funds",
                }
            ],
        ),
        scenario("timeout", [{"step": "timeout"}]),
        scenario(
            "reversal",
            [
                {
                    "step": "respond",
                    "status": "success",
                    "delay_ms": 0,
                    "rail_transaction_id": "SIM-{ref_hash8}",
                },
                {"step": "reversal_ack", "status": "reversed"},
                {
                    "step": "callback",
                    "after_ms": 5000,
                    "status": "reversed",
                    "rail_transaction_id": "SIM-{ref_hash8}",
                    "sign": True,
                },
            ],
        ),
        scenario(
            "duplicate_callback",
            [
                {
                    "step": "respond",
                    "status": "pending",
                    "delay_ms": 0,
                    "rail_transaction_id": "SIM-{ref_hash8}",
                },
                {
                    "step": "callback",
                    "after_ms": 3000,
                    "status": "success",
                    "rail_transaction_id": "SIM-{ref_hash8}",
                    "sign": True,
                },
                {"step": "duplicate_callback", "count": 2, "after_ms": 5000},
            ],
        ),
    ]


def build_extended_scenarios(
    connector_id: str, *, created_at: datetime, created_by: str = "system"
) -> list[SimulatorScenario]:
    """Timeout-settlement scenarios (additive; SPEC_ERRATA-LANE-B LB4).

    ``timeout_then_success``: the rail processed the instruction (truth =
    success, one rail-side effect) but the response was lost — the runner
    observes TIMED_OUT and a status poll finds the recorded success.
    ``timeout_then_reversal``: the rail holds the instruction unresolved
    (truth = pending) and never confirms — polls stay non-definitive, so the
    poll-then-reverse path issues a reversal which the rail acknowledges.
    """

    def scenario(name: str, script: list[dict]) -> SimulatorScenario:
        return make_scenario(
            connector_id,
            name,
            match_rules={"metadata_equals": {"sim_scenario": name}},
            script=script,
            created_at=created_at,
            created_by=created_by,
        )

    return [
        scenario(
            "timeout_then_success",
            [
                {
                    "step": "respond",
                    "status": "success",
                    "delay_ms": 0,
                    "rail_transaction_id": "SIM-{ref_hash8}",
                },
                {"step": "timeout"},
            ],
        ),
        scenario(
            "timeout_then_reversal",
            [
                {"step": "reversal_ack", "status": "reversed"},
                {
                    "step": "respond",
                    "status": "pending",
                    "delay_ms": 0,
                    "rail_transaction_id": "SIM-{ref_hash8}",
                },
                {"step": "timeout"},
            ],
        ),
    ]

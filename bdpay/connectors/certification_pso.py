"""PSO certification battery — PSO-CERT-01..10 + the founding live-demo profile.

spec/19 workstream PSO-4 (owning package ``connectors``, "certification
extension") plus the PSO-5 executable live-demo scenario runner. The battery
is the platform-level scenario suite gating PSO readiness: N simulated
participants across a full window lifecycle, a live Net-Debit-Cap breach
refusal mid-window, a participant failure mid-window, netting-instruction
correctness against a hand-computed fixture, byte-parity replay of a disputed
window, and the four-layer PLATFORM_MODE gating proof. Results land in their
own ``pso_certification_runs`` shape (spec/19 0104 DDL) — platform-scoped,
alongside (never inside) the per-connector ``certification_runs`` suite.

Architecture seam (binding package rule, arch §"Package-boundary"): the
connectors package never imports ``bdpay.kernel`` / ``bdpay.ledger``. The
battery therefore drives the platform through the typed
:class:`PsoPlatformBench` protocol; the composition root (and the test kit)
builds the bench over the REAL netting engine, position service, onboarding
service, and replay service, exactly the way the spec/10 harness takes an
``environment_factory``. Every assertion lives HERE; the bench only executes
primitives and reports facts.

FSM (spec/19 FSM 5, conventions §8, refusal-first — any pair not listed is
DENIED)::

    QUEUED  --[runner_pickup]--> RUNNING
    RUNNING --[checks_passed]--> PASSED   (terminal)
    RUNNING --[check_failed]---> FAILED   (terminal)
    RUNNING --[errored]--------> ERRORED  (terminal)

Determinism contract (PSO-CERT-10, CERT-10 class): the runner executes the
whole suite twice on freshly built benches and the two check lists must be
byte-identical under canonical JSON; the sealed report contains no clock
reads and no run ids, so two consecutive battery runs produce byte-identical
reports (hash equality pinned by test). Under ``scenario_profile="DEMO"`` the
SAME runner additionally executes the eight live-demo steps from the tracked
``demo-pso-founding-v1`` scenario document — the demo IS a battery run, so
demo drift from the battery is impossible by construction (spec/19 failure
mode table).

Mode gate (spec/19 §Scope, binding): :meth:`PsoCertificationBattery.run`
raises :class:`PsoModeError` under ``PLATFORM_MODE=PSP`` BEFORE any record is
read or written; the error is never caught-and-continued. PSO-CERT-07 runs
the PSP refusal probe from a PSO-mode battery against a PSP-mode bench twin.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from bdpay.connectors.ports import (
    AuditSink,
    EventSink,
    InMemoryAuditSink,
    InMemoryEventSink,
    InMemoryObjectStore,
    ObjectStore,
)
from bdpay.connectors.registry import ConnectorMode, ConnectorRegistry
from bdpay.connectors.scenarios import build_baseline_scenarios, build_extended_scenarios
from bdpay.connectors.simulator import ScenarioEngine, SimulatorConnector
from bdpay.platform.canonical import canonical_json, sha256_canonical
from bdpay.platform.clock import Clock
from bdpay.platform.errors import AuthorizationError
from bdpay.platform.ids import HASH_LENGTH

__all__ = [
    "DEMO_SCRIPT_NAME",
    "DEMO_STEP_NAMES",
    "DEMO_TRANSACTION_COUNT",
    "DEMO_WINDOW_DATE",
    "DEMO_WINDOW_SESSION",
    "GOLDEN_DISPUTE_FIXTURE",
    "GOLDEN_EXPECTED_OBLIGATIONS",
    "GOLDEN_TOTAL_GROSS",
    "GOLDEN_TOTAL_NET_PAY",
    "PSO_CERTIFICATION_TRANSITIONS",
    "PSO_CERT_CHECK_IDS",
    "PSO_CERT_SUITE_VERSION",
    "RESULT_ANCHOR_KEYS",
    "SCENARIO_PROFILES",
    "SIMULATED_PARTICIPANT_CONNECTOR_IDS",
    "DemoScriptError",
    "InMemoryPsoCertificationRunStore",
    "PsoCertificationBattery",
    "PsoCertificationError",
    "PsoCertificationRunRecord",
    "PsoCertificationRunStore",
    "PsoCertificationTransitionError",
    "PsoModeError",
    "PsoPlatformBench",
    "check_matrix",
    "derive_demo_transfers",
    "fixture_transfers_from_positions",
    "make_pso_certification_id",
    "pso_certification_matrix",
    "register_simulated_participants",
    "validate_demo_script",
]

PSO_CERT_SUITE_VERSION = "pso-cert-v1"
SCENARIO_PROFILES: tuple[str, ...] = ("CERT", "DEMO")
PSO_CERT_CHECK_IDS: tuple[str, ...] = tuple(f"PSO-CERT-{i:02d}" for i in range(1, 11))

_CHECK_NAMES: dict[str, str] = {
    "PSO-CERT-01": "full_window_lifecycle",
    "PSO-CERT-02": "ndc_breach_refusal_mid_window",
    "PSO-CERT-03": "participant_failure_mid_window",
    "PSO-CERT-04": "settlement_failure_unwind",
    "PSO-CERT-05": "netting_golden_vector",
    "PSO-CERT-06": "replay_byte_parity",
    "PSO-CERT-07": "platform_mode_gating",
    "PSO-CERT-08": "seeded_order_independence",
    "PSO-CERT-09": "conformance_bidirectionality",
    "PSO-CERT-10": "battery_determinism",
}

#: PSO-CERT-01 floor (spec/19: ">= 1,000 deterministic transactions").
CERT_PROFILE_TRANSACTION_COUNT = 1_000

#: The five simulated founding participants (spec/19 battery section):
#: spec/10 ``connector_registry`` rows, protocol PAYMENT, mode SIMULATOR,
#: ONE adapter class (the spec/10 ``SimulatorConnector``).
SIMULATED_PARTICIPANT_CONNECTOR_IDS: tuple[str, ...] = tuple(
    f"pso_participant_sim_{i:02d}_v1" for i in range(1, 6)
)

#: Order-dependent chain-attestation fields of the netting result document.
#: PSO-CERT-08 asserts order independence over the ANCHOR-STRIPPED core
#: (errata N19-3/R19-3, adopted here as C19-2): the anchors attest a specific
#: insert order by design of the hash chain.
RESULT_ANCHOR_KEYS: tuple[str, ...] = ("chain_index_at_compute", "chain_hash_at_compute")

DEMO_SCRIPT_NAME = "demo-pso-founding-v1"

#: The eight step names of the tracked PSO-5 scenario artifact
#: ``docs/founding-participant/demo-pso-founding-v1.json`` (owned by the
#: PSO-5 pack lane; this runner EXECUTES it, never rewrites it).
DEMO_STEP_NAMES: tuple[str, ...] = (
    "register_and_activate_participants",
    "open_demo_window",
    "submit_10000_deterministic_transactions",
    "live_ndc_refusal",
    "cutoff_and_netting",
    "instruct_and_confirm_rtgs",
    "close_and_recon",
    "byte_identical_window_replay",
)
DEMO_TRANSACTION_COUNT = 10_000
DEMO_PARTICIPANT_COUNT = 5  # FOUNDING_PARTICIPANT_TARGET_COUNT default

#: The runner-chosen demo execution window (the artifact pins the AM session
#: but no calendar date — the bench clock is injected, so the date is a
#: deterministic runner constant; a BB working day).
DEMO_WINDOW_DATE = "2026-06-15"
DEMO_WINDOW_SESSION = "AM"

_DNS_SESSIONS = ("AM", "PM", "EOD")

# ---------------------------------------------------------------------------
# Hand-computed golden vector (PSO-CERT-05; integer paisa)
#
#   transfers (payer -> payee):
#     a->b 3,000,000   b->c 1,200,000   c->a   800,000   d->b   600,000
#     e->d 1,500,000   b->e   400,000   a->e   250,000
#   nets: a = -3,000,000 +800,000 -250,000              = -2,450,000  PAY
#         b = +3,000,000 -1,200,000 +600,000 -400,000   = +2,000,000  RECEIVE
#         c = +1,200,000 -800,000                       =   +400,000  RECEIVE
#         d =   -600,000 +1,500,000                     =   +900,000  RECEIVE
#         e = -1,500,000 +400,000 +250,000              =   -850,000  PAY
#   zero-sum: -2,450,000 +2,000,000 +400,000 +900,000 -850,000 == 0
#   total_net_pay = 2,450,000 + 850,000 = 3,300,000
#   total_gross   = 2 x 7,750,000 = 15,500,000 (each transfer = 2 position legs)
# ---------------------------------------------------------------------------

GOLDEN_PARTICIPANTS: tuple[str, ...] = (
    "part_pso_golden_a",
    "part_pso_golden_b",
    "part_pso_golden_c",
    "part_pso_golden_d",
    "part_pso_golden_e",
)
_GA, _GB, _GC, _GD, _GE = GOLDEN_PARTICIPANTS

#: (from, to, amount_minor, ref)
GOLDEN_TRANSFERS: tuple[tuple[str, str, int, str], ...] = (
    (_GA, _GB, 3_000_000, "pi_pso_g1"),
    (_GB, _GC, 1_200_000, "pi_pso_g2"),
    (_GC, _GA, 800_000, "pi_pso_g3"),
    (_GD, _GB, 600_000, "pi_pso_g4"),
    (_GE, _GD, 1_500_000, "pi_pso_g5"),
    (_GB, _GE, 400_000, "pi_pso_g6"),
    (_GA, _GE, 250_000, "pi_pso_g7"),
)

#: participant -> (direction, obligation_minor, net_position_minor)
GOLDEN_EXPECTED_OBLIGATIONS: dict[str, tuple[str, int, int]] = {
    _GA: ("PAY", 2_450_000, -2_450_000),
    _GB: ("RECEIVE", 2_000_000, 2_000_000),
    _GC: ("RECEIVE", 400_000, 400_000),
    _GD: ("RECEIVE", 900_000, 900_000),
    _GE: ("PAY", 850_000, -850_000),
}
GOLDEN_TOTAL_NET_PAY = 3_300_000
GOLDEN_TOTAL_GROSS = 15_500_000
GOLDEN_CAP_MINOR = 100_000_000  # BDT 10 lakh in paisa — ample headroom
GOLDEN_NET_DEBTOR = _GA

#: The disputed-window fixture shape shared by PSO-CERT-06 and the demo
#: script's step 8 (the script ships its own tracked copy of these values).
GOLDEN_DISPUTE_FIXTURE: dict[str, Any] = {
    "window": {"window_date": "2026-06-08", "dns_session": "AM"},
    "transfers": [
        {"from": f, "to": t, "amount_minor": a, "ref": r} for f, t, a, r in GOLDEN_TRANSFERS
    ],
}


# ---------------------------------------------------------------------------
# Errors + the pcert id shim (errata C19-1, E18 class)
# ---------------------------------------------------------------------------


class PsoCertificationError(Exception):
    """Battery misuse or invariant violation (refusal-first)."""


class PsoCertificationTransitionError(PsoCertificationError):
    """A (state, trigger) pair outside the spec/19 FSM 5 table (DENIED)."""


class DemoScriptError(PsoCertificationError):
    """The demo scenario document failed shape validation (fail closed)."""


class PsoModeError(AuthorizationError):
    """A PSO-only operation was attempted while PLATFORM_MODE != PSO.

    Same contract as the kernel/ledger lanes' ``PsoModeError`` (errata P19-9
    class): subclasses the platform ``AuthorizationError`` so the engine-layer
    refusal and the binding ``403 authorization / platform_mode_pso_required``
    envelope are the same object. Consolidates into ``bdpay.platform`` at the
    prefix/exception fold-in.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message, code="platform_mode_pso_required")


_PCERT_PREFIX = "pcert"


def make_pso_certification_id(payload: Mapping[str, Any]) -> str:
    """``pcert_<sha256_canonical(payload)[:24]>`` — spec/19 Entities scheme.

    Format-parity shim (errata C19-1, E18/E28/E37 class): ``pcert`` is not in
    the frozen ``bdpay.platform.ids.PREFIXES`` table nor in the spec/10
    ``CONNECTOR_PREFIXES`` shim (another workstream's registration list), so
    this module mints it over the ONE binding E12 canonical implementation.
    Collapses to plain ``make_id`` at the prefix fold-in without changing any
    stored id.
    """
    digest = sha256_canonical(dict(payload))
    return f"{_PCERT_PREFIX}_{digest[:HASH_LENGTH]}"


# ---------------------------------------------------------------------------
# FSM 5 + the pso_certification_runs record/store (spec/19 0104 DDL shape)
# ---------------------------------------------------------------------------

#: (from_state, trigger) -> to_state. Refusal-first: anything else is DENIED.
PSO_CERTIFICATION_TRANSITIONS: dict[tuple[str, str], str] = {
    ("QUEUED", "runner_pickup"): "RUNNING",
    ("RUNNING", "checks_passed"): "PASSED",
    ("RUNNING", "check_failed"): "FAILED",
    ("RUNNING", "errored"): "ERRORED",
}

PSO_CERTIFICATION_TERMINAL_STATES: frozenset[str] = frozenset({"PASSED", "FAILED", "ERRORED"})


@dataclass
class PsoCertificationRunRecord:
    """One ``pso_certification_runs`` row (append-only; FSM advances status)."""

    pso_certification_run_id: str
    suite_version: str
    scenario_profile: str
    platform_mode: str
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
class PsoCertificationRunStore(Protocol):
    """Persistence port for battery runs (``pso_certification_runs``)."""

    def save(self, record: PsoCertificationRunRecord) -> None: ...

    def get(self, run_id: str) -> PsoCertificationRunRecord | None: ...

    def list_for(self, suite_version: str) -> list[PsoCertificationRunRecord]: ...


class InMemoryPsoCertificationRunStore:
    """Deterministic in-memory store (insertion-ordered)."""

    def __init__(self) -> None:
        self._by_id: dict[str, PsoCertificationRunRecord] = {}
        self._order: list[str] = []

    def save(self, record: PsoCertificationRunRecord) -> None:
        if record.pso_certification_run_id not in self._by_id:
            self._order.append(record.pso_certification_run_id)
        self._by_id[record.pso_certification_run_id] = record

    def get(self, run_id: str) -> PsoCertificationRunRecord | None:
        return self._by_id.get(run_id)

    def list_for(self, suite_version: str) -> list[PsoCertificationRunRecord]:
        return [
            self._by_id[run_id]
            for run_id in self._order
            if self._by_id[run_id].suite_version == suite_version
        ]

    def latest_for(self, suite_version: str) -> PsoCertificationRunRecord | None:
        """The newest run for the suite (the §API D readiness-gate read)."""
        rows = self.list_for(suite_version)
        return rows[-1] if rows else None


# ---------------------------------------------------------------------------
# The platform bench protocol (the connectors/kernel/ledger seam)
# ---------------------------------------------------------------------------


@runtime_checkable
class PsoPlatformBench(Protocol):
    """One freshly built deterministic PSO platform bench.

    Implementations compose the REAL engines (ledger + netting engine +
    position service + onboarding service + replay service + the registered
    RTGS simulator seam) behind these primitives. The bench executes and
    reports facts; every PASS/FAIL judgment stays in the battery. All ids,
    clocks, and references must be deterministic: building the bench twice
    and replaying the same calls must yield identical observable facts.
    """

    #: Pre-provisioned ACTIVE participants (ids ASC), with live NDCs and
    #: tokenized settlement-account references.
    participants: tuple[str, ...]

    def provision_participants(
        self, participant_ids: Sequence[str], *, cap_minor: int
    ) -> None: ...

    def open_window(self, *, window_date: str, dns_session: str) -> str: ...

    def window_status(self, window_id: str) -> str: ...

    def post_transfer(
        self,
        window_id: str,
        *,
        from_participant_id: str,
        to_participant_id: str,
        amount_minor: int,
        ref: str,
    ) -> str: ...

    def attempt_transfer(
        self,
        window_id: str,
        *,
        from_participant_id: str,
        to_participant_id: str,
        amount_minor: int,
        ref: str,
    ) -> Mapping[str, Any]: ...

    def attempt_over_cap_debit(
        self,
        window_id: str,
        participant_id: str,
        *,
        floor_amount_minor: int | None = None,
    ) -> Mapping[str, Any]: ...

    def net_position(self, window_id: str, participant_id: str) -> int: ...

    def ndc_breach_count(self, participant_id: str) -> int: ...

    def outbox_count(self, event_type: str) -> int: ...

    def journal_entry_count(self) -> int: ...

    def trial_balance_ok(self) -> bool: ...

    def suspend_participant(self, window_id: str, participant_id: str) -> None: ...

    def cutoff(self, window_id: str) -> None: ...

    def compute_netting(self, window_id: str) -> Mapping[str, Any]: ...

    def netting_run_view(self, netting_run_id: str) -> Mapping[str, Any]: ...

    def netting_obligations(self, window_id: str) -> Sequence[Mapping[str, Any]]: ...

    def netting_result_document(self, window_id: str) -> Mapping[str, Any]: ...

    def netting_result_bytes(self, window_id: str) -> bytes: ...

    def instruct(self, window_id: str) -> None: ...

    async def settle_legs(
        self, window_id: str, *, reject_participant_id: str | None = None
    ) -> Mapping[str, Any]: ...

    def confirm(self, window_id: str) -> str: ...

    def close_window(self, window_id: str) -> Mapping[str, Any]: ...

    def unwind(self, window_id: str, excluded_participant_id: str) -> Mapping[str, Any]: ...

    def replay_window(self, window_id: str) -> Mapping[str, Any]: ...

    async def onboard_via_conformance(self, *, failing: bool) -> Mapping[str, Any]: ...

    async def onboard_and_activate(
        self, connector_id: str, *, cap_minor: int
    ) -> Mapping[str, Any]: ...

    def psp_probe(self) -> Mapping[str, Any]: ...


# ---------------------------------------------------------------------------
# Simulated participants (spec/10 registry rows; one adapter class)
# ---------------------------------------------------------------------------


async def _instant_sleep(_seconds: float) -> None:
    return None


def register_simulated_participants(
    registry: ConnectorRegistry,
    runner: Any | None = None,
    *,
    clock: Clock,
    signing_key: bytes = b"pso-participant-sim-key",
) -> dict[str, SimulatorConnector]:
    """Register ``pso_participant_sim_{01..05}_v1`` as SIMULATOR rows.

    Protocol ``PAYMENT``, mode ``SIMULATOR``, one adapter class
    (:class:`SimulatorConnector`) driven by ``SimulatorScenario`` documents —
    the EXISTING spec/10 machinery, zero new dispatch machinery. The spec/10
    production assert (``ALLOW_SIMULATOR=false`` refuses boot with SIMULATOR
    rows) applies unchanged, so simulated participants are structurally
    impossible in production.
    """
    adapters: dict[str, SimulatorConnector] = {}
    for connector_id in SIMULATED_PARTICIPANT_CONNECTOR_IDS:
        scenarios = build_baseline_scenarios(connector_id, created_at=clock.now())
        scenarios += build_extended_scenarios(connector_id, created_at=clock.now())
        engine = ScenarioEngine(
            connector_id,
            scenarios,
            clock=clock,
            signing_key=signing_key,
            sleep=_instant_sleep,
        )
        adapter = SimulatorConnector(connector_id, engine)
        registry.register(
            connector_id,
            display_name=f"PSO simulated participant ({connector_id})",
            protocol="PAYMENT",
            capabilities=("submit", "query_status", "reverse", "health_check", "webhook"),
            supported_methods=adapter.supported_methods,
            active_mode=ConnectorMode.SIMULATOR,
            timeout_ms=150,
            retry_budget={"submit_retries": 0, "poll_attempts": 3, "poll_interval_ms": 5_000},
        )
        if runner is not None:
            runner.register_adapter(connector_id, ConnectorMode.SIMULATOR, adapter)
        adapters[connector_id] = adapter
    return adapters


# ---------------------------------------------------------------------------
# Deterministic demo transactions (refs derived sha256(seed || i) — PSO-5)
# ---------------------------------------------------------------------------


def derive_demo_transfers(
    participants: Sequence[str], *, count: int, seed: str
) -> tuple[tuple[str, str, int, str], ...]:
    """``count`` deterministic inter-participant transfers from ``seed``.

    The reference derivation is the one the tracked scenario artifact pins:
    ``ref = sha256_canonical({'seed': seed, 'i': i})[:24]`` — no randomness,
    no clock (demo step 3). Routing and amount come from later hex of the
    SAME digest; amounts are integer paisa in [100, 10_099] (sized so 10,000
    transactions stay far inside the step-1 Net Debit Cap; verified maximum
    in-flight |net| for the founding seed is 931,834 paisa against a
    10,000,000-paisa cap). Payer != payee by construction.
    """
    if len(participants) < 2:
        raise PsoCertificationError("demo transfers need at least two participants")
    if count < 1:
        raise PsoCertificationError(f"transfer count must be >= 1, got {count}")
    ordered = tuple(sorted(participants))
    n = len(ordered)
    out: list[tuple[str, str, int, str]] = []
    for index in range(count):
        digest = sha256_canonical({"seed": seed, "i": index})
        from_index = int(digest[24:32], 16) % n
        to_index = (from_index + 1 + (int(digest[32:40], 16) % (n - 1))) % n
        amount_minor = 100 + int(digest[40:52], 16) % 10_000
        out.append((ordered[from_index], ordered[to_index], amount_minor, digest[:24]))
    return tuple(out)


def fixture_transfers_from_positions(
    positions: Mapping[str, int],
) -> tuple[tuple[str, str, int, str], ...]:
    """Deterministic transfer set reproducing the fixture's net positions.

    The tracked artifact's step-8 fixture ships target POSITIONS, not the
    transfers that produced them; the runner reconstructs a minimal balanced
    transfer set by greedy debtor->creditor matching in ``participant_id``
    ASC order. Pure integer-paisa arithmetic; the derived window then MUST
    re-net to exactly the fixture positions (asserted by the step).
    """
    if sum(positions.values()) != 0:
        raise DemoScriptError("fixture positions must be zero-sum")
    debtors = [(pid, -net) for pid, net in sorted(positions.items()) if net < 0]
    creditors = [(pid, net) for pid, net in sorted(positions.items()) if net > 0]
    if not debtors or not creditors:
        raise DemoScriptError("fixture positions need at least one PAY and one RECEIVE")
    out: list[tuple[str, str, int, str]] = []
    d_index = c_index = 0
    d_left = debtors[0][1]
    c_left = creditors[0][1]
    sequence = 0
    while d_index < len(debtors) and c_index < len(creditors):
        amount = min(d_left, c_left)
        out.append(
            (debtors[d_index][0], creditors[c_index][0], amount, f"pi_fixture_{sequence:04d}")
        )
        sequence += 1
        d_left -= amount
        c_left -= amount
        if d_left == 0:
            d_index += 1
            d_left = debtors[d_index][1] if d_index < len(debtors) else 0
        if c_left == 0:
            c_index += 1
            c_left = creditors[c_index][1] if c_index < len(creditors) else 0
    return tuple(out)


def validate_demo_script(script: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the tracked live-demo scenario artifact; return the run plan.

    The artifact is ``docs/founding-participant/demo-pso-founding-v1.json``
    (spec/19 PSO-5 artifact 3, owned by the founding-participant pack lane;
    the battery EXECUTES it and never rewrites it). Every machine-readable
    field the runner relies on is validated fail-closed; a drifted script
    must refuse loudly, never run with surprises.
    """
    if not isinstance(script, Mapping):
        raise DemoScriptError("demo script must be a JSON object")
    if script.get("scenario_id") != DEMO_SCRIPT_NAME:
        raise DemoScriptError(
            f"scenario_id must be {DEMO_SCRIPT_NAME!r}, got {script.get('scenario_id')!r}"
        )
    if script.get("suite_version") != PSO_CERT_SUITE_VERSION:
        raise DemoScriptError(f"suite_version must be {PSO_CERT_SUITE_VERSION!r}")
    if script.get("scenario_profile") != "DEMO":
        raise DemoScriptError("scenario_profile must be 'DEMO'")
    pins = script.get("config_pins")
    if not isinstance(pins, Mapping):
        raise DemoScriptError("config_pins must be an object")
    if pins.get("PLATFORM_MODE") != "PSO":
        raise DemoScriptError("config_pins.PLATFORM_MODE must be 'PSO'")
    if pins.get("PSO_NETTING_INSTRUCTION_RAIL") != "rtgs_iso20022_v1":
        raise DemoScriptError("config_pins.PSO_NETTING_INSTRUCTION_RAIL must pin rtgs_iso20022_v1")
    if pins.get("FOUNDING_PARTICIPANT_TARGET_COUNT") != DEMO_PARTICIPANT_COUNT:
        raise DemoScriptError(
            f"config_pins.FOUNDING_PARTICIPANT_TARGET_COUNT must be {DEMO_PARTICIPANT_COUNT}"
        )
    sims = script.get("simulated_participants")
    if not isinstance(sims, list) or len(sims) != DEMO_PARTICIPANT_COUNT:
        raise DemoScriptError(f"simulated_participants must list {DEMO_PARTICIPANT_COUNT} rows")
    connector_ids: list[str] = []
    for row in sims:
        if not isinstance(row, Mapping):
            raise DemoScriptError("each simulated participant must be an object")
        if row.get("protocol") != "PAYMENT" or row.get("mode") != "SIMULATOR":
            raise DemoScriptError("simulated participants must be protocol=PAYMENT mode=SIMULATOR")
        connector_ids.append(str(row.get("connector_id")))
    if tuple(connector_ids) != SIMULATED_PARTICIPANT_CONNECTOR_IDS:
        raise DemoScriptError(
            "simulated participant connector ids must be exactly "
            f"{list(SIMULATED_PARTICIPANT_CONNECTOR_IDS)}"
        )
    steps = script.get("steps")
    if not isinstance(steps, list) or len(steps) != len(DEMO_STEP_NAMES):
        raise DemoScriptError(f"steps must list exactly the {len(DEMO_STEP_NAMES)} demo steps")
    by_number: dict[int, Mapping[str, Any]] = {}
    for index, step in enumerate(steps, start=1):
        if (
            not isinstance(step, Mapping)
            or step.get("step") != index
            or step.get("name") != DEMO_STEP_NAMES[index - 1]
        ):
            raise DemoScriptError(
                f"step {index} must be {{'step': {index}, 'name': "
                f"{DEMO_STEP_NAMES[index - 1]!r}, ...}}"
            )
        by_number[index] = step
    transaction_count = by_number[3].get("transaction_count")
    if transaction_count != DEMO_TRANSACTION_COUNT:
        raise DemoScriptError(f"step 3 transaction_count must be {DEMO_TRANSACTION_COUNT}")
    ref_derivation = str(by_number[3].get("ref_derivation", ""))
    if DEMO_SCRIPT_NAME not in ref_derivation or "sha256_canonical" not in ref_derivation:
        raise DemoScriptError(
            "step 3 ref_derivation must pin sha256_canonical over the scenario seed"
        )
    over_cap_connector = str(by_number[4].get("scripted_over_cap_participant", ""))
    if over_cap_connector not in SIMULATED_PARTICIPANT_CONNECTOR_IDS:
        raise DemoScriptError("step 4 scripted_over_cap_participant must be a demo participant")
    over_cap_amount = by_number[4].get("over_cap_amount_minor")
    if isinstance(over_cap_amount, bool) or not isinstance(over_cap_amount, int):
        raise DemoScriptError("step 4 over_cap_amount_minor must be int paisa")
    if over_cap_amount < 2:
        raise DemoScriptError("step 4 over_cap_amount_minor must exceed the cap by at least 1")
    cap_minor = over_cap_amount - 1  # the artifact pins amount = cap + 1
    fixture = by_number[8].get("disputed_window_fixture")
    if not isinstance(fixture, Mapping):
        raise DemoScriptError("step 8 must ship disputed_window_fixture")
    fixture_date = str(fixture.get("window_date", ""))
    try:
        datetime.strptime(fixture_date, "%Y-%m-%d")
    except ValueError as exc:
        raise DemoScriptError("fixture window_date must be YYYY-MM-DD") from exc
    fixture_session = fixture.get("dns_session")
    if fixture_session not in _DNS_SESSIONS:
        raise DemoScriptError(f"fixture dns_session must be one of {_DNS_SESSIONS}")
    if (fixture_date, fixture_session) == (DEMO_WINDOW_DATE, DEMO_WINDOW_SESSION):
        raise DemoScriptError(
            "the disputed window must be PRIOR to (distinct from) the demo window"
        )
    raw_positions = fixture.get("positions")
    if not isinstance(raw_positions, list) or not raw_positions:
        raise DemoScriptError("fixture positions must be a non-empty list")
    positions: dict[str, int] = {}
    for row in raw_positions:
        if not isinstance(row, Mapping) or set(row.keys()) != {
            "participant_id",
            "net_position_minor",
        }:
            raise DemoScriptError(
                "each fixture position needs exactly participant_id + net_position_minor"
            )
        net = row["net_position_minor"]
        if isinstance(net, bool) or not isinstance(net, int) or net == 0:
            raise DemoScriptError("fixture nets must be nonzero int paisa (FLAT rows excluded)")
        pid = str(row["participant_id"])
        if pid in positions:
            raise DemoScriptError(f"duplicate fixture participant {pid}")
        positions[pid] = net
    if sum(positions.values()) != 0:
        raise DemoScriptError("fixture positions must be zero-sum")
    if fixture.get("participant_count") != len(positions):
        raise DemoScriptError("fixture participant_count must equal len(positions)")
    fixture_cap = max(cap_minor, max(abs(net) for net in positions.values()) * 2)
    return {
        "seed": DEMO_SCRIPT_NAME,
        "transaction_count": DEMO_TRANSACTION_COUNT,
        "connector_ids": tuple(connector_ids),
        "cap_minor": cap_minor,
        "over_cap_connector_id": over_cap_connector,
        "over_cap_amount_minor": over_cap_amount,
        "demo_window": {"window_date": DEMO_WINDOW_DATE, "dns_session": DEMO_WINDOW_SESSION},
        "fixture_window": {"window_date": fixture_date, "dns_session": str(fixture_session)},
        "fixture_positions": positions,
        "fixture_cap_minor": fixture_cap,
    }


# ---------------------------------------------------------------------------
# Check helpers
# ---------------------------------------------------------------------------


def _check(check_id: str, passed: bool, evidence: Mapping[str, Any]) -> dict:
    return {
        "check_id": check_id,
        "name": _CHECK_NAMES[check_id],
        "verdict": "PASSED" if passed else "FAILED",
        "evidence_hash": sha256_canonical(dict(evidence)),
    }


def check_matrix(record: PsoCertificationRunRecord) -> dict[str, str]:
    """The machine-readable ``{PSO-CERT-01..10: verdict}`` matrix (§API D)."""
    verdicts = {check["check_id"]: check["verdict"] for check in record.checks}
    return {check_id: verdicts.get(check_id, "MISSING") for check_id in PSO_CERT_CHECK_IDS}


def pso_certification_matrix(records: Sequence[PsoCertificationRunRecord]) -> dict:
    """Battery-run matrix (machine-readable; canonical-JSON serializable)."""
    runs: dict[str, dict] = {}
    for record in records:
        runs[record.pso_certification_run_id] = {
            "suite_version": record.suite_version,
            "scenario_profile": record.scenario_profile,
            "platform_mode": record.platform_mode,
            "status": record.status,
            "report_hash": record.report_hash,
            "checks": check_matrix(record),
        }
    return {
        "schema_version": 1,
        "matrix_kind": "pso_certification",
        "runs": runs,
    }


# ---------------------------------------------------------------------------
# The battery
# ---------------------------------------------------------------------------


class PsoCertificationBattery:
    """Executes PSO-CERT-01..10 (and the DEMO profile) over fresh benches."""

    def __init__(
        self,
        bench_factory: Callable[[], PsoPlatformBench],
        *,
        clock: Clock,
        platform_mode: str = "PSO",
        suite_version: str = PSO_CERT_SUITE_VERSION,
        run_store: PsoCertificationRunStore | None = None,
        object_store: ObjectStore | None = None,
        audit: AuditSink | None = None,
        events: EventSink | None = None,
        triggered_by: str = "operator:pso-certify",
        cert_transaction_count: int = CERT_PROFILE_TRANSACTION_COUNT,
        demo_script: Mapping[str, Any] | None = None,
    ) -> None:
        if platform_mode not in ("PSP", "PSO"):
            raise PsoCertificationError(
                f"platform_mode must be 'PSP' or 'PSO', got {platform_mode!r}"
            )
        if cert_transaction_count < CERT_PROFILE_TRANSACTION_COUNT:
            raise PsoCertificationError(
                "PSO-CERT-01 requires >= "
                f"{CERT_PROFILE_TRANSACTION_COUNT} transactions (spec/19), "
                f"got {cert_transaction_count}"
            )
        self._factory = bench_factory
        self._clock = clock
        self._platform_mode = platform_mode
        self._suite_version = suite_version
        self._runs = run_store if run_store is not None else InMemoryPsoCertificationRunStore()
        self._objects = object_store if object_store is not None else InMemoryObjectStore()
        self._audit = audit if audit is not None else InMemoryAuditSink()
        self._events = events if events is not None else InMemoryEventSink()
        self._triggered_by = triggered_by
        self._tx_count = cert_transaction_count
        self._demo_script = (
            None if demo_script is None else validate_demo_script(demo_script)
        )

    @property
    def run_store(self) -> PsoCertificationRunStore:
        return self._runs

    # -- FSM -------------------------------------------------------------------

    @staticmethod
    def _transition(record: PsoCertificationRunRecord, trigger: str) -> str:
        to_state = PSO_CERTIFICATION_TRANSITIONS.get((record.status, trigger))
        if to_state is None:
            raise PsoCertificationTransitionError(
                f"battery run {record.pso_certification_run_id}: "
                f"{record.status} --[{trigger}]--> ? is not in the FSM 5 table (denied)"
            )
        return to_state

    # -- run ---------------------------------------------------------------------

    async def run(self, scenario_profile: str = "CERT") -> PsoCertificationRunRecord:
        """One battery execution; returns the sealed run record.

        Refusal order (binding): profile validity, then the PLATFORM_MODE
        gate — BEFORE the clock is read or any record is written (spec/19
        §Scope: no data read or written under PSP).
        """
        if scenario_profile not in SCENARIO_PROFILES:
            raise PsoCertificationError(
                f"scenario_profile must be one of {SCENARIO_PROFILES}, got {scenario_profile!r}"
            )
        if self._platform_mode != "PSO":
            raise PsoModeError(
                "the PSO certification battery is a PSO-only operation; refused "
                f"under PLATFORM_MODE={self._platform_mode!r}"
            )
        if scenario_profile == "DEMO" and self._demo_script is None:
            raise DemoScriptError(
                "scenario_profile='DEMO' requires the validated "
                f"{DEMO_SCRIPT_NAME} scenario document (none injected)"
            )
        started_at = self._clock.now()
        record = PsoCertificationRunRecord(
            pso_certification_run_id=make_pso_certification_id(
                {
                    "suite_version": self._suite_version,
                    "platform_mode": self._platform_mode,
                    "started_at": started_at,
                }
            ),
            suite_version=self._suite_version,
            scenario_profile=scenario_profile,
            platform_mode=self._platform_mode,
            triggered_by=self._triggered_by,
            created_at=started_at,
        )
        self._runs.save(record)
        record.status = self._transition(record, "runner_pickup")
        record.started_at = started_at
        self._runs.save(record)
        try:
            checks_one, demo_one = await self._execute_pass(scenario_profile)
            checks_two, demo_two = await self._execute_pass(scenario_profile)
            bytes_one = canonical_json({"checks": checks_one, "demo_steps": demo_one})
            bytes_two = canonical_json({"checks": checks_two, "demo_steps": demo_two})
            checks = list(checks_one)
            checks.append(
                _check(
                    "PSO-CERT-10",
                    bytes_one == bytes_two,
                    {
                        "pass_one_hash": sha256_canonical(
                            {"checks": checks_one, "demo_steps": demo_one}
                        ),
                        "pass_two_hash": sha256_canonical(
                            {"checks": checks_two, "demo_steps": demo_two}
                        ),
                    },
                )
            )
            report: dict[str, Any] = {
                "suite_version": self._suite_version,
                "scenario_profile": scenario_profile,
                "platform_mode": self._platform_mode,
                "checks": checks,
            }
            demo_passed = True
            if scenario_profile == "DEMO":
                report["demo_steps"] = demo_one
                demo_passed = bool(demo_one) and all(step["passed"] for step in demo_one)
            report_bytes = canonical_json(report)
            record.checks = checks
            record.report_hash = sha256_canonical(report)
            record.report_pointer = self._objects.put(
                f"psocert/{record.pso_certification_run_id}/report", report_bytes
            )
            all_passed = all(c["verdict"] == "PASSED" for c in checks) and demo_passed
            record.status = self._transition(
                record, "checks_passed" if all_passed else "check_failed"
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
            record.status = self._transition(record, "errored")
        record.finished_at = self._clock.now()
        self._runs.save(record)
        self._audit.record(
            "PSO_CERTIFICATION_COMPLETED",
            actor_id=self._triggered_by,
            occurred_at=record.finished_at,
            detail={
                "pso_certification_run_id": record.pso_certification_run_id,
                "suite_version": self._suite_version,
                "scenario_profile": scenario_profile,
                "status": record.status,
            },
        )
        self._events.emit(
            "pso_certification_run.completed",
            {
                "pso_certification_run_id": record.pso_certification_run_id,
                "suite_version": self._suite_version,
                "status": record.status,
                "report_hash": record.report_hash,
            },
        )
        return record

    async def _execute_pass(
        self, scenario_profile: str
    ) -> tuple[list[dict], list[dict]]:
        """One full pass of PSO-CERT-01..09 (+ demo steps under DEMO)."""
        checks = [
            await self._cert_01(self._factory()),
            self._cert_02(self._factory()),
            await self._cert_03(self._factory()),
            await self._cert_04(self._factory()),
            self._cert_05(self._factory()),
            await self._cert_06(self._factory()),
            await self._cert_07(self._factory()),
            self._cert_08(),
            await self._cert_09(self._factory()),
        ]
        checks.sort(key=lambda c: c["check_id"])
        demo_steps: list[dict] = []
        if scenario_profile == "DEMO":
            assert self._demo_script is not None  # guarded in run()
            demo_steps = await self._run_demo(self._factory(), self._demo_script)
        return checks, demo_steps

    # -- shared drivers ----------------------------------------------------------

    async def _drive_window_to_close(
        self,
        bench: PsoPlatformBench,
        window_id: str,
    ) -> dict[str, Any]:
        """cutoff -> netting -> TCSA-gated instruct -> settle -> confirm -> close."""
        bench.cutoff(window_id)
        run = bench.compute_netting(window_id)
        bench.instruct(window_id)
        legs = await bench.settle_legs(window_id)
        confirmed = bench.confirm(window_id)
        closed = bench.close_window(window_id)
        return {
            "run": dict(run),
            "legs": dict(legs),
            "confirmed_status": confirmed,
            "closed": dict(closed),
        }

    async def _build_disputed_window(
        self,
        bench: PsoPlatformBench,
        fixture: Mapping[str, Any],
        *,
        cap_minor: int,
    ) -> str:
        """Build + fully close a disputed-window fixture; returns window id."""
        transfers = fixture["transfers"]
        participant_ids = tuple(
            sorted({row["from"] for row in transfers} | {row["to"] for row in transfers})
        )
        bench.provision_participants(participant_ids, cap_minor=cap_minor)
        window_id = bench.open_window(
            window_date=fixture["window"]["window_date"],
            dns_session=fixture["window"]["dns_session"],
        )
        for row in transfers:
            bench.post_transfer(
                window_id,
                from_participant_id=row["from"],
                to_participant_id=row["to"],
                amount_minor=row["amount_minor"],
                ref=row["ref"],
            )
        await self._drive_window_to_close(bench, window_id)
        return window_id

    # -- PSO-CERT-01: full window lifecycle ---------------------------------------

    async def _cert_01(self, bench: PsoPlatformBench) -> dict:
        states: list[str] = []
        window_id = bench.open_window(window_date="2026-06-15", dns_session="AM")
        states.append(bench.window_status(window_id))  # ACCUMULATING_POSITIONS
        transfers = derive_demo_transfers(
            bench.participants, count=self._tx_count, seed="pso-cert-01"
        )
        for from_p, to_p, amount, ref in transfers:
            bench.post_transfer(
                window_id,
                from_participant_id=from_p,
                to_participant_id=to_p,
                amount_minor=amount,
                ref=ref,
            )
        bench.cutoff(window_id)
        states.append(bench.window_status(window_id))  # CUTOFF_REACHED
        run = bench.compute_netting(window_id)
        states.append(bench.window_status(window_id))  # NETTING_CALCULATED
        zero_sum = sum(int(o["net_position_minor"]) for o in bench.netting_obligations(window_id))
        bench.instruct(window_id)
        states.append(bench.window_status(window_id))  # SETTLEMENT_INSTRUCTED
        legs = await bench.settle_legs(window_id)
        states.append(bench.confirm(window_id))  # SETTLEMENT_CONFIRMED
        closed = bench.close_window(window_id)
        states.append(str(closed["window_status"]))  # WINDOW_CLOSED
        trial_balance = bench.trial_balance_ok()
        expected_states = [
            "ACCUMULATING_POSITIONS",
            "CUTOFF_REACHED",
            "NETTING_CALCULATED",
            "SETTLEMENT_INSTRUCTED",
            "SETTLEMENT_CONFIRMED",
            "WINDOW_CLOSED",
        ]
        passed = (
            states == expected_states
            and len(transfers) >= CERT_PROFILE_TRANSACTION_COUNT
            and run["status"] == "PASSED"
            and int(run["participant_count"]) == len(bench.participants)
            and zero_sum == 0
            and all(status == "SETTLED" for status in dict(legs["legs"]).values())
            and list(closed["recon_unmatched"]) == []
            and trial_balance
        )
        return _check(
            "PSO-CERT-01",
            passed,
            {
                "states": states,
                "transactions": len(transfers),
                "participants": len(bench.participants),
                "zero_sum_delta_minor": zero_sum,
                "netting_run_status": run["status"],
                "result_hash": run["result_hash"],
                "leg_statuses": dict(legs["legs"]),
                "recon_unmatched": list(closed["recon_unmatched"]),
                "trial_balance_ok": trial_balance,
            },
        )

    # -- PSO-CERT-02: NDC breach refusal mid-window --------------------------------

    def _cert_02(self, bench: PsoPlatformBench) -> dict:
        window_id = bench.open_window(window_date="2026-06-15", dns_session="AM")
        target = bench.participants[2]
        bench.post_transfer(
            window_id,
            from_participant_id=bench.participants[0],
            to_participant_id=target,
            amount_minor=50_000,
            ref="pi_cert02_seed",
        )
        before = {
            "status": bench.window_status(window_id),
            "net": bench.net_position(window_id, target),
            "breaches": bench.ndc_breach_count(target),
            "events": bench.outbox_count("settlement_window.ndc_breach_blocked"),
            "entries": bench.journal_entry_count(),
        }
        refusal = dict(bench.attempt_over_cap_debit(window_id, target))
        after = {
            "status": bench.window_status(window_id),
            "net": bench.net_position(window_id, target),
            "breaches": bench.ndc_breach_count(target),
            "events": bench.outbox_count("settlement_window.ndc_breach_blocked"),
            "entries": bench.journal_entry_count(),
        }
        passed = (
            refusal["refused"] is True
            and refusal["error"] == "NdcBreachError"
            and after["breaches"] == before["breaches"] + 1
            and after["events"] == before["events"] + 1
            and after["status"] == before["status"] == "ACCUMULATING_POSITIONS"
            and after["net"] == before["net"]
            and after["entries"] == before["entries"]  # nothing reached the ledger
        )
        return _check(
            "PSO-CERT-02",
            passed,
            {"refusal": refusal, "before": before, "after": after, "participant_index": 2},
        )

    # -- PSO-CERT-03: participant failure mid-window --------------------------------

    async def _cert_03(self, bench: PsoPlatformBench) -> dict:
        window_id = bench.open_window(window_date="2026-06-15", dns_session="AM")
        suspended = bench.participants[2]
        survivor = bench.participants[0]
        transfers = derive_demo_transfers(bench.participants, count=40, seed="pso-cert-03")
        for from_p, to_p, amount, ref in transfers:
            bench.post_transfer(
                window_id,
                from_participant_id=from_p,
                to_participant_id=to_p,
                amount_minor=amount,
                ref=ref,
            )
        bench.post_transfer(
            window_id,
            from_participant_id=suspended,
            to_participant_id=survivor,
            amount_minor=750_000,
            ref="pi_cert03_accepted",
        )
        net_before = bench.net_position(window_id, suspended)
        events_before = bench.outbox_count("settlement_window.participant_suspended")
        bench.suspend_participant(window_id, suspended)
        refusal_from = dict(
            bench.attempt_transfer(
                window_id,
                from_participant_id=suspended,
                to_participant_id=survivor,
                amount_minor=10_000,
                ref="pi_cert03_refused_out",
            )
        )
        refusal_to = dict(
            bench.attempt_transfer(
                window_id,
                from_participant_id=survivor,
                to_participant_id=suspended,
                amount_minor=10_000,
                ref="pi_cert03_refused_in",
            )
        )
        net_after = bench.net_position(window_id, suspended)
        outcome = await self._drive_window_to_close(bench, window_id)
        passed = (
            bench.outbox_count("settlement_window.participant_suspended") == events_before + 1
            and refusal_from["accepted"] is False
            and refusal_from["error"] == "NdcParticipantNotActiveError"
            and refusal_to["accepted"] is False
            and refusal_to["error"] == "NdcParticipantNotActiveError"
            and net_after == net_before  # accepted-is-final: positions kept
            and outcome["run"]["status"] == "PASSED"
            and str(outcome["closed"]["window_status"]) == "WINDOW_CLOSED"
            and bench.trial_balance_ok()
        )
        return _check(
            "PSO-CERT-03",
            passed,
            {
                "suspended_participant": suspended,
                "refusal_outbound": refusal_from,
                "refusal_inbound": refusal_to,
                "net_before_minor": net_before,
                "net_after_minor": net_after,
                "window_final": str(outcome["closed"]["window_status"]),
                "leg_statuses": dict(outcome["legs"]["legs"]),
            },
        )

    # -- PSO-CERT-04: settlement-failure unwind (D-19-2) ----------------------------

    async def _cert_04(self, bench: PsoPlatformBench) -> dict:
        bench.provision_participants(GOLDEN_PARTICIPANTS, cap_minor=GOLDEN_CAP_MINOR)
        window_id = bench.open_window(window_date="2026-06-15", dns_session="PM")
        for from_p, to_p, amount, ref in GOLDEN_TRANSFERS:
            bench.post_transfer(
                window_id,
                from_participant_id=from_p,
                to_participant_id=to_p,
                amount_minor=amount,
                ref=ref,
            )
        bench.cutoff(window_id)
        old_run = dict(bench.compute_netting(window_id))
        bench.instruct(window_id)
        failed_legs = await bench.settle_legs(
            window_id, reject_participant_id=GOLDEN_NET_DEBTOR
        )
        failed_state = bench.window_status(window_id)
        entries_before = bench.journal_entry_count()
        unwound = dict(bench.unwind(window_id, GOLDEN_NET_DEBTOR))
        entries_after = bench.journal_entry_count()
        new_run = dict(unwound["run"])
        superseded = dict(unwound["superseded"])
        # excluded participant nets FLAT in the survivor re-net:
        survivor_obligations = {
            str(o["participant_id"]): str(o["direction"])
            for o in bench.netting_obligations(window_id)
        }
        zero_sum = sum(int(o["net_position_minor"]) for o in bench.netting_obligations(window_id))
        bench.instruct(window_id)
        survivor_legs = await bench.settle_legs(window_id)
        confirmed = bench.confirm(window_id)
        closed = bench.close_window(window_id)
        # The debtor touched 3 golden transfers => 3 offsetting entries; the
        # re-net writes 1 fresh settlement_batch_close entry => +4 total.
        passed = (
            failed_state == "SETTLEMENT_FAILED"
            and dict(failed_legs["legs"])[GOLDEN_NET_DEBTOR] == "FAILED"
            and superseded["status"] == "SUPERSEDED"
            and new_run["status"] == "PASSED"
            and new_run["excluded_participant_id"] == GOLDEN_NET_DEBTOR
            and new_run["supersedes_netting_run_id"] == old_run["netting_run_id"]
            and entries_after == entries_before + 4
            and survivor_obligations[GOLDEN_NET_DEBTOR] == "FLAT"
            and zero_sum == 0
            and all(s in ("CANCELLED", "FAILED") for s in dict(unwound["cancelled_legs"]).values())
            and all(status == "SETTLED" for status in dict(survivor_legs["legs"]).values())
            and confirmed == "SETTLEMENT_CONFIRMED"
            and str(closed["window_status"]) == "WINDOW_CLOSED"
            and bench.trial_balance_ok()
        )
        return _check(
            "PSO-CERT-04",
            passed,
            {
                "excluded_participant": GOLDEN_NET_DEBTOR,
                "old_run": old_run["netting_run_id"],
                "old_run_status": superseded["status"],
                "new_run": new_run["netting_run_id"],
                "reversal_entry_delta": entries_after - entries_before,
                "cancelled_legs": dict(unwound["cancelled_legs"]),
                "survivor_zero_sum_delta": zero_sum,
                "window_final": str(closed["window_status"]),
            },
        )

    # -- PSO-CERT-05: hand-computed golden vector ------------------------------------

    def _cert_05(self, bench: PsoPlatformBench) -> dict:
        bench.provision_participants(GOLDEN_PARTICIPANTS, cap_minor=GOLDEN_CAP_MINOR)
        window_id = bench.open_window(window_date="2026-06-15", dns_session="EOD")
        for from_p, to_p, amount, ref in GOLDEN_TRANSFERS:
            bench.post_transfer(
                window_id,
                from_participant_id=from_p,
                to_participant_id=to_p,
                amount_minor=amount,
                ref=ref,
            )
        bench.cutoff(window_id)
        run = dict(bench.compute_netting(window_id))
        observed: dict[str, Any] = {}
        instructions_ok = True
        for row in bench.netting_obligations(window_id):
            participant_id = str(row["participant_id"])
            observed[participant_id] = (
                str(row["direction"]),
                int(row["obligation_minor"]),
                int(row["net_position_minor"]),
            )
            instruction = row.get("instruction")
            direction, obligation, _net = GOLDEN_EXPECTED_OBLIGATIONS[participant_id]
            if direction == "FLAT":
                instructions_ok = instructions_ok and instruction is None
            else:
                expected_ref = self._expected_connector_ref(
                    run["netting_run_id"], participant_id
                )
                instructions_ok = (
                    instructions_ok
                    and instruction is not None
                    and int(instruction["amount_minor"]) == obligation
                    and str(instruction["direction"])
                    == ("DEBIT" if direction == "PAY" else "CREDIT")
                    and str(instruction["rail"]) == "rtgs_iso20022_v1"
                    and str(instruction["connector_ref"]) == expected_ref
                )
        expected = {
            pid: GOLDEN_EXPECTED_OBLIGATIONS[pid] for pid in GOLDEN_PARTICIPANTS
        }
        passed = (
            observed == expected
            and int(run["total_net_pay_minor"]) == GOLDEN_TOTAL_NET_PAY
            and int(run["total_gross_minor"]) == GOLDEN_TOTAL_GROSS
            and instructions_ok
        )
        return _check(
            "PSO-CERT-05",
            passed,
            {
                "observed": {pid: list(v) for pid, v in observed.items()},
                "expected": {pid: list(v) for pid, v in expected.items()},
                "total_net_pay_minor": int(run["total_net_pay_minor"]),
                "total_gross_minor": int(run["total_gross_minor"]),
                "instructions_ok": instructions_ok,
            },
        )

    @staticmethod
    def _expected_connector_ref(netting_run_id: str, participant_id: str) -> str:
        """The PSO-2 content-addressed leg ref, recomputed independently."""
        digest = sha256_canonical(
            {
                "kind": "PSO_NETTING",
                "netting_run_id": netting_run_id,
                "participant_id": participant_id,
            }
        )
        return f"psonet_{digest[:HASH_LENGTH]}"

    # -- PSO-CERT-06: replay byte-parity on a disputed window -------------------------

    async def _cert_06(self, bench: PsoPlatformBench) -> dict:
        window_id = await self._build_disputed_window(
            bench, GOLDEN_DISPUTE_FIXTURE, cap_minor=GOLDEN_CAP_MINOR
        )
        run = dict(bench.compute_netting(window_id))  # idempotent read of the live run
        first = dict(bench.replay_window(window_id))
        second = dict(bench.replay_window(window_id))
        passed = (
            first["verdict"] == "PASSED"
            and second["verdict"] == "PASSED"
            and first["result_bytes_identical"] is True
            and second["result_bytes_identical"] is True
            and first["chain_deep_verified"] is True
            and second["chain_deep_verified"] is True
            and first["recomputed_result_hash"]
            == second["recomputed_result_hash"]
            == run["result_hash"]
            and first["record_status"] == "COMPLETED"
            and second["record_status"] == "COMPLETED"
        )
        return _check(
            "PSO-CERT-06",
            passed,
            {
                "window_id": window_id,
                "stored_result_hash": run["result_hash"],
                "replay_one": first,
                "replay_two": second,
            },
        )

    # -- PSO-CERT-07: PLATFORM_MODE gating ---------------------------------------------

    async def _cert_07(self, bench: PsoPlatformBench) -> dict:
        probe = dict(bench.psp_probe())
        battery_refusal: dict[str, Any] = {"refused": False}
        psp_battery = PsoCertificationBattery(
            self._factory,
            clock=self._clock,
            platform_mode="PSP",
            suite_version=self._suite_version,
        )
        try:
            await psp_battery.run()
        except PsoModeError as exc:
            battery_refusal = {
                "refused": True,
                "code": exc.code,
                "type": exc.type,
                "http_status": exc.http_status,
            }
        engine_refusals = dict(probe.get("engine_refusals", {}))
        envelope = dict(probe.get("api_envelope", {}))
        passed = (
            bool(engine_refusals)
            and all(name == "PsoModeError" for name in engine_refusals.values())
            and list(probe.get("pso_scheduler_tasks_under_psp", ["sentinel"])) == []
            and len(list(probe.get("pso_scheduler_tasks_under_pso", []))) == 3
            and envelope.get("type") == "authorization"
            and envelope.get("code") == "platform_mode_pso_required"
            and envelope.get("http_status") == 403
            and battery_refusal.get("refused") is True
            and battery_refusal.get("code") == "platform_mode_pso_required"
            and battery_refusal.get("http_status") == 403
        )
        return _check(
            "PSO-CERT-07",
            passed,
            {"probe": probe, "battery_refusal": battery_refusal},
        )

    # -- PSO-CERT-08: seeded-order independence ------------------------------------------

    def _cert_08(self) -> dict:
        seeds = ("pso-cert-08-a", "pso-cert-08-b", "pso-cert-08-c")
        inputs_hashes: list[str] = []
        core_hashes: list[str] = []
        full_hashes: list[str] = []
        orders: list[list[str]] = []
        for seed in seeds:
            bench = self._factory()
            bench.provision_participants(GOLDEN_PARTICIPANTS, cap_minor=GOLDEN_CAP_MINOR)
            window_id = bench.open_window(window_date="2026-06-15", dns_session="AM")
            shuffled = sorted(
                GOLDEN_TRANSFERS,
                key=lambda row: hashlib.sha256(f"{seed}|{row[3]}".encode()).hexdigest(),
            )
            orders.append([row[3] for row in shuffled])
            for from_p, to_p, amount, ref in shuffled:
                bench.post_transfer(
                    window_id,
                    from_participant_id=from_p,
                    to_participant_id=to_p,
                    amount_minor=amount,
                    ref=ref,
                )
            bench.cutoff(window_id)
            run = dict(bench.compute_netting(window_id))
            document = dict(bench.netting_result_document(window_id))
            core = {k: v for k, v in document.items() if k not in RESULT_ANCHOR_KEYS}
            inputs_hashes.append(str(run["inputs_hash"]))
            core_hashes.append(sha256_canonical(core))
            full_hashes.append(str(run["result_hash"]))
        distinct_orders = len({tuple(order) for order in orders}) > 1
        passed = (
            distinct_orders
            and len(set(inputs_hashes)) == 1
            and len(set(core_hashes)) == 1
        )
        return _check(
            "PSO-CERT-08",
            passed,
            {
                "seeds": list(seeds),
                "orders": orders,
                "inputs_hashes": inputs_hashes,
                "core_document_hashes": core_hashes,
                # Anchors are order-dependent attestations (C19-2/N19-3):
                # full-document hashes are recorded, never asserted equal.
                "full_result_hashes": full_hashes,
            },
        )

    # -- PSO-CERT-09: conformance bidirectionality -----------------------------------------

    async def _cert_09(self, bench: PsoPlatformBench) -> dict:
        passing = dict(await bench.onboard_via_conformance(failing=False))
        failing = dict(await bench.onboard_via_conformance(failing=True))
        run_verdicts = dict(passing.get("run_verdicts", {}))
        held_states = list(failing.get("states_after_failures", []))
        retry_limit = int(failing.get("retry_limit", 0))
        passed = (
            passing.get("final_state") == "CONFORMANCE_PASSED"
            and run_verdicts.get("OUTBOUND") == "PASSED"
            and run_verdicts.get("INBOUND") == "PASSED"
            and failing.get("final_state") == "REJECTED"
            and retry_limit >= 1
            and len(held_states) == retry_limit - 1
            and all(state == "MOU_SIGNED" for state in held_states)
            and int(failing.get("failed_runs", 0)) == retry_limit
        )
        return _check(
            "PSO-CERT-09",
            passed,
            {"passing": passing, "failing": failing},
        )

    # -- DEMO profile: the eight live-demo steps (PSO-5) -------------------------------------

    async def _run_demo(
        self, bench: PsoPlatformBench, plan: Mapping[str, Any]
    ) -> list[dict]:
        steps: list[dict] = []
        cap_minor = int(plan["cap_minor"])

        # The PRIOR disputed window is history before the live demo begins:
        # reconstructed deterministically from the artifact's target positions
        # (greedy debtor->creditor matching), fully netted and closed.
        fixture_positions: dict[str, int] = dict(plan["fixture_positions"])
        prior_window_id = await self._build_disputed_window(
            bench,
            {
                "window": dict(plan["fixture_window"]),
                "transfers": [
                    {"from": f, "to": t, "amount_minor": a, "ref": r}
                    for f, t, a, r in fixture_transfers_from_positions(fixture_positions)
                ],
            },
            cap_minor=int(plan["fixture_cap_minor"]),
        )
        prior_positions = {
            str(o["participant_id"]): int(o["net_position_minor"])
            for o in bench.netting_obligations(prior_window_id)
        }

        # Step 1 — register + activate the five simulated participants
        # (full PSO-1 FSM, fast-pathed by simulator conformance).
        onboarded: list[dict] = []
        connector_to_participant: dict[str, str] = {}
        for connector_id in plan["connector_ids"]:
            row = dict(await bench.onboard_and_activate(connector_id, cap_minor=cap_minor))
            onboarded.append(row)
            connector_to_participant[connector_id] = str(row["participant_id"])
        demo_participants = tuple(sorted(connector_to_participant.values()))
        step1_passed = (
            len(onboarded) == DEMO_PARTICIPANT_COUNT
            and all(row["final_state"] == "ACTIVE" for row in onboarded)
            and len(set(demo_participants)) == len(onboarded)
        )
        steps.append(
            {
                "step": 1,
                "name": DEMO_STEP_NAMES[0],
                "passed": step1_passed,
                "participants": list(demo_participants),
                "final_states": [row["final_state"] for row in onboarded],
            }
        )

        # Step 2 — open the demo window.
        window_id = bench.open_window(
            window_date=str(plan["demo_window"]["window_date"]),
            dns_session=str(plan["demo_window"]["dns_session"]),
        )
        step2_status = bench.window_status(window_id)
        steps.append(
            {
                "step": 2,
                "name": DEMO_STEP_NAMES[1],
                "passed": step2_status == "ACCUMULATING_POSITIONS",
                "window_id": window_id,
                "window_status": step2_status,
            }
        )

        # Step 3 — 10,000 deterministic transactions
        # (refs = sha256_canonical({'seed', 'i'})[:24], the artifact pin).
        transfers = derive_demo_transfers(
            demo_participants,
            count=int(plan["transaction_count"]),
            seed=str(plan["seed"]),
        )
        for from_p, to_p, amount, ref in transfers:
            bench.post_transfer(
                window_id,
                from_participant_id=from_p,
                to_participant_id=to_p,
                amount_minor=amount,
                ref=ref,
            )
        steps.append(
            {
                "step": 3,
                "name": DEMO_STEP_NAMES[2],
                "passed": (
                    len(transfers) == DEMO_TRANSACTION_COUNT
                    and len({ref for _f, _t, _a, ref in transfers}) == DEMO_TRANSACTION_COUNT
                    and bench.trial_balance_ok()
                ),
                "transactions": len(transfers),
                "first_ref": transfers[0][3],
                "last_ref": transfers[-1][3],
            }
        )

        # Step 4 — LIVE NDC refusal (scripted over-cap submission).
        target = connector_to_participant[str(plan["over_cap_connector_id"])]
        breaches_before = bench.ndc_breach_count(target)
        events_before = bench.outbox_count("settlement_window.ndc_breach_blocked")
        net_before = bench.net_position(window_id, target)
        refusal = dict(
            bench.attempt_over_cap_debit(
                window_id, target, floor_amount_minor=int(plan["over_cap_amount_minor"])
            )
        )
        step4_passed = (
            refusal["refused"] is True
            and refusal["error"] == "NdcBreachError"
            and int(refusal["attempted_debit_minor"]) >= int(plan["over_cap_amount_minor"])
            and bench.ndc_breach_count(target) == breaches_before + 1
            and bench.outbox_count("settlement_window.ndc_breach_blocked") == events_before + 1
            and bench.net_position(window_id, target) == net_before
            and bench.window_status(window_id) == "ACCUMULATING_POSITIONS"
        )
        steps.append(
            {
                "step": 4,
                "name": DEMO_STEP_NAMES[3],
                "passed": step4_passed,
                "participant": target,
                "scripted_floor_minor": int(plan["over_cap_amount_minor"]),
                "refusal": refusal,
            }
        )

        # Step 5 — cutoff + netting; the netting file and its hash.
        bench.cutoff(window_id)
        run = dict(bench.compute_netting(window_id))
        file_bytes = bench.netting_result_bytes(window_id)
        zero_sum = sum(int(o["net_position_minor"]) for o in bench.netting_obligations(window_id))
        file_hash_ok = hashlib.sha256(file_bytes).hexdigest() == run["result_hash"]
        steps.append(
            {
                "step": 5,
                "name": DEMO_STEP_NAMES[4],
                "passed": run["status"] == "PASSED" and zero_sum == 0 and file_hash_ok,
                "netting_run_id": run["netting_run_id"],
                "result_hash": run["result_hash"],
                "netting_file_bytes": len(file_bytes),
                "zero_sum_delta_minor": zero_sum,
            }
        )

        # Step 6 — instruct + confirm on the registered RTGS simulator seam.
        bench.instruct(window_id)
        legs = await bench.settle_legs(window_id)
        confirmed = bench.confirm(window_id)
        steps.append(
            {
                "step": 6,
                "name": DEMO_STEP_NAMES[5],
                "passed": (
                    all(status == "SETTLED" for status in dict(legs["legs"]).values())
                    and str(legs["rail"]) == "rtgs_iso20022_v1"
                    and confirmed == "SETTLEMENT_CONFIRMED"
                ),
                "leg_statuses": dict(legs["legs"]),
                "rail": str(legs["rail"]),
            }
        )

        # Step 7 — close + reconcile with zero unmatched.
        closed = bench.close_window(window_id)
        steps.append(
            {
                "step": 7,
                "name": DEMO_STEP_NAMES[6],
                "passed": (
                    str(closed["window_status"]) == "WINDOW_CLOSED"
                    and list(closed["recon_unmatched"]) == []
                    and bench.trial_balance_ok()
                ),
                "window_status": str(closed["window_status"]),
                "recon_unmatched": list(closed["recon_unmatched"]),
            }
        )

        # Step 8 — byte-identical WINDOW_REPLAY of the PRIOR disputed window.
        # The reconstructed window must net to EXACTLY the artifact's target
        # positions before the byte-parity replay means anything.
        prior_run = dict(bench.compute_netting(prior_window_id))
        first = dict(bench.replay_window(prior_window_id))
        second = dict(bench.replay_window(prior_window_id))
        steps.append(
            {
                "step": 8,
                "name": DEMO_STEP_NAMES[7],
                "passed": (
                    prior_positions == fixture_positions
                    and first["verdict"] == "PASSED"
                    and second["verdict"] == "PASSED"
                    and first["result_bytes_identical"] is True
                    and second["result_bytes_identical"] is True
                    and first["recomputed_result_hash"]
                    == second["recomputed_result_hash"]
                    == prior_run["result_hash"]
                ),
                "prior_window_id": prior_window_id,
                "fixture_positions_reproduced": prior_positions == fixture_positions,
                "stored_result_hash": prior_run["result_hash"],
                "replay_one_hash": first["recomputed_result_hash"],
                "replay_two_hash": second["recomputed_result_hash"],
            }
        )
        return steps

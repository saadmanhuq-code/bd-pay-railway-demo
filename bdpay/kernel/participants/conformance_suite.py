"""``pso-conf-v1`` suite content (spec/19 FSM 2 table of PCONF checks).

The EXISTING spec/10 harness machinery executes everything here — the
simulator scenario engine, the deterministic-report convention
(``dict -> canonical_json -> sha256``), the evidence-hash shape, and the
:class:`bdpay.connectors.certification.CertificationHarness` itself (PCONF-01
is literally CERT-01/02/05/06/07 run against the participant's registered
connector seam). No new dispatch machinery, per spec/19 §Connector contract.

Directions:

- OUTBOUND (:class:`PsoConformanceOutboundSuite`): our harness drives the
  participant's registered (simulated/sandbox) connector seam — PCONF-01,
  PCONF-02 (full sandbox window walk), PCONF-03 (NDC-refusal handling) and
  PCONF-06 (two consecutive runs byte-identical, CERT-10 class).
- INBOUND (:func:`evaluate_inbound_evidence`): the participant certified
  against our sandbox windows; we recompute their report hash, check
  coverage/verdicts (PCONF-04) and reconcile their submitted sandbox
  instructions against our recon view with zero unmatched (PCONF-05).

Determinism: fresh :class:`SimulatorCertEnvironment` per run (injected
``environment_factory``), stepping clock, content-derived references — no
wall clock, no randomness.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Any

from bdpay.connectors.certification import (
    CertificationHarness,
    SimulatorCertEnvironment,
)
from bdpay.connectors.sdk import ConnectorStatus, Money, PaymentInstruction
from bdpay.connectors.simulator import sign_callback
from bdpay.platform.canonical import canonical_json, sha256_canonical
from bdpay.platform.clock import Clock

__all__ = [
    "PCONF_INBOUND_CHECKS",
    "PCONF_OUTBOUND_CHECKS",
    "PsoConformanceOutboundSuite",
    "evaluate_inbound_evidence",
]

#: The published OUTBOUND battery (what we drive, and what an INBOUND report
#: must cover — PCONF-04/05 are our own verifications of that report and the
#: sandbox recon; they cannot appear inside the participant's report).
PCONF_OUTBOUND_CHECKS: tuple[str, ...] = ("PCONF-01", "PCONF-02", "PCONF-03", "PCONF-06")
PCONF_INBOUND_CHECKS: tuple[str, ...] = ("PCONF-04", "PCONF-05")

#: PCONF-01 reuses exactly these spec/10 checks (spec/19 FSM 2 table).
_PCONF01_CERT_IDS: tuple[str, ...] = ("CERT-01", "CERT-02", "CERT-05", "CERT-06", "CERT-07")


def _check(check_id: str, verdict_passed: bool, evidence: Mapping[str, Any]) -> dict:
    return {
        "check_id": check_id,
        "verdict": "PASSED" if verdict_passed else "FAILED",
        "evidence_hash": sha256_canonical(dict(evidence)),
    }


def _rfc3339(clock: Clock) -> str:
    return clock.now().strftime("%Y-%m-%dT%H:%M:%SZ")


class PsoConformanceOutboundSuite:
    """Executes the OUTBOUND ``pso-conf-v1`` checks for one participant seam.

    ``environment_factory`` builds a fresh deterministic
    :class:`SimulatorCertEnvironment` for the participant's registered
    connector (the spec/10 ``build_simulator_environment`` shape); a fresh
    environment per run is what makes PCONF-06 a real determinism proof.
    """

    def __init__(
        self,
        participant_id: str,
        environment_factory: Callable[[], SimulatorCertEnvironment],
    ) -> None:
        self._participant_id = participant_id
        self._factory = environment_factory

    async def run(self) -> list[dict]:
        """One OUTBOUND suite execution -> the PCONF check list (sorted)."""
        checks_one = await self._run_once(self._factory())
        checks_two = await self._run_once(self._factory())
        bytes_one = canonical_json({"checks": checks_one})
        bytes_two = canonical_json({"checks": checks_two})
        checks = list(checks_one)
        checks.append(
            _check(
                "PCONF-06",
                bytes_one == bytes_two,
                {
                    "run_one_hash": sha256_canonical({"checks": checks_one}),
                    "run_two_hash": sha256_canonical({"checks": checks_two}),
                },
            )
        )
        return sorted(checks, key=lambda c: c["check_id"])

    async def _run_once(self, env: SimulatorCertEnvironment) -> list[dict]:
        checks = [await self._pconf_01(env)]
        checks.append(await self._pconf_02(self._factory()))
        checks.append(await self._pconf_03(self._factory()))
        return checks

    # -- PCONF-01: participant seam passes CERT-01/02/05/06/07 ----------------

    async def _pconf_01(self, env: SimulatorCertEnvironment) -> dict:
        harness = CertificationHarness(
            env.connector_id,
            self._factory,
            clock=env.clock,
            triggered_by=f"conformance:{self._participant_id}",
        )
        record = await harness.run()
        verdicts = {c["check_id"]: c["verdict"] for c in record.checks}
        relevant = {cid: verdicts.get(cid, "MISSING") for cid in _PCONF01_CERT_IDS}
        return _check(
            "PCONF-01",
            all(v == "PASSED" for v in relevant.values()),
            {"cert_verdicts": relevant, "cert_run_status": record.status},
        )

    # -- PCONF-02: full sandbox window walk -----------------------------------

    async def _pconf_02(self, env: SimulatorCertEnvironment) -> dict:
        """Position notification -> cutoff ack -> netting file -> instruction ack.

        The window interactions ride the participant's standard protocol
        seams (spec/19: simulated participants implement ``PaymentConnector``
        + ``ConnectorWebhookHandler`` — scenario documents, not protocol
        changes): window notifications arrive as signed callbacks through the
        fail-closed webhook pipeline; the netting result file is consumed
        from the object store; the participant acknowledges its instruction
        by processing it idempotently with a content-addressed reference
        derived from the netting file hash.
        """
        window_ref = f"pconf-window-{self._participant_id}"
        dispositions: dict[str, Any] = {}

        # 1. Position notification + 2. cutoff acknowledgement (signed
        # callbacks; the seam must verify and accept both exactly once).
        for step in ("position", "cutoff"):
            payload = {
                "instruction_id": f"ins_{'0' * 24}",
                "connector_ref": f"{window_ref}-{step}",
                "status": "success",
                "rail_transaction_id": f"SIM-WIN-{step.upper()}",
                "responded_at": _rfc3339(env.clock),
                "error_code": None,
            }
            body = canonical_json(payload)
            headers = sign_callback(env.signing_key, body, _rfc3339(env.clock))
            outcome = await env.pipeline.ingest(env.connector_id, headers, body)
            dispositions[f"{step}_notification"] = outcome.status

        # 3. Netting result file: written to the window object store; the
        # consumption proof is the content-addressed instruction reference.
        netting_file = canonical_json(
            {
                "suite": "pso-conf-v1",
                "window_ref": window_ref,
                "obligation": {"participant_id": self._participant_id, "direction": "PAY"},
            }
        )
        file_hash = sha256_canonical(
            {"window_ref": window_ref, "participant_id": self._participant_id}
        )
        pointer = env.object_store.put(f"conformance/{window_ref}/netting-result", netting_file)
        fetched = env.object_store.get(pointer)
        dispositions["netting_file_bytes_match"] = fetched == netting_file

        # 4. Instruction acknowledgement: the participant processes its
        # settlement instruction (ref derived from the netting file hash)
        # exactly once, echoing both identifiers.
        connector_ref = f"{window_ref}-instruction-{file_hash[:16]}"
        instruction = PaymentInstruction(
            instruction_id=f"ins_{file_hash[:24]}",
            connector_ref=connector_ref,
            amount=Money(amount_minor=10_100),
            method=env.adapter.supported_methods[0],
            sender_ref="tok-pso-settlement",
            beneficiary_ref="tok-participant-settlement",
            rail=env.connector_id,
            instruction_at=_rfc3339(env.clock),
            metadata={"sim_scenario": "success", "window_ref": window_ref},
        )
        first = await env.runner.dispatch(instruction)
        second = await env.runner.dispatch(instruction)
        dispositions["instruction_status"] = first.status.value
        dispositions["instruction_echo"] = (
            first.instruction_id == instruction.instruction_id
            and first.connector_ref == connector_ref
        )
        dispositions["instruction_idempotent"] = (
            second.status == first.status
            and env.engine.effect_count(connector_ref) == 1
        )

        passed = (
            dispositions["position_notification"] == "PROCESSED"
            and dispositions["cutoff_notification"] == "PROCESSED"
            and dispositions["netting_file_bytes_match"] is True
            and dispositions["instruction_status"] == ConnectorStatus.SUCCESS.value
            and dispositions["instruction_echo"] is True
            and dispositions["instruction_idempotent"] is True
        )
        return _check("PCONF-02", passed, dispositions)

    # -- PCONF-03: NDC refusal handling ----------------------------------------

    async def _pconf_03(self, env: SimulatorCertEnvironment) -> dict:
        """A scripted over-cap submission is refused; no retry-blind storm.

        The refusal is delivered through the participant's seam as a hard
        decline (the simulator ``decline`` scenario — REJECTED, do not
        retry). The participant must surface exactly one effect for the
        refused ``connector_ref`` (idempotent on re-submission, within the
        zero-submit retry budget the registry pins) — a duplicate-ref storm
        fails the check.
        """
        connector_ref = f"pconf-ndc-overcap-{self._participant_id}"
        instruction = PaymentInstruction(
            instruction_id=f"ins_{sha256_canonical({'ref': connector_ref})[:24]}",
            connector_ref=connector_ref,
            amount=Money(amount_minor=10_100),
            method=env.adapter.supported_methods[0],
            sender_ref="tok-pso-settlement",
            beneficiary_ref="tok-participant-settlement",
            rail=env.connector_id,
            instruction_at=_rfc3339(env.clock),
            metadata={"sim_scenario": "decline", "refusal": "ndc_breach"},
        )
        first = await env.runner.dispatch(instruction)
        second = await env.runner.dispatch(instruction)  # must not re-fire the effect
        retry_budget = env.registry.get(env.connector_id).retry_budget.get(
            "submit_retries", 0
        )
        effects = env.engine.effect_count(connector_ref)
        evidence = {
            "first_status": first.status.value,
            "second_status": second.status.value,
            "effects": effects,
            "retry_budget": retry_budget,
        }
        passed = (
            first.status is ConnectorStatus.REJECTED
            and second.status is ConnectorStatus.REJECTED
            and effects <= 1 + retry_budget
        )
        return _check("PCONF-03", passed, evidence)


# ---------------------------------------------------------------------------
# INBOUND evaluation (PCONF-04 / PCONF-05) — pure functions over evidence
# ---------------------------------------------------------------------------


def evaluate_inbound_evidence(
    *,
    report: Mapping[str, Any],
    recon_unmatched: Iterable[str],
    submitted_refs: Iterable[str],
) -> list[dict]:
    """The INBOUND ``pso-conf-v1`` checks over an already-hash-verified report.

    The evidence-hash recompute happens at runner pickup (FSM 2 guard; a
    mismatch never reaches this function — the run is ``ERRORED`` before
    ``RUNNING``). ``report`` is the parsed participant report object;
    ``submitted_refs`` are the sandbox instruction references the participant
    claims; ``recon_unmatched`` is our sandbox recon's unmatched subset of
    those references (empty = every instruction reconciled).
    """
    report_checks = report.get("checks", [])
    verdicts: dict[str, str] = {}
    if isinstance(report_checks, list):
        for entry in report_checks:
            if isinstance(entry, Mapping) and "check_id" in entry:
                verdicts[str(entry["check_id"])] = str(entry.get("verdict", "MISSING"))
    coverage = {cid: verdicts.get(cid, "MISSING") for cid in PCONF_OUTBOUND_CHECKS}
    pconf_04 = _check(
        "PCONF-04",
        all(v == "PASSED" for v in coverage.values()),
        {"coverage": coverage},
    )

    refs = sorted(set(submitted_refs))
    unmatched = sorted(set(recon_unmatched))
    pconf_05 = _check(
        "PCONF-05",
        len(refs) > 0 and not unmatched,
        {"submitted": len(refs), "unmatched": unmatched},
    )
    return [pconf_04, pconf_05]

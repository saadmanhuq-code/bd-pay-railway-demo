"""Deterministic goAML portal simulator (spec/12 §F, §K).

:class:`ScriptedGoamlPortal` is the richer scriptable counterpart of the
connector's built-in :class:`~bdpay.connectors.identity.goaml.
SimulatedGoamlPortal` (re-exported here as the one import surface). It speaks
the same ``PortalTransport`` protocol and adds the per-scenario controls the
§K suite needs:

- ``fail_next(n)`` — n consecutive HTTP-500 submit failures
  (``portal_500_storm``: 50 consecutive 500s and the filing is STILL QUEUED
  with a growing retry_count — CERT-G1, a filing is never dropped);
- ``reject_entity(ref)`` / ``clear_reject(ref)`` — XSD/business-rule rejects
  at ack time (``xsd_reject_then_requeue``);
- ``ack_after_polls(ref, n)`` — the portal acknowledges only after n ack
  polls (models the 30-minute ack cadence without wall time).

Receipts are content-addressed from the submitted XML; nothing is random.
"""

from __future__ import annotations

from bdpay.connectors.identity.goaml import PortalReply, SimulatedGoamlPortal
from bdpay.platform.canonical import sha256_canonical

__all__ = ["ScriptedGoamlPortal", "SimulatedGoamlPortal"]


class ScriptedGoamlPortal:
    """Deterministic goAML portal endpoint behind the ``PortalTransport`` port."""

    def __init__(self) -> None:
        self._fail_remaining = 0
        self._reject_refs: set[str] = set()
        self._ack_after: dict[str, int] = {}  # submission_ref -> polls remaining
        self._entity_by_submission: dict[str, str] = {}
        self.submissions: list[str] = []  # entity_references accepted, in order
        self.submit_attempts = 0

    # -- scripting -------------------------------------------------------------------

    def fail_next(self, count: int) -> None:
        self._fail_remaining = count

    def reject_entity(self, entity_reference: str) -> None:
        self._reject_refs.add(entity_reference)

    def clear_reject(self, entity_reference: str) -> None:
        self._reject_refs.discard(entity_reference)

    def ack_after_polls(self, entity_reference: str, polls: int) -> None:
        self._ack_after[entity_reference] = polls

    # -- PortalTransport -----------------------------------------------------------------

    @staticmethod
    def _entity_reference(xml_bytes: bytes) -> str:
        from defusedxml.ElementTree import fromstring

        return fromstring(xml_bytes.decode("utf-8")).findtext("entity_reference") or ""

    async def submit_report(self, xml_bytes: bytes) -> PortalReply:
        self.submit_attempts += 1
        if self._fail_remaining > 0:
            self._fail_remaining -= 1
            return PortalReply(status_code=500, body={"error": "portal_unavailable"})
        entity_reference = self._entity_reference(xml_bytes)
        submission_ref = "GOAML-" + sha256_canonical({"xml": xml_bytes.decode("utf-8")})[:12]
        self._entity_by_submission[submission_ref] = entity_reference
        self.submissions.append(entity_reference)
        return PortalReply(status_code=200, body={"submission_ref": submission_ref})

    async def poll_ack(self, submission_ref: str) -> PortalReply:
        entity_reference = self._entity_by_submission.get(submission_ref, "")
        if entity_reference in self._reject_refs:
            return PortalReply(
                status_code=200,
                body={"status": "rejected", "reason": "schema_validation_failed"},
            )
        remaining = self._ack_after.get(entity_reference, 0)
        if remaining > 0:
            self._ack_after[entity_reference] = remaining - 1
            return PortalReply(status_code=200, body={"status": "processing"})
        return PortalReply(status_code=200, body={"status": "accepted"})

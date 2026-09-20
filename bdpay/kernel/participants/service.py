"""ParticipantOnboardingService — spec/19 PSO-1 institutional onboarding.

Drives the v2 ParticipantOnboarding FSM (D-19-1 single institutional path):
application -> document verification (five PDocument classes, maker-checker)
-> MOU_SIGNED (non-binding, contingent-on-license founding instrument) ->
CONFORMANCE_TESTING (bidirectional ConformanceRuns on the spec/10 harness)
-> CONFORMANCE_PASSED -> two-eyes activation through the platform
ApprovalGateway -> ACTIVE, plus the unchanged spec/08 rows (suspend /
reinstate / terminate / reject).

Refusal-first throughout: every transition resolves against
:data:`PARTICIPANT_V2_TABLE` (undeclared pairs are DENIED); every transition
writes one ``audit_events`` row; events go via the outbox only; and every
mutating entry point refuses with :class:`PsoModeError` under
``PLATFORM_MODE=PSP`` (spec/19 §Scope: the error is never
caught-and-continued).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import replace
from datetime import date, datetime
from typing import Any

from bdpay.kernel.fsm import TransitionRule
from bdpay.kernel.ids_ext import make_kernel_id
from bdpay.kernel.participants.config import ParticipantOnboardingConfig
from bdpay.kernel.participants.conformance_suite import evaluate_inbound_evidence
from bdpay.kernel.participants.errors import PsoModeError
from bdpay.kernel.participants.models import (
    ConformanceRunRecord,
    ParticipantDocumentRecord,
    ParticipantMouRecord,
    ParticipantRecord,
)
from bdpay.kernel.participants.pids import make_participant_id
from bdpay.kernel.participants.ports import (
    NetDebitCapPort,
    ObjectStorePort,
    ParticipantLedgerPort,
    PsoBatteryGatePort,
)
from bdpay.kernel.participants.states import (
    CONFORMANCE_RUN_TABLE,
    CONFORMANCE_RUN_TIMEOUT_SECONDS,
    DOCUMENT_CLASSES,
    PARTICIPANT_V2_TABLE,
)
from bdpay.kernel.participants.stores import ParticipantStore
from bdpay.platform.canonical import canonical_json, sha256_canonical
from bdpay.platform.clock import Clock
from bdpay.platform.config import Settings
from bdpay.platform.errors import (
    AuthorizationError,
    ConflictError,
    InvalidRequestError,
    NotFoundError,
)
from bdpay.platform.ids import make_id
from bdpay.platform.interfaces import (
    ApprovalPort,
    AuditEventSpec,
    AuditPort,
    OutboxPort,
)
from bdpay.platform.outbox import build_event
from bdpay.platform.pii import redact

__all__ = [
    "ACTIVATION_ACTION_TYPE",
    "ParticipantOnboardingService",
]

PRODUCER = "onboarding-service@1"

#: Two-eyes catalogue entry for participant activation (always required).
#: The spec/15 catalogue predates spec/19; the entry is supplied additively
#: at ApprovalGateway composition (errata P19-2).
ACTIVATION_ACTION_TYPE = "participant_activation"

_SHA256_HEX_LEN = 64

#: BB_LICENSE metadata keys the §API A contract requires.
_BB_LICENSE_KEYS = ("bb_license_number", "bb_license_type", "bb_license_expiry")


def _utc_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise InvalidRequestError(
            "date fields must be YYYY-MM-DD", code="invalid_date"
        ) from exc


class ParticipantOnboardingService:
    """The spec/19 PSO-1 onboarding engine (logical ``onboarding-service``)."""

    def __init__(
        self,
        store: ParticipantStore,
        *,
        clock: Clock,
        audit: AuditPort,
        outbox: OutboxPort,
        approvals: ApprovalPort,
        settings: Settings,
        config: ParticipantOnboardingConfig | None = None,
        ndc: NetDebitCapPort,
        ledger: ParticipantLedgerPort,
        battery_gate: PsoBatteryGatePort,
        object_store: ObjectStorePort,
    ) -> None:
        self._store = store
        self._clock = clock
        self._audit = audit
        self._outbox = outbox
        self._approvals = approvals
        self._settings = settings
        self._config = config if config is not None else ParticipantOnboardingConfig()
        self._ndc = ndc
        self._ledger = ledger
        self._battery_gate = battery_gate
        self._objects = object_store

    # ------------------------------------------------------------------
    # mode gate (spec/19 §Scope item 3 — engine layer)
    # ------------------------------------------------------------------

    def _require_pso_mode(self) -> None:
        if self._settings.platform_mode != "PSO":
            raise PsoModeError(
                "participant onboarding is a PSO-only operation; "
                f"PLATFORM_MODE={self._settings.platform_mode}"
            )

    # ------------------------------------------------------------------
    # application intake (spec/08 §8.11 surface; v2 path target per D-19-1)
    # ------------------------------------------------------------------

    def submit_application(
        self,
        *,
        institution_name: str,
        institution_type: str,
        bb_license_number: str | None = None,
        actor_id: str,
        conn: Any | None = None,
    ) -> ParticipantRecord:
        """Create the ``participants`` row in ``APPLICATION_SUBMITTED``."""
        self._require_pso_mode()
        now = self._clock.now()
        participant_id = make_id(
            "part",
            {
                "institution_name": institution_name,
                "institution_type": institution_type,
                "bb_license_number": bb_license_number,
            },
        )
        existing = self._store.get_participant(participant_id, conn=conn)
        if existing is not None:
            return existing  # content-addressed idempotency
        record = ParticipantRecord(
            participant_id=participant_id,
            institution_name=institution_name,
            institution_type=institution_type,
            bb_license_number=bb_license_number,
            created_at=now,
            updated_at=now,
        )
        self._store.insert_participant(record, conn=conn)
        self._audit_event(
            "PARTICIPANT_APPLICATION_SUBMITTED",
            record,
            actor_id=actor_id,
            payload={"institution_type": institution_type},
            conn=conn,
        )
        return record

    def accept_application(
        self, participant_id: str, *, actor_id: str, conn: Any | None = None
    ) -> ParticipantRecord:
        """``application_accepted`` -> DOCUMENT_VERIFICATION_PENDING (v2 path)."""
        self._require_pso_mode()
        record = self._require_participant(participant_id, conn=conn)
        record = self._advance(
            record,
            "application_accepted",
            actor_id=actor_id,
            audit_type="PARTICIPANT_DOCS_REQUESTED",
            conn=conn,
        )
        self._emit(
            "participant.application_submitted",
            participant_id,
            {"participant_id": participant_id, "institution_type": record.institution_type},
            conn=conn,
        )
        return record

    # ------------------------------------------------------------------
    # documents (spec/19 §API A; PDocument records)
    # ------------------------------------------------------------------

    def register_document(
        self,
        participant_id: str,
        *,
        document_class: str,
        storage_pointer: str,
        content_sha256: str,
        metadata: Mapping[str, Any] | None = None,
        uploaded_by: str,
        conn: Any | None = None,
    ) -> ParticipantDocumentRecord:
        """Register one onboarding document (re-upload supersedes)."""
        self._require_pso_mode()
        record = self._require_participant(participant_id, conn=conn)
        if record.kyb_status != "DOCUMENT_VERIFICATION_PENDING":
            raise ConflictError(
                "documents are registered while the participant is in "
                "DOCUMENT_VERIFICATION_PENDING",
                code="invalid_state_transition",
            )
        if document_class not in DOCUMENT_CLASSES:
            raise InvalidRequestError(
                f"document_class must be one of {DOCUMENT_CLASSES}",
                code="unknown_document_class",
            )
        if (
            not isinstance(content_sha256, str)
            or len(content_sha256) != _SHA256_HEX_LEN
            or any(c not in "0123456789abcdef" for c in content_sha256.lower())
        ):
            raise InvalidRequestError(
                "content_sha256 must be a 64-char hex digest", code="invalid_content_hash"
            )
        metadata_dict = dict(metadata or {})
        self._screen_metadata(metadata_dict, document_class)
        now = self._clock.now()

        # Re-upload supersedes: exactly one live document per class.
        for existing in self._store.list_documents(participant_id, conn=conn):
            if existing.document_class == document_class and existing.review_status in (
                "PENDING",
                "VERIFIED",
            ):
                if existing.content_sha256 == content_sha256:
                    return existing  # content-addressed idempotency
                self._store.update_document(
                    replace(existing, review_status="SUPERSEDED"), conn=conn
                )
        document = ParticipantDocumentRecord(
            document_id=make_participant_id(
                "pdoc",
                {
                    "participant_id": participant_id,
                    "document_class": document_class,
                    "content_sha256": content_sha256,
                },
            ),
            participant_id=participant_id,
            document_class=document_class,
            storage_pointer=storage_pointer,
            content_sha256=content_sha256,
            metadata=metadata_dict,
            uploaded_by=uploaded_by,
            uploaded_at=now,
        )
        self._store.insert_document(document, conn=conn)
        self._audit_event(
            "PARTICIPANT_DOCUMENT_UPLOADED",
            record,
            actor_id=uploaded_by,
            payload={"document_id": document.document_id, "document_class": document_class},
            conn=conn,
        )
        return document

    def _screen_metadata(self, metadata: dict[str, Any], document_class: str) -> None:
        """PII screening at intake — reject, not redact (errata P19-5).

        Raw PII (mobile numbers, NIDs, emails, PANs) lives inside the stored
        object only, never in the row. Contact name/role stay in metadata;
        ``SETTLEMENT_ACCOUNT_DETAILS`` carries the vault token, never a raw
        account number.
        """
        try:
            blob = canonical_json(metadata).decode("utf-8")
        except Exception as exc:
            raise InvalidRequestError(
                "document metadata must be canonical-JSON-serializable",
                code="invalid_metadata",
            ) from exc
        if redact(blob) != blob:
            raise InvalidRequestError(
                "document metadata must not contain raw PII; PII belongs inside "
                "the stored document object (reject-not-redact intake rule)",
                code="pii_in_metadata",
            )
        if document_class == "BB_LICENSE":
            missing = [k for k in _BB_LICENSE_KEYS if not metadata.get(k)]
            if missing:
                raise InvalidRequestError(
                    f"BB_LICENSE metadata must carry {list(_BB_LICENSE_KEYS)}; "
                    f"missing {missing}",
                    code="bb_license_metadata_incomplete",
                )
            _utc_date(metadata["bb_license_expiry"])
        if document_class == "SETTLEMENT_ACCOUNT_DETAILS" and not metadata.get(
            "settlement_account_token"
        ):
            raise InvalidRequestError(
                "SETTLEMENT_ACCOUNT_DETAILS metadata must carry the tokenized "
                "settlement-account reference (settlement_account_token); raw "
                "account numbers never appear in this payload",
                code="settlement_account_token_required",
            )

    def verify_document(
        self,
        participant_id: str,
        document_id: str,
        *,
        verified_by: str,
        conn: Any | None = None,
    ) -> ParticipantDocumentRecord:
        """Operator marks the document verified (maker-checker)."""
        self._require_pso_mode()
        record = self._require_participant(participant_id, conn=conn)
        document = self._require_document(participant_id, document_id, conn=conn)
        if document.review_status != "PENDING":
            raise ConflictError(
                f"document is {document.review_status}; only PENDING documents verify",
                code="invalid_state_transition",
            )
        if verified_by == document.uploaded_by:
            raise AuthorizationError(
                "the verifying operator must differ from the uploading operator",
                code="verifier_must_differ",
            )
        now = self._clock.now()
        if document.document_class == "BB_LICENSE":
            expiry = _utc_date(document.metadata["bb_license_expiry"])
            if expiry < now.date():
                # spec/19 FSM 1: license_invalid -> REJECTED.
                self._advance(
                    record,
                    "license_invalid",
                    actor_id=verified_by,
                    audit_type="PARTICIPANT_LICENSE_INVALID",
                    payload={"document_id": document_id, "bb_license_expiry": str(expiry)},
                    conn=conn,
                )
                self._emit(
                    "participant.kyb_rejected",
                    participant_id,
                    {"participant_id": participant_id, "reason_code": "LICENSE_EXPIRED"},
                    conn=conn,
                )
                raise ConflictError(
                    "BB license is expired; participant application rejected",
                    code="license_invalid",
                )
        rule = PARTICIPANT_V2_TABLE.resolve(record.kyb_status, "document_verified")
        updated = replace(
            document, review_status="VERIFIED", verified_by=verified_by, verified_at=now
        )
        self._store.update_document(updated, conn=conn)
        if document.document_class == "BB_LICENSE":
            self._write_license_verification(record, document, verified_by, now, conn=conn)
            self._emit(
                "participant.license_verified",
                participant_id,
                {
                    "participant_id": participant_id,
                    "bb_license_number": document.metadata["bb_license_number"],
                },
                conn=conn,
            )
        self._audit_event(
            "PARTICIPANT_DOCUMENT_VERIFIED",
            record,
            actor_id=verified_by,
            payload={"document_id": document_id, "document_class": document.document_class},
            from_state=rule.from_state,
            to_state=rule.to_state,
            conn=conn,
        )
        self._emit(
            "participant.document_verified",
            participant_id,
            {
                "participant_id": participant_id,
                "document_class": document.document_class,
                "pdoc_id": document_id,
            },
            conn=conn,
        )
        return updated

    def _write_license_verification(
        self,
        record: ParticipantRecord,
        document: ParticipantDocumentRecord,
        verified_by: str,
        now: datetime,
        *,
        conn: Any | None,
    ) -> None:
        """The spec/08 ``participant_license_verifications`` row (D-19-1)."""
        metadata = document.metadata
        self._store.insert_license_verification(
            {
                # spec/08 `plv` prefix via the kernel's E18-class shim.
                "verification_id": make_kernel_id(
                    "plv",
                    {
                        "participant_id": record.participant_id,
                        "bb_license_number": metadata["bb_license_number"],
                        "verified_at": now,
                    },
                ),
                "participant_id": record.participant_id,
                "bb_license_number": metadata["bb_license_number"],
                "bb_license_type": metadata["bb_license_type"],
                "bb_license_expiry": _utc_date(metadata["bb_license_expiry"]),
                "verification_method": "MANUAL_BB_REGISTRY",
                "verified_by": verified_by,
                "verified_at": now,
                "verification_notes": None,
                "document_pointer": document.storage_pointer,
                "schema_version": 1,
                "created_at": now,
            },
            conn=conn,
        )
        updated = replace(
            self._require_participant(record.participant_id, conn=conn),
            bb_license_number=metadata["bb_license_number"],
            bb_license_type=metadata["bb_license_type"],
            bb_license_expiry=_utc_date(metadata["bb_license_expiry"]),
            updated_at=now,
        )
        self._store.update_participant(updated, conn=conn)

    def reject_document(
        self,
        participant_id: str,
        document_id: str,
        *,
        reason: str,
        actor_id: str,
        conn: Any | None = None,
    ) -> ParticipantDocumentRecord:
        """Reject with mandatory reason; participant awaits re-upload."""
        self._require_pso_mode()
        record = self._require_participant(participant_id, conn=conn)
        document = self._require_document(participant_id, document_id, conn=conn)
        if not reason or not isinstance(reason, str):
            raise InvalidRequestError(
                "document rejection requires a reason", code="reason_required"
            )
        if document.review_status != "PENDING":
            raise ConflictError(
                f"document is {document.review_status}; only PENDING documents reject",
                code="invalid_state_transition",
            )
        rule = PARTICIPANT_V2_TABLE.resolve(record.kyb_status, "document_rejected")
        updated = replace(document, review_status="REJECTED", reject_reason=redact(reason))
        self._store.update_document(updated, conn=conn)
        self._audit_event(
            "PARTICIPANT_DOCUMENT_REJECTED",
            record,
            actor_id=actor_id,
            payload={
                "document_id": document_id,
                "document_class": document.document_class,
                "reason": redact(reason),
            },
            from_state=rule.from_state,
            to_state=rule.to_state,
            conn=conn,
        )
        return updated

    # ------------------------------------------------------------------
    # MOU (spec/19 §API A; ParticipantMou)
    # ------------------------------------------------------------------

    def record_mou(
        self,
        participant_id: str,
        *,
        mou_template_version: str,
        executed_date: str,
        signed_document_pointer: str,
        signed_document_sha256: str,
        counterparty_signatory: Mapping[str, str],
        founding_fee_terms_ref: str | None = None,
        recorded_by: str,
        conn: Any | None = None,
    ) -> tuple[ParticipantMouRecord, ParticipantRecord]:
        """Record the executed MOU and fire ``mou_recorded -> MOU_SIGNED``."""
        self._require_pso_mode()
        record = self._require_participant(participant_id, conn=conn)
        rule = PARTICIPANT_V2_TABLE.resolve(record.kyb_status, "mou_recorded")
        verified = self.verified_document_classes(participant_id, conn=conn)
        missing = [c for c in DOCUMENT_CLASSES if c not in verified]
        if missing:
            raise ConflictError(
                "the MOU is recorded only after all five document classes are "
                f"VERIFIED; missing {missing}",
                code="documents_not_verified",
            )
        signatory = dict(counterparty_signatory or {})
        blob = canonical_json(signatory).decode("utf-8")
        if redact(blob) != blob:
            raise InvalidRequestError(
                "counterparty_signatory must not contain raw PII patterns",
                code="pii_in_metadata",
            )
        now = self._clock.now()
        mou = ParticipantMouRecord(
            mou_id=make_participant_id(
                "pmou",
                {
                    "participant_id": participant_id,
                    "mou_template_version": mou_template_version,
                    "executed_date": executed_date,
                },
            ),
            participant_id=participant_id,
            mou_template_version=mou_template_version,
            executed_date=_utc_date(executed_date),
            signed_document_pointer=signed_document_pointer,
            signed_document_sha256=signed_document_sha256,
            counterparty_signatory=signatory,
            founding_fee_terms_ref=founding_fee_terms_ref,
            recorded_by=recorded_by,
            recorded_at=now,
        )
        self._store.insert_mou(mou, conn=conn)
        record = replace(record, kyb_status=rule.to_state, mou_id=mou.mou_id, updated_at=now)
        self._store.update_participant(record, conn=conn)
        self._audit_event(
            "PARTICIPANT_MOU_SIGNED",
            record,
            actor_id=recorded_by,
            payload={"mou_id": mou.mou_id, "mou_template_version": mou_template_version},
            from_state=rule.from_state,
            to_state=rule.to_state,
            conn=conn,
        )
        self._emit(
            "participant.mou_signed",
            participant_id,
            {
                "participant_id": participant_id,
                "mou_id": mou.mou_id,
                "mou_template_version": mou_template_version,
            },
            conn=conn,
        )
        return mou, record

    # ------------------------------------------------------------------
    # conformance runs (spec/19 FSM 2; drives the spec/10 harness)
    # ------------------------------------------------------------------

    def queue_conformance_run(
        self,
        participant_id: str,
        *,
        direction: str,
        suite_version: str | None = None,
        evidence: Mapping[str, Any] | None = None,
        triggered_by: str,
        conn: Any | None = None,
    ) -> ConformanceRunRecord:
        """Queue a run; first run of a cycle fires ``conformance_started``."""
        self._require_pso_mode()
        record = self._require_participant(participant_id, conn=conn)
        suite = suite_version or self._config.suite_version
        if record.kyb_status not in ("MOU_SIGNED", "CONFORMANCE_TESTING"):
            raise ConflictError(
                "conformance runs are queued from MOU_SIGNED or CONFORMANCE_TESTING",
                code="invalid_state_transition",
            )
        evidence_dict = dict(evidence or {})
        evidence_pointer: str | None = None
        evidence_sha256: str | None = None
        if direction == "INBOUND":
            evidence_pointer = evidence_dict.get("report_pointer")
            evidence_sha256 = evidence_dict.get("report_sha256")
            if not evidence_pointer or not evidence_sha256:
                raise InvalidRequestError(
                    "INBOUND runs require evidence.report_pointer and "
                    "evidence.report_sha256 (the uploaded report object)",
                    code="inbound_evidence_required",
                )
        now = self._clock.now()
        run = ConformanceRunRecord(
            conformance_run_id=make_participant_id(
                "confr",
                {
                    "participant_id": participant_id,
                    "direction": direction,
                    "suite_version": suite,
                    # The make_id payload key is the spec's "started_at"; the
                    # value is the queue instant so the content-addressed id
                    # exists from creation (errata P19-8). The row's
                    # started_at column is set at runner pickup.
                    "started_at": now,
                },
            ),
            participant_id=participant_id,
            direction=direction,
            suite_version=suite,
            evidence_pointer=evidence_pointer,
            evidence_sha256=evidence_sha256,
            triggered_by=triggered_by,
            created_at=now,
        )
        self._store.insert_conformance_run(run, conn=conn)
        if record.kyb_status == "MOU_SIGNED":
            # P19-4: first run of the current attempt cycle.
            record = self._advance(
                record,
                "conformance_started",
                actor_id=triggered_by,
                audit_type="PARTICIPANT_CONFORMANCE_STARTED",
                payload={"conformance_run_id": run.conformance_run_id},
                conn=conn,
            )
            self._emit(
                "participant.conformance_started",
                participant_id,
                {
                    "participant_id": participant_id,
                    "conformance_run_id": run.conformance_run_id,
                    "direction": direction,
                },
                conn=conn,
            )
        return run

    def pickup_conformance_run(
        self, conformance_run_id: str, *, conn: Any | None = None
    ) -> ConformanceRunRecord:
        """``runner_pickup`` — INBOUND evidence hash recomputed first.

        A forged or garbled INBOUND report leaves the run ``ERRORED`` before
        ``RUNNING`` (P19-3); the participant's onboarding state is untouched.
        """
        self._require_pso_mode()
        run = self._require_run(conformance_run_id, conn=conn)
        now = self._clock.now()
        if run.direction == "INBOUND":
            try:
                report_bytes = self._objects.get(run.evidence_pointer or "")
            except KeyError:
                report_bytes = None
            recomputed = (
                None if report_bytes is None else hashlib.sha256(report_bytes).hexdigest()
            )
            if recomputed is None or recomputed != run.evidence_sha256:
                rule = CONFORMANCE_RUN_TABLE.resolve(run.status, "evidence_hash_mismatch")
                errored = replace(run, status=rule.to_state, finished_at=now)
                self._store.advance_conformance_run(
                    errored, from_status=rule.from_state, conn=conn
                )
                self._run_audit(
                    errored,
                    "CONFORMANCE_RUN_EVIDENCE_MISMATCH",
                    actor_id="onboarding-service",
                    payload={
                        "recomputed_sha256": recomputed,
                        "declared_sha256": run.evidence_sha256,
                    },
                    from_state=rule.from_state,
                    to_state=rule.to_state,
                    conn=conn,
                )
                self._emit_run_completed(errored, conn=conn)
                return errored
        rule = CONFORMANCE_RUN_TABLE.resolve(run.status, "runner_pickup")
        running = replace(run, status=rule.to_state, started_at=now)
        self._store.advance_conformance_run(running, from_status=rule.from_state, conn=conn)
        self._run_audit(
            running,
            "CONFORMANCE_RUN_STARTED",
            actor_id="onboarding-service",
            payload={"direction": run.direction},
            from_state=rule.from_state,
            to_state=rule.to_state,
            conn=conn,
        )
        return running

    async def execute_conformance_run(
        self,
        conformance_run_id: str,
        *,
        outbound_suite: Any | None = None,
        recon_unmatched: tuple[str, ...] = (),
        conn: Any | None = None,
    ) -> ConformanceRunRecord:
        """Execute a RUNNING run and seal the deterministic report.

        ``outbound_suite`` is the OUTBOUND executor (the
        :class:`~bdpay.kernel.participants.conformance_suite.PsoConformanceOutboundSuite`
        built over the participant's registered connector seam). INBOUND runs
        evaluate the already-hash-verified uploaded report plus the sandbox
        recon view (``recon_unmatched``).
        """
        self._require_pso_mode()
        run = self._require_run(conformance_run_id, conn=conn)
        if run.status != "RUNNING":
            raise ConflictError(
                f"run is {run.status}; only RUNNING runs execute",
                code="invalid_state_transition",
            )
        try:
            if run.direction == "OUTBOUND":
                if outbound_suite is None:
                    raise InvalidRequestError(
                        "OUTBOUND execution requires the participant's suite executor",
                        code="outbound_suite_required",
                    )
                checks = await outbound_suite.run()
            else:
                report_bytes = self._objects.get(run.evidence_pointer or "")
                report = json.loads(report_bytes)
                checks = evaluate_inbound_evidence(
                    report=report,
                    recon_unmatched=recon_unmatched,
                    submitted_refs=tuple(report.get("sandbox_instruction_refs", ())),
                )
        except (InvalidRequestError, ConflictError):
            raise
        except Exception as exc:
            return self._seal_run(
                run,
                trigger="errored",
                checks=(
                    {
                        "check_id": "SUITE",
                        "verdict": "ERRORED",
                        "evidence_hash": sha256_canonical({"error_type": type(exc).__name__}),
                    },
                ),
                conn=conn,
            )
        now = self._clock.now()
        elapsed = (now - run.started_at).total_seconds() if run.started_at else 0.0
        if elapsed > CONFORMANCE_RUN_TIMEOUT_SECONDS:
            # spec/10 timeout convention: any single run over 15 min ERRORED.
            return self._seal_run(run, trigger="errored", checks=tuple(checks), conn=conn)
        all_passed = bool(checks) and all(c["verdict"] == "PASSED" for c in checks)
        return self._seal_run(
            run,
            trigger="checks_passed" if all_passed else "check_failed",
            checks=tuple(checks),
            conn=conn,
        )

    def _seal_run(
        self,
        run: ConformanceRunRecord,
        *,
        trigger: str,
        checks: tuple[dict[str, Any], ...],
        conn: Any | None,
    ) -> ConformanceRunRecord:
        rule = CONFORMANCE_RUN_TABLE.resolve(run.status, trigger)
        now = self._clock.now()
        report = {
            "suite_version": run.suite_version,
            "participant_id": run.participant_id,
            "direction": run.direction,
            "checks": list(checks),
            "verdict": rule.to_state,
        }
        report_bytes = canonical_json(report)
        report_hash = sha256_canonical(report)
        report_pointer = self._objects.put(
            f"conformance/{run.conformance_run_id}/report", report_bytes
        )
        sealed = replace(
            run,
            status=rule.to_state,
            checks=checks,
            report_pointer=report_pointer,
            report_hash=report_hash,
            finished_at=now,
        )
        self._store.advance_conformance_run(sealed, from_status=rule.from_state, conn=conn)
        failed_checks = [c["check_id"] for c in checks if c["verdict"] != "PASSED"]
        self._run_audit(
            sealed,
            "CONFORMANCE_RUN_COMPLETED",
            actor_id="onboarding-service",
            payload={"verdict": rule.to_state, "failed_checks": failed_checks},
            from_state=rule.from_state,
            to_state=rule.to_state,
            conn=conn,
        )
        self._emit_run_completed(sealed, conn=conn)
        return sealed

    def evaluate_conformance(
        self, participant_id: str, *, actor_id: str = "onboarding-service", conn: Any | None = None
    ) -> ParticipantRecord:
        """Apply FSM 1's conformance guards after a run completes.

        Latest OUTBOUND and INBOUND runs both PASSED for the current suite
        version => ``conformance_passed``. A FAILED/ERRORED run =>
        ``conformance_failed`` (retry budget left) or
        ``conformance_exhausted`` (budget spent). Pickup-refused runs (P19-3:
        ERRORED with no ``started_at``) leave the participant untouched.
        """
        self._require_pso_mode()
        record = self._require_participant(participant_id, conn=conn)
        if record.kyb_status != "CONFORMANCE_TESTING":
            raise ConflictError(
                "conformance evaluation applies in CONFORMANCE_TESTING",
                code="invalid_state_transition",
            )
        suite = self._config.suite_version
        runs = [
            r
            for r in self._store.list_conformance_runs(participant_id, conn=conn)
            if r.suite_version == suite
        ]
        executed = [r for r in runs if r.started_at is not None]
        latest: dict[str, ConformanceRunRecord] = {}
        for run in sorted(executed, key=lambda r: r.created_at):
            if run.status in ("PASSED", "FAILED", "ERRORED"):
                latest[run.direction] = run
        if (
            "OUTBOUND" in latest
            and "INBOUND" in latest
            and latest["OUTBOUND"].status == "PASSED"
            and latest["INBOUND"].status == "PASSED"
        ):
            now = self._clock.now()
            record = self._advance(
                record,
                "conformance_passed",
                actor_id=actor_id,
                audit_type="PARTICIPANT_CONFORMANCE_PASSED",
                payload={"suite_version": suite},
                conn=conn,
                extra_updates={"conformance_passed_at": now},
            )
            self._emit(
                "participant.conformance_passed",
                participant_id,
                {"participant_id": participant_id, "suite_version": suite},
                conn=conn,
            )
            return record
        failures = [r for r in executed if r.status in ("FAILED", "ERRORED")]
        terminal_failure = any(
            latest.get(d) is not None and latest[d].status in ("FAILED", "ERRORED")
            for d in ("OUTBOUND", "INBOUND")
        )
        if not terminal_failure:
            return record  # still waiting on the other direction
        failed_run = next(
            latest[d]
            for d in ("OUTBOUND", "INBOUND")
            if latest.get(d) is not None and latest[d].status in ("FAILED", "ERRORED")
        )
        failed_check_ids = [
            c["check_id"] for c in failed_run.checks if c.get("verdict") != "PASSED"
        ]
        if len(failures) < self._config.conformance_retry_limit:
            record = self._advance(
                record,
                "conformance_failed",
                actor_id=actor_id,
                audit_type="PARTICIPANT_CONFORMANCE_FAILED",
                payload={
                    "conformance_run_id": failed_run.conformance_run_id,
                    "failed_checks": failed_check_ids,
                },
                conn=conn,
            )
            self._emit(
                "participant.conformance_failed",
                participant_id,
                {
                    "participant_id": participant_id,
                    "conformance_run_id": failed_run.conformance_run_id,
                    "failed_checks": failed_check_ids,
                },
                conn=conn,
            )
            return record
        record = self._advance(
            record,
            "conformance_exhausted",
            actor_id=actor_id,
            audit_type="PARTICIPANT_CONFORMANCE_EXHAUSTED",
            payload={"failed_run_count": len(failures)},
            conn=conn,
        )
        self._emit(
            "participant.kyb_rejected",
            participant_id,
            {"participant_id": participant_id, "reason_code": "CONFORMANCE_EXHAUSTED"},
            conn=conn,
        )
        return record

    # ------------------------------------------------------------------
    # activation (two-eyes through the platform ApprovalGateway)
    # ------------------------------------------------------------------

    def request_activation(
        self,
        participant_id: str,
        *,
        requested_by: str,
        settlement_agreement_pointer: str | None = None,
        conn: Any | None = None,
    ) -> tuple[Any, ParticipantRecord]:
        """spec/19 §API A activate — guards, then ApprovalRequest + FSM fire."""
        self._require_pso_mode()
        record = self._require_participant(participant_id, conn=conn)
        if record.kyb_status != "CONFORMANCE_PASSED":
            raise ConflictError(
                "activation is requested from CONFORMANCE_PASSED",
                code="invalid_state_transition",
            )
        cap_minor = self._ndc.active_cap_minor(participant_id)
        if cap_minor is None or cap_minor <= 0:
            raise ConflictError(
                "an active NetDebitCap is required before activation "
                "(spec/08 net-debit-cap flow)",
                code="net_debit_cap_required",
            )
        if not self._battery_gate.battery_is_fresh_and_passed():
            # spec/19 §API D PSO-readiness gate.
            raise ConflictError(
                "the latest PSO certification battery is stale or not PASSED",
                code="pso_battery_stale",
            )
        if not self._config.pso_license_active:
            # D-19-1: pre-license, founding participants park at CONFORMANCE_PASSED.
            raise ConflictError(
                "activation requires a live PSO license (PSO_LICENSE_ACTIVE)",
                code="license_not_active",
            )
        if not settlement_agreement_pointer:
            # D-19-1: the binding settlement agreement is a guard on activation.
            raise ConflictError(
                "the binding settlement-agreement pointer must be recorded "
                "at activation (D-19-1 guard)",
                code="settlement_agreement_required",
            )
        approval = self._approvals.request(
            action_type=ACTIVATION_ACTION_TYPE,
            subject_type="Participant",
            subject_id=participant_id,
            payload={
                "participant_id": participant_id,
                "net_debit_cap_minor": cap_minor,
                "settlement_agreement_pointer": settlement_agreement_pointer,
            },
            reason="participant activation (spec/19 PSO-1 two-eyes)",
            initiator_id=requested_by,
        )
        record = self._advance(
            record,
            "activation_requested",
            actor_id=requested_by,
            audit_type="PARTICIPANT_ACTIVATION_REQUESTED",
            payload={"approval_request_id": approval.approval_request_id},
            conn=conn,
        )
        return approval, record

    def complete_activation(
        self,
        participant_id: str,
        *,
        approval_request_id: str,
        net_debit_cap_id: str,
        actor_id: str,
        conn: Any | None = None,
    ) -> ParticipantRecord:
        """``ndc_assigned -> ACTIVE`` after second-operator approval."""
        self._require_pso_mode()
        record = self._require_participant(participant_id, conn=conn)
        approval = self._approvals.get(approval_request_id)
        if approval is None:
            raise NotFoundError(f"unknown approval request {approval_request_id}")
        if approval.state != "APPROVED" or approval.subject_id != participant_id:
            raise ConflictError(
                "activation completes only on an APPROVED request for this "
                "participant (two-eyes)",
                code="approval_not_granted",
            )
        cap_minor = self._ndc.active_cap_minor(participant_id)
        if cap_minor is None or cap_minor <= 0:
            raise ConflictError(
                "net_debit_cap_amount_minor must be > 0", code="net_debit_cap_required"
            )
        now = self._clock.now()
        record = self._advance(
            record,
            "ndc_assigned",
            actor_id=actor_id,
            audit_type="PARTICIPANT_ACTIVATED",
            payload={
                "approval_request_id": approval_request_id,
                "net_debit_cap_id": net_debit_cap_id,
            },
            conn=conn,
            extra_updates={"activated_at": now, "net_debit_cap_id": net_debit_cap_id},
        )
        # spec/08 side-effects unchanged: ledger participant_position +
        # net_debit_cap accounts; DB-trigger cap live.
        self._ledger.open_participant_accounts(participant_id, cap_minor=cap_minor, conn=conn)
        self._emit(
            "participant.activated",
            participant_id,
            {"participant_id": participant_id, "net_debit_cap_minor": cap_minor},
            conn=conn,
        )
        return record

    # ------------------------------------------------------------------
    # spec/08 rows, unchanged (suspend / reinstate / terminate / reject)
    # ------------------------------------------------------------------

    def suspend(
        self, participant_id: str, *, reason: str, actor_id: str, conn: Any | None = None
    ) -> ParticipantRecord:
        self._require_pso_mode()
        record = self._require_participant(participant_id, conn=conn)
        if not reason:
            raise InvalidRequestError("suspension requires a reason", code="reason_required")
        record = self._advance(
            record,
            "suspended",
            actor_id=actor_id,
            audit_type="PARTICIPANT_SUSPENDED",
            payload={"reason": redact(reason)},
            conn=conn,
            extra_updates={"suspended_reason": redact(reason)},
        )
        self._emit(
            "participant.suspended",
            participant_id,
            {"participant_id": participant_id},
            conn=conn,
        )
        return record

    def reinstate(
        self, participant_id: str, *, actor_id: str, conn: Any | None = None
    ) -> ParticipantRecord:
        self._require_pso_mode()
        record = self._require_participant(participant_id, conn=conn)
        return self._advance(
            record,
            "reinstated",
            actor_id=actor_id,
            audit_type="PARTICIPANT_REINSTATED",
            conn=conn,
            extra_updates={"suspended_reason": None},
        )

    def terminate(
        self, participant_id: str, *, reason: str, actor_id: str, conn: Any | None = None
    ) -> ParticipantRecord:
        self._require_pso_mode()
        record = self._require_participant(participant_id, conn=conn)
        if not reason:
            raise InvalidRequestError("termination requires a reason", code="reason_required")
        record = self._advance(
            record,
            "terminated",
            actor_id=actor_id,
            audit_type="PARTICIPANT_TERMINATED",
            payload={"reason": redact(reason)},
            conn=conn,
        )
        self._emit(
            "participant.terminated",
            participant_id,
            {"participant_id": participant_id},
            conn=conn,
        )
        return record

    def reject(
        self, participant_id: str, *, reason: str, actor_id: str, conn: Any | None = None
    ) -> ParticipantRecord:
        """FSM 1: any non-terminal --[rejected / operator + reason]--> REJECTED."""
        self._require_pso_mode()
        record = self._require_participant(participant_id, conn=conn)
        if not reason:
            raise InvalidRequestError(
                "rejection requires a mandatory reason", code="reason_required"
            )
        record = self._advance(
            record,
            "rejected",
            actor_id=actor_id,
            audit_type="PARTICIPANT_REJECTED",
            payload={"reason": redact(reason)},
            conn=conn,
        )
        self._emit(
            "participant.kyb_rejected",
            participant_id,
            {"participant_id": participant_id, "reason_code": "OPERATOR_REJECTED"},
            conn=conn,
        )
        return record

    # ------------------------------------------------------------------
    # reads (route surface)
    # ------------------------------------------------------------------

    def get_participant(
        self, participant_id: str, *, conn: Any | None = None
    ) -> ParticipantRecord | None:
        return self._store.get_participant(participant_id, conn=conn)

    def get_document(
        self, document_id: str, *, conn: Any | None = None
    ) -> ParticipantDocumentRecord | None:
        return self._store.get_document(document_id, conn=conn)

    def get_conformance_run(
        self, conformance_run_id: str, *, conn: Any | None = None
    ) -> ConformanceRunRecord | None:
        return self._store.get_conformance_run(conformance_run_id, conn=conn)

    def list_conformance_runs(
        self, participant_id: str, *, conn: Any | None = None
    ) -> list[ConformanceRunRecord]:
        return self._store.list_conformance_runs(participant_id, conn=conn)

    def verified_document_classes(
        self, participant_id: str, *, conn: Any | None = None
    ) -> frozenset[str]:
        return frozenset(
            d.document_class
            for d in self._store.list_documents(participant_id, conn=conn)
            if d.review_status == "VERIFIED"
        )

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _require_participant(
        self, participant_id: str, *, conn: Any | None
    ) -> ParticipantRecord:
        record = self._store.get_participant(participant_id, conn=conn)
        if record is None:
            raise NotFoundError(
                f"unknown participant {participant_id}", code="participant_not_found"
            )
        return record

    def _require_document(
        self, participant_id: str, document_id: str, *, conn: Any | None
    ) -> ParticipantDocumentRecord:
        document = self._store.get_document(document_id, conn=conn)
        if document is None or document.participant_id != participant_id:
            raise NotFoundError(f"unknown document {document_id}", code="document_not_found")
        return document

    def _require_run(
        self, conformance_run_id: str, *, conn: Any | None
    ) -> ConformanceRunRecord:
        run = self._store.get_conformance_run(conformance_run_id, conn=conn)
        if run is None:
            raise NotFoundError(
                f"unknown conformance run {conformance_run_id}",
                code="conformance_run_not_found",
            )
        return run

    def _advance(
        self,
        record: ParticipantRecord,
        trigger: str,
        *,
        actor_id: str,
        audit_type: str,
        payload: Mapping[str, object] | None = None,
        conn: Any | None,
        extra_updates: Mapping[str, Any] | None = None,
    ) -> ParticipantRecord:
        """Resolve against the table (refusal-first), persist, audit."""
        rule: TransitionRule = PARTICIPANT_V2_TABLE.resolve(record.kyb_status, trigger)
        now = self._clock.now()
        updates: dict[str, Any] = {"kyb_status": rule.to_state, "updated_at": now}
        if extra_updates:
            updates.update(extra_updates)
        record = replace(record, **updates)
        self._store.update_participant(record, conn=conn)
        self._audit_event(
            audit_type,
            record,
            actor_id=actor_id,
            payload=dict(payload or {}) | {"trigger": trigger},
            from_state=rule.from_state,
            to_state=rule.to_state,
            conn=conn,
        )
        return record

    def _audit_event(
        self,
        event_type: str,
        record: ParticipantRecord,
        *,
        actor_id: str,
        payload: Mapping[str, object],
        from_state: str | None = None,
        to_state: str | None = None,
        conn: Any | None,
    ) -> None:
        self._audit.append(
            AuditEventSpec(
                event_type=event_type,
                actor_id=actor_id,
                subject_type="Participant",
                subject_id=record.participant_id,
                from_state=from_state,
                to_state=to_state,
                payload=dict(payload),
            ),
            clock=self._clock,
            conn=conn,
        )

    def _run_audit(
        self,
        run: ConformanceRunRecord,
        event_type: str,
        *,
        actor_id: str,
        payload: Mapping[str, object],
        from_state: str | None = None,
        to_state: str | None = None,
        conn: Any | None,
    ) -> None:
        self._audit.append(
            AuditEventSpec(
                event_type=event_type,
                actor_id=actor_id,
                subject_type="ConformanceRun",
                subject_id=run.conformance_run_id,
                from_state=from_state,
                to_state=to_state,
                payload=dict(payload),
            ),
            clock=self._clock,
            conn=conn,
        )

    def _emit(
        self,
        event_type: str,
        participant_id: str,
        payload: dict[str, object],
        *,
        conn: Any | None,
    ) -> None:
        event = build_event(
            event_type=event_type,
            subject_type="Participant",
            subject_id=participant_id,
            producer=PRODUCER,
            topic="kyc.events",
            payload=payload,
            occurred_at=self._clock.now(),
        )
        self._outbox.enqueue(event, conn=conn)

    def _emit_run_completed(self, run: ConformanceRunRecord, *, conn: Any | None) -> None:
        event = build_event(
            event_type="conformance_run.completed",
            subject_type="ConformanceRun",
            subject_id=run.conformance_run_id,
            producer=PRODUCER,
            topic="kyc.events",
            payload={
                "conformance_run_id": run.conformance_run_id,
                "participant_id": run.participant_id,
                "direction": run.direction,
                "status": run.status,
                "report_hash": run.report_hash,
            },
            occurred_at=self._clock.now(),
        )
        self._outbox.enqueue(event, conn=conn)

"""StrReport entity, repository, and the suspicion-based STR workflow (spec/06).

BINDING CUT-LAST RULE #1: STR creation is suspicion-based at ANY amount —
nothing in this module compares an amount against a threshold to gate an STR.
The BDT 500k CDD gate and BDT 1M wire gate are pre-auth blocking rules owned
by the kernel's LimitEnforcer; conflating them with STR triggering is the
criminal-liability design bug forbidden by ARCHITECTURE-DECISION Part I (C).

BINDING CUT-LAST RULE #2: an STR is NEVER silently discarded. The filing
retry loop runs with exponential backoff (initial 30s, max 4h) until
``MAX_STR_RETRY``; exhaustion sets ``bfiu_manual_escalation_required`` and
preserves the goAML payload pointer for manual out-of-band filing.

Filing clock: ``filing_due_at = camlco_approved_at + STR_FILING_CLOCK_HOURS``
(24h, thresholds artifact) tracks the time-to-file from the CAMLCO decision.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any, Protocol, runtime_checkable

from bdpay.compliance.goaml import build_str_payload, validate_str_payload
from bdpay.compliance.thresholds import ComplianceThresholds
from bdpay.platform.canonical import sha256_canonical
from bdpay.platform.clock import Clock
from bdpay.platform.errors import (
    AuthorizationError,
    ConflictError,
    InvalidRequestError,
)
from bdpay.platform.ids import make_id
from bdpay.platform.interfaces import AuditEventSpec, AuditPort, OutboxPort
from bdpay.platform.outbox import build_event

__all__ = [
    "InMemoryStrStore",
    "StrReport",
    "StrStore",
    "StrWorkflow",
]

PRODUCER = "regulatory-reporting@1.0.0"

_STATES = (
    "PENDING_CAMLCO_REVIEW",
    "CAMLCO_APPROVED",
    "FILING_PENDING",
    "FILING_SUBMITTED",
    "FILING_CONFIRMED",
    "FILING_FAILED",
    "WITHDRAWN",
)
_TERMINAL = ("FILING_CONFIRMED", "WITHDRAWN")


@dataclass(frozen=True)
class StrReport:
    """One str_reports row (migration 0072)."""

    str_id: str
    alert_id: str
    subject_type: str
    subject_id: str
    status: str
    camlco_id: str
    suspicion_narrative_hash: str
    suspicion_narrative_pointer: str
    created_at: datetime
    camlco_approved_at: datetime | None = None
    filing_due_at: datetime | None = None  # 24h filing clock from the decision
    requires_bfiu_escalation: bool = False
    bfiu_manual_escalation_required: bool = False
    goaml_payload_hash: str | None = None
    goaml_payload_pointer: str | None = None
    goaml_ref: str | None = None
    filing_submitted_at: datetime | None = None
    filing_confirmed_at: datetime | None = None
    filing_error_detail_hash: str | None = None
    retry_count: int = 0
    retry_scheduled_at: datetime | None = None
    withdrawn_at: datetime | None = None
    withdrawal_reason: str | None = None
    contributing_payment_ids: tuple[str, ...] = ()
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.status not in _STATES:
            raise ValueError(f"status must be one of {_STATES}, got {self.status!r}")

    @property
    def is_terminal(self) -> bool:
        return self.status in _TERMINAL

    def filing_overdue(self, now: datetime) -> bool:
        """True when the 24h filing clock has elapsed without confirmation.

        A FILING_SUBMITTED STR whose 24h filing clock has elapsed is overdue:
        the goAML ack has not arrived in time, so an alarm must raise instead
        of the STR going invisible (cut-last: never silently drop a regulatory
        filing).
        """
        if self.filing_due_at is None or self.status in _TERMINAL:
            return False
        return now > self.filing_due_at


@runtime_checkable
class StrStore(Protocol):
    """Storage contract for str_reports."""

    def insert(self, report: StrReport) -> None: ...

    def save(self, report: StrReport) -> None: ...

    def get(self, str_id: str) -> StrReport | None: ...

    def by_status(self, status: str) -> list[StrReport]: ...

    def by_alert(self, alert_id: str) -> list[StrReport]: ...


@runtime_checkable
class StrObjectStore(Protocol):
    """Object-store port for STR narrative payloads."""

    def put(self, key: str, data: bytes) -> object: ...

    def get(self, key: str) -> bytes | None: ...


class InMemoryStrObjectStore:
    """Deterministic object store for STR workflow unit tests."""

    def __init__(self) -> None:
        self._objects: dict[str, bytes] = {}

    def put(self, key: str, data: bytes) -> str:
        if not key:
            raise ValueError("object key must be non-empty")
        if not isinstance(data, bytes):
            raise TypeError(f"object store data must be bytes, got {type(data).__name__}")
        self._objects[key] = bytes(data)
        return key

    def get(self, key: str) -> bytes | None:
        return self._objects.get(key)


class InMemoryStrStore:
    """Deterministic in-memory STR store for unit tests."""

    def __init__(self) -> None:
        self._rows: dict[str, StrReport] = {}
        self._order: list[str] = []

    def insert(self, report: StrReport) -> None:
        if report.str_id in self._rows:
            raise ConflictError(f"str report {report.str_id} already exists")
        self._rows[report.str_id] = report
        self._order.append(report.str_id)

    def save(self, report: StrReport) -> None:
        if report.str_id not in self._rows:
            raise ConflictError(f"unknown str report {report.str_id}")
        self._rows[report.str_id] = report

    def get(self, str_id: str) -> StrReport | None:
        return self._rows.get(str_id)

    def by_status(self, status: str) -> list[StrReport]:
        return [self._rows[s] for s in self._order if self._rows[s].status == status]

    def by_alert(self, alert_id: str) -> list[StrReport]:
        return [self._rows[s] for s in self._order if self._rows[s].alert_id == alert_id]


class StrWorkflow:
    """STR lifecycle engine: create -> CAMLCO decision -> file -> confirm.

    The goAML wire submission belongs to the ``goaml_reporter_v1`` connector
    (spec/12); this workflow calls it through the ``AmlFilingConnector``
    sub-protocol (spec/00 §10) and owns every state around that call.
    """

    def __init__(
        self,
        store: StrStore,
        *,
        clock: Clock,
        audit: AuditPort,
        outbox: OutboxPort,
        thresholds: ComplianceThresholds,
        object_store_prefix: str = "objstore://str/",
        object_store: StrObjectStore | None = None,
    ) -> None:
        self._store = store
        self._clock = clock
        self._audit = audit
        self._outbox = outbox
        self._thresholds = thresholds
        self._objstore_prefix = object_store_prefix
        self._object_store = (
            object_store if object_store is not None else InMemoryStrObjectStore()
        )

    # -- creation (suspicion-based, ANY amount) -----------------------------------

    def create_from_alert(
        self,
        *,
        alert_id: str,
        subject_type: str,
        subject_id: str,
        camlco_id: str,
        suspicion_narrative: str,
        contributing_payment_ids: tuple[str, ...] = (),
        requires_bfiu_escalation: bool = False,
        conn: Any | None = None,
    ) -> StrReport:
        """Create an StrReport in PENDING_CAMLCO_REVIEW.

        Suspicion is the ONLY trigger. This method takes no amount parameter
        at all, so no code path can gate STR creation on an amount.
        """
        if not suspicion_narrative:
            raise InvalidRequestError(
                "an STR requires a suspicion narrative", code="narrative_required"
            )
        now = self._clock.now()
        narrative_hash = sha256_canonical({"narrative": suspicion_narrative})
        str_id = make_id(
            "str",
            {
                "alert_id": alert_id,
                "camlco_id": camlco_id,
                "suspicion_narrative_hash": narrative_hash,
                "created_at": now,
            },
        )
        narrative_pointer = f"{self._objstore_prefix}{str_id}/narrative"
        self._object_store.put(narrative_pointer, suspicion_narrative.encode("utf-8"))
        report = StrReport(
            str_id=str_id,
            alert_id=alert_id,
            subject_type=subject_type,
            subject_id=subject_id,
            status="PENDING_CAMLCO_REVIEW",
            camlco_id=camlco_id,
            suspicion_narrative_hash=narrative_hash,
            suspicion_narrative_pointer=narrative_pointer,
            created_at=now,
            requires_bfiu_escalation=requires_bfiu_escalation,
            contributing_payment_ids=contributing_payment_ids,
        )
        self._store.insert(report)
        self._audit.append(
            AuditEventSpec(
                event_type="STR_REPORT_CREATED",
                actor_id=camlco_id,
                subject_type="StrReport",
                subject_id=str_id,
                from_state=None,
                to_state="PENDING_CAMLCO_REVIEW",
                payload={"alert_id": alert_id},
            ),
            clock=self._clock,
            conn=conn,
        )
        return report

    # -- FSM helpers ------------------------------------------------------------

    def _require(self, str_id: str) -> StrReport:
        report = self._store.get(str_id)
        if report is None:
            raise InvalidRequestError(f"unknown STR report {str_id}", code="str_not_found")
        return report

    @staticmethod
    def _deny_unless(report: StrReport, expected: tuple[str, ...], trigger: str) -> None:
        if report.status not in expected:
            raise ConflictError(
                f"transition {trigger!r} from state {report.status!r} is DENIED "
                "(refusal-first)",
                code="fsm_transition_denied",
            )

    def _effects(
        self,
        report: StrReport,
        *,
        event_type: str,
        actor_id: str,
        from_state: str,
        payload: dict,
        conn: Any | None,
    ) -> None:
        self._audit.append(
            AuditEventSpec(
                event_type="STR_REPORT_STATE_TRANSITION",
                actor_id=actor_id,
                subject_type="StrReport",
                subject_id=report.str_id,
                from_state=from_state,
                to_state=report.status,
                payload=payload,
            ),
            clock=self._clock,
            conn=conn,
        )
        self._outbox.enqueue(
            build_event(
                event_type=event_type,
                subject_type="StrReport",
                subject_id=report.str_id,
                producer=PRODUCER,
                topic="aml.events",
                payload={"str_id": report.str_id, **payload},
                occurred_at=self._clock.now(),
            ),
            conn=conn,
        )

    # -- CAMLCO decision ----------------------------------------------------------

    def camlco_approve(
        self,
        str_id: str,
        *,
        actor_id: str,
        actor_role: str,
        camlco_declaration: bool,
        conn: Any | None = None,
    ) -> StrReport:
        """PENDING_CAMLCO_REVIEW -> FILING_PENDING (via CAMLCO_APPROVED).

        AML-H1 fix: camlco_approve() now chains directly into queue_filing() in
        the same transaction so a CAMLCO decision always advances the STR to
        FILING_PENDING without a separate manual call.  The intermediate
        CAMLCO_APPROVED state still exists and its event is still emitted (audit
        trail), but the STR does not rest there — it immediately proceeds to
        FILING_PENDING so the outbox worker can pick it up.  Callers that
        previously called queue_filing() after approve() will receive a
        ConflictError (already FILING_PENDING); use the return value of
        camlco_approve() directly instead.
        """
        report = self._require(str_id)
        self._deny_unless(report, ("PENDING_CAMLCO_REVIEW",), "camlco_approved")
        if actor_role != "CAMLCO":
            raise AuthorizationError("only the CAMLCO may approve an STR for filing")
        if camlco_declaration is not True:
            raise InvalidRequestError(
                "camlco_declaration must be true", code="camlco_declaration_required"
            )
        now = self._clock.now()
        approved = replace(
            report,
            status="CAMLCO_APPROVED",
            camlco_approved_at=now,
            filing_due_at=now + timedelta(hours=self._thresholds.str_filing_clock_hours),
        )
        self._store.save(approved)
        self._effects(
            approved,
            event_type="str_report.camlco_approved",
            actor_id=actor_id,
            from_state="PENDING_CAMLCO_REVIEW",
            payload={},
            conn=conn,
        )
        # AML-H1: advance immediately to FILING_PENDING so the outbox worker picks
        # up without a separate queue_filing() call. Before this chaining, every
        # approved STR sat permanently in CAMLCO_APPROVED and never reached
        # BFIU/goAML filing. Thread the same conn for atomicity.
        return self.queue_filing(str_id, conn=conn)

    def withdraw(
        self,
        str_id: str,
        *,
        actor_id: str,
        actor_role: str,
        withdrawal_reason: str,
        conn: Any | None = None,
    ) -> StrReport:
        """PENDING_CAMLCO_REVIEW -> WITHDRAWN (terminal); reason is mandatory."""
        report = self._require(str_id)
        self._deny_unless(report, ("PENDING_CAMLCO_REVIEW",), "withdrawn")
        if actor_role != "CAMLCO":
            raise AuthorizationError("only the CAMLCO may withdraw an STR")
        if not withdrawal_reason:
            raise InvalidRequestError(
                "a withdrawal requires a reason (mandatory)", code="withdrawal_reason_required"
            )
        now = self._clock.now()
        updated = replace(
            report,
            status="WITHDRAWN",
            withdrawn_at=now,
            withdrawal_reason=withdrawal_reason,
        )
        self._store.save(updated)
        self._effects(
            updated,
            event_type="str_report.withdrawn",
            actor_id=actor_id,
            from_state="PENDING_CAMLCO_REVIEW",
            payload={},
            conn=conn,
        )
        return updated

    # -- filing pipeline -----------------------------------------------------------

    def queue_filing(self, str_id: str, *, conn: Any | None = None) -> StrReport:
        """CAMLCO_APPROVED -> FILING_PENDING (outbox worker pickup)."""
        report = self._require(str_id)
        self._deny_unless(report, ("CAMLCO_APPROVED",), "filing_queued")
        payload = self.build_filing_payload(report)
        updated = replace(
            report,
            status="FILING_PENDING",
            goaml_payload_hash=sha256_canonical(payload),
            goaml_payload_pointer=f"{self._objstore_prefix}{report.str_id}/goaml_payload",
        )
        self._store.save(updated)
        self._effects(
            updated,
            event_type="str_report.filing_pending",
            actor_id=PRODUCER,
            from_state="CAMLCO_APPROVED",
            payload={},
            conn=conn,
        )
        return updated

    def build_filing_payload(self, report: StrReport) -> dict:
        """The validated goAML STR payload (spec/06 Connector contract shape)."""
        narrative = self._resolved_narrative(report)
        payload = build_str_payload(
            str_id=report.str_id,
            camlco_id=report.camlco_id,
            subject_type=report.subject_type,
            subject_id=report.subject_id,
            transaction_ids=list(report.contributing_payment_ids),
            suspicion_narrative_pointer=report.suspicion_narrative_pointer,
            goaml_payload_pointer=(
                report.goaml_payload_pointer
                or f"{self._objstore_prefix}{report.str_id}/goaml_payload"
            ),
            raised_at=report.created_at,
            filing_at=self._clock.now(),
            suspicion_narrative=narrative,
        )
        validate_str_payload(payload)
        return payload

    def _resolved_narrative(self, report: StrReport) -> str:
        try:
            raw = self._object_store.get(report.suspicion_narrative_pointer)
        except KeyError as exc:
            raise InvalidRequestError(
                "STR suspicion narrative object is missing",
                code="str_narrative_missing",
            ) from exc
        if raw is None:
            raise InvalidRequestError(
                "STR suspicion narrative object is missing",
                code="str_narrative_missing",
            )
        try:
            narrative = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise InvalidRequestError(
                "STR suspicion narrative object is not valid UTF-8",
                code="str_narrative_invalid",
            ) from exc
        if sha256_canonical({"narrative": narrative}) != report.suspicion_narrative_hash:
            raise InvalidRequestError(
                "STR suspicion narrative hash mismatch",
                code="str_narrative_hash_mismatch",
            )
        if not narrative.strip():
            raise InvalidRequestError(
                "STR suspicion narrative object is empty",
                code="str_narrative_missing",
            )
        return narrative

    async def attempt_filing(
        self,
        str_id: str,
        *,
        connector: Any,  # AmlFilingConnector (spec/00 §10); duck-typed
        conn: Any | None = None,
    ) -> StrReport:
        """FILING_PENDING -> FILING_SUBMITTED | FILING_FAILED. Never drops."""
        report = self._require(str_id)
        self._deny_unless(report, ("FILING_PENDING",), "filing_submitted")
        payload = self.build_filing_payload(report)
        now = self._clock.now()
        try:
            goaml_ref = await connector.file_str(payload)
        except Exception as exc:  # noqa: BLE001 - any connector failure is a retry
            error_hash = sha256_canonical({"error": f"{type(exc).__name__}: {exc}"})
            retry_count = report.retry_count + 1
            delay = min(
                self._thresholds.str_retry_initial_delay_seconds * (2 ** (retry_count - 1)),
                self._thresholds.str_retry_max_delay_seconds,
            )
            exhausted = retry_count >= self._thresholds.max_str_retry
            updated = replace(
                report,
                status="FILING_FAILED",
                filing_error_detail_hash=error_hash,
                retry_count=retry_count,
                retry_scheduled_at=None if exhausted else now + timedelta(seconds=delay),
                bfiu_manual_escalation_required=exhausted
                or report.bfiu_manual_escalation_required,
            )
            self._store.save(updated)
            self._effects(
                updated,
                event_type="str_report.filing_failed",
                actor_id=PRODUCER,
                from_state="FILING_PENDING",
                payload={"retry_count": retry_count, "error_code": "connector_error"},
                conn=conn,
            )
            if exhausted:
                self._effects(
                    updated,
                    event_type="str_report.retry_exhausted",
                    actor_id=PRODUCER,
                    from_state="FILING_FAILED",
                    payload={"retry_count": retry_count},
                    conn=conn,
                )
            return updated
        updated = replace(
            report,
            status="FILING_SUBMITTED",
            goaml_ref=goaml_ref,
            filing_submitted_at=now,
        )
        self._store.save(updated)
        self._effects(
            updated,
            event_type="str_report.filed",
            actor_id=PRODUCER,
            from_state="FILING_PENDING",
            payload={"goaml_ref": goaml_ref},
            conn=conn,
        )
        return updated

    def retry_filing(
        self, str_id: str, *, actor_id: str, conn: Any | None = None
    ) -> StrReport:
        """FILING_FAILED -> FILING_PENDING while retry budget remains."""
        report = self._require(str_id)
        self._deny_unless(report, ("FILING_FAILED",), "retry_filing")
        if report.retry_count >= self._thresholds.max_str_retry:
            raise ConflictError(
                "retry budget exhausted; bfiu_manual_escalation_required is set and "
                "the payload is preserved for manual out-of-band filing",
                code="str_retry_exhausted",
            )
        updated = replace(report, status="FILING_PENDING", retry_scheduled_at=None)
        self._store.save(updated)
        self._effects(
            updated,
            event_type="str_report.filing_pending",
            actor_id=actor_id,
            from_state="FILING_FAILED",
            payload={"retry_count": report.retry_count},
            conn=conn,
        )
        return updated

    def confirm_filing(
        self, str_id: str, *, goaml_ref: str, conn: Any | None = None
    ) -> StrReport:
        """FILING_SUBMITTED -> FILING_CONFIRMED (goAML callback / poll)."""
        report = self._require(str_id)
        self._deny_unless(report, ("FILING_SUBMITTED",), "filing_confirmed")
        if report.goaml_ref is not None and goaml_ref != report.goaml_ref:
            raise ConflictError(
                "confirmation reference does not match the submitted filing",
                code="goaml_ref_mismatch",
            )
        now = self._clock.now()
        updated = replace(report, status="FILING_CONFIRMED", filing_confirmed_at=now)
        self._store.save(updated)
        self._effects(
            updated,
            event_type="str_report.confirmed",
            actor_id=PRODUCER,
            from_state="FILING_SUBMITTED",
            payload={"goaml_ref": goaml_ref},
            conn=conn,
        )
        return updated

    # -- review-queue visibility ------------------------------------------------

    def pending_camlco_review(self) -> list[StrReport]:
        """The CAMLCO review queue (spec/06 GET str-reports FILING view)."""
        return self._store.by_status("PENDING_CAMLCO_REVIEW")

    def review_overdue(self, *, now: datetime) -> list[StrReport]:
        """Reports stuck in PENDING_CAMLCO_REVIEW beyond the 2h escalation TTL."""
        limit = timedelta(hours=self._thresholds.str_camlco_review_hours)
        return [
            r for r in self._store.by_status("PENDING_CAMLCO_REVIEW") if now - r.created_at > limit
        ]

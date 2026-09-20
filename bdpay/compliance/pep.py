"""PEP screening + PepRecord policy (spec/07 PEP policy, normative).

The screener is the SAME ported matcher as sanctions (pep-matcher.ts reuses
matcher.ts), run against a PEP snapshot. Invariants carried verbatim:

- A PEP hit is NEVER auto-cleared: any match requires human EDD; "cleared"
  means no snapshot match, NOT confirmed non-PEP status.
- Empty PEP list refuses to screen (anti-false-cleared guard).
- PEP is NOT a sanctions hit: no freeze, no decline — it is an EDD/approval
  gate. An ACTIVE pep_record forces the subject risk tier to HIGH (+40
  score), requires CAMLCO approval before activation, and carries an annual
  review date.
- Closing a PEP flag is four-eyes (spec/07 PEP policy item 5, same pattern as
  hit clearance): this module requires an APPROVED ``ApprovalRequest`` scoped
  to the pep_id before the ACTIVE -> CLOSED transition executes.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable

from bdpay.compliance.ids_ext import make_compliance_id
from bdpay.compliance.sanctions.matcher import (
    ScreenedSubject,
    WatchlistEntry,
    WatchlistScreenReport,
    screen_subjects,
)
from bdpay.platform.clock import Clock
from bdpay.platform.errors import (
    AuthorizationError,
    ConflictError,
    InvalidRequestError,
)
from bdpay.platform.interfaces import AuditEventSpec, AuditPort, OutboxPort
from bdpay.platform.outbox import build_event

__all__ = [
    "PEP_RISK_DELTA",
    "InMemoryPepStore",
    "PepRecord",
    "PepScreener",
    "PepStore",
]

PRODUCER = "sanctions-screener@1.0.0"

#: spec/07 PEP policy item 3: ACTIVE pep_record adds +40 and forces HIGH.
PEP_RISK_DELTA = 40

_CATEGORIES = ("DOMESTIC_PEP", "FOREIGN_PEP", "INTERNATIONAL_ORG_PEP", "RCA")
_SOURCES = ("SELF_DECLARED", "SCREENING_VENDOR", "MANUAL_RESEARCH", "BFIU_NOTICE")
_SUBJECT_TYPES = ("Customer", "Merchant", "UBO", "Participant")


@dataclass(frozen=True)
class PepRecord:
    """One pep_records row (migration 0077)."""

    pep_id: str
    subject_type: str
    subject_id: str
    pep_category: str
    position_held: str
    source: str
    status: str  # ACTIVE | CLOSED
    next_review_at: datetime
    created_at: datetime
    edd_completed_at: datetime | None = None
    camlco_approved_by: str | None = None
    camlco_approved_at: datetime | None = None
    closed_at: datetime | None = None
    close_approval_request_id: str | None = None
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.pep_category not in _CATEGORIES:
            raise ValueError(f"pep_category must be one of {_CATEGORIES}")
        if self.source not in _SOURCES:
            raise ValueError(f"source must be one of {_SOURCES}")
        if self.subject_type not in _SUBJECT_TYPES:
            raise ValueError(f"subject_type must be one of {_SUBJECT_TYPES}")
        if self.status not in ("ACTIVE", "CLOSED"):
            raise ValueError("status must be ACTIVE or CLOSED")


@runtime_checkable
class PepStore(Protocol):
    """Storage contract for pep_records."""

    def insert(self, record: PepRecord) -> None: ...

    def save(self, record: PepRecord) -> None: ...

    def get(self, pep_id: str) -> PepRecord | None: ...

    def active_for_subject(self, subject_type: str, subject_id: str) -> list[PepRecord]: ...


class InMemoryPepStore:
    """Deterministic in-memory PEP store for unit tests."""

    def __init__(self) -> None:
        self._rows: dict[str, PepRecord] = {}

    def insert(self, record: PepRecord) -> None:
        if record.pep_id in self._rows:
            raise ConflictError(f"pep record {record.pep_id} already exists")
        self._rows[record.pep_id] = record

    def save(self, record: PepRecord) -> None:
        if record.pep_id not in self._rows:
            raise ConflictError(f"unknown pep record {record.pep_id}")
        self._rows[record.pep_id] = record

    def get(self, pep_id: str) -> PepRecord | None:
        return self._rows.get(pep_id)

    def active_for_subject(self, subject_type: str, subject_id: str) -> list[PepRecord]:
        return [
            r
            for r in self._rows.values()
            if r.subject_type == subject_type
            and r.subject_id == subject_id
            and r.status == "ACTIVE"
        ]


class PepScreener:
    """PEP snapshot screening + the PepRecord EDD/approval gate."""

    def __init__(
        self,
        store: PepStore,
        *,
        clock: Clock,
        audit: AuditPort,
        outbox: OutboxPort,
        pep_list: Sequence[WatchlistEntry],
        approvals: Any | None = None,  # ApprovalPort, needed for close
        escalate_threshold: Decimal = Decimal("0.90"),
        review_threshold: Decimal = Decimal("0.80"),
    ) -> None:
        self._store = store
        self._clock = clock
        self._audit = audit
        self._outbox = outbox
        self._pep_list = tuple(pep_list)
        self._approvals = approvals
        self._escalate = escalate_threshold
        self._review = review_threshold

    # -- screening (ported pep-matcher semantics) -----------------------------------

    def screen(self, subjects: Sequence[ScreenedSubject]) -> WatchlistScreenReport:
        """Screen subjects against the PEP snapshot (never auto-clears a hit).

        Raises on an empty PEP list — the safety net preserved from
        ``screenOwnersForPep`` (it delegates to the empty-watchlist guard).
        """
        return screen_subjects(
            list(subjects),
            list(self._pep_list),
            escalate_threshold=self._escalate,
            review_threshold=self._review,
            screened_at=self._clock.now(),
        )

    # -- flag lifecycle ---------------------------------------------------------

    def raise_flag(
        self,
        *,
        subject_type: str,
        subject_id: str,
        pep_category: str,
        position_held: str,
        source: str,
        conn: Any | None = None,
    ) -> PepRecord:
        """Open an ACTIVE pep_record (idempotent per subject+category+source)."""
        pep_id = make_compliance_id(
            "pep",
            {
                "subject_type": subject_type,
                "subject_id": subject_id,
                "pep_category": pep_category,
                "source": source,
            },
        )
        existing = self._store.get(pep_id)
        if existing is not None and existing.status == "ACTIVE":
            return existing
        now = self._clock.now()
        record = PepRecord(
            pep_id=pep_id,
            subject_type=subject_type,
            subject_id=subject_id,
            pep_category=pep_category,
            position_held=position_held,
            source=source,
            status="ACTIVE",
            next_review_at=now + timedelta(days=365),  # PEPs are always HIGH: annual
            created_at=now,
        )
        if existing is None:
            self._store.insert(record)
        else:
            self._store.save(record)
        self._audit.append(
            AuditEventSpec(
                event_type="PEP_FLAG_RAISED",
                actor_id=PRODUCER,
                subject_type="PepRecord",
                subject_id=pep_id,
                to_state="ACTIVE",
                payload={"pep_category": pep_category, "source": source},
            ),
            clock=self._clock,
            conn=conn,
        )
        self._outbox.enqueue(
            build_event(
                event_type="pep_flag.raised",
                subject_type="PepRecord",
                subject_id=pep_id,
                producer=PRODUCER,
                topic="aml.events",
                payload={"pep_id": pep_id, "subject_id": subject_id},
                occurred_at=now,
            ),
            conn=conn,
        )
        return record

    def complete_edd(
        self, pep_id: str, *, camlco_id: str, conn: Any | None = None
    ) -> PepRecord:
        """Record EDD completion + the mandatory CAMLCO relationship approval.

        Until this is set, spec/08/09 FSMs may not enter ACTIVE for the
        subject (``camlco_approved_at`` gate, PEP policy item 3).
        """
        record = self._store.get(pep_id)
        if record is None:
            raise InvalidRequestError(f"unknown pep record {pep_id}", code="pep_not_found")
        if record.status != "ACTIVE":
            raise ConflictError("EDD applies to ACTIVE pep records only", code="pep_not_active")
        now = self._clock.now()
        updated = replace(
            record,
            edd_completed_at=now,
            camlco_approved_by=camlco_id,
            camlco_approved_at=now,
        )
        self._store.save(updated)
        self._audit.append(
            AuditEventSpec(
                event_type="PEP_EDD_COMPLETED",
                actor_id=camlco_id,
                subject_type="PepRecord",
                subject_id=pep_id,
                payload={},
            ),
            clock=self._clock,
            conn=conn,
        )
        return updated

    def close_flag(
        self,
        pep_id: str,
        *,
        actor_id: str,
        actor_role: str,
        approval_request_id: str,
        conn: Any | None = None,
    ) -> PepRecord:
        """ACTIVE -> CLOSED, four-eyes only (PEP policy item 5)."""
        record = self._store.get(pep_id)
        if record is None:
            raise InvalidRequestError(f"unknown pep record {pep_id}", code="pep_not_found")
        if record.status != "ACTIVE":
            raise ConflictError("only an ACTIVE pep record can close", code="pep_not_active")
        if actor_role not in ("COMPLIANCE_ANALYST", "CAMLCO"):
            raise AuthorizationError("PEP close requires a compliance role")
        if self._approvals is None:
            raise ConflictError(
                "PEP close requires the ApprovalPort to be wired (four-eyes)",
                code="approval_port_missing",
            )
        approval = self._approvals.get(approval_request_id)
        if approval is None or approval.state != "APPROVED":
            raise ConflictError(
                "PEP close requires an APPROVED four-eyes ApprovalRequest",
                code="approval_not_granted",
            )
        if approval.subject_id != pep_id:
            raise ConflictError(
                "approval request does not match this pep record", code="approval_mismatch"
            )
        now = self._clock.now()
        updated = replace(
            record,
            status="CLOSED",
            closed_at=now,
            close_approval_request_id=approval_request_id,
        )
        self._store.save(updated)
        self._audit.append(
            AuditEventSpec(
                event_type="PEP_FLAG_CLOSED",
                actor_id=actor_id,
                subject_type="PepRecord",
                subject_id=pep_id,
                from_state="ACTIVE",
                to_state="CLOSED",
                payload={"approval_request_id": approval_request_id},
            ),
            clock=self._clock,
            conn=conn,
        )
        self._outbox.enqueue(
            build_event(
                event_type="pep_flag.closed",
                subject_type="PepRecord",
                subject_id=pep_id,
                producer=PRODUCER,
                topic="aml.events",
                payload={"pep_id": pep_id, "subject_id": record.subject_id},
                occurred_at=now,
            ),
            conn=conn,
        )
        return updated

    # -- risk surface ------------------------------------------------------------

    def get_risk_delta(self, subject_type: str, subject_id: str) -> int:
        """+40 (forced-HIGH factor) while any ACTIVE pep_record exists."""
        if self._store.active_for_subject(subject_type, subject_id):
            return PEP_RISK_DELTA
        return 0

    def is_active_pep(self, subject_type: str, subject_id: str) -> bool:
        return bool(self._store.active_for_subject(subject_type, subject_id))

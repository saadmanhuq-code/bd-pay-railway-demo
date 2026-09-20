"""spec/19 PSO-1 entity records (in-process shapes of the 0041/0100/0101 rows)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from bdpay.kernel.participants.states import (
    CONFORMANCE_DIRECTIONS,
    CONFORMANCE_RUN_TABLE,
    DOCUMENT_CLASSES,
    DOCUMENT_REVIEW_STATUSES,
    PARTICIPANT_V2_TABLE,
)
from bdpay.platform.errors import InvalidRequestError

__all__ = [
    "ConformanceRunRecord",
    "INSTITUTION_TYPES",
    "ParticipantDocumentRecord",
    "ParticipantMouRecord",
    "ParticipantRecord",
]

#: 0041_participants.sql closed set (spec/08; institutions only — PSO posture).
INSTITUTION_TYPES: tuple[str, ...] = (
    "SCHEDULED_BANK",
    "NBFI",
    "LICENSED_PSO",
    "LICENSED_PSP",
    "MFS_OPERATOR",
)


@dataclass(frozen=True)
class ParticipantRecord:
    """One ``participants`` row (0041 + the 0100 spec/19 columns)."""

    participant_id: str
    institution_name: str
    institution_type: str
    created_at: datetime
    updated_at: datetime
    kyb_status: str = "APPLICATION_SUBMITTED"
    bb_license_number: str | None = None
    bb_license_type: str | None = None
    bb_license_expiry: date | None = None
    net_debit_cap_id: str | None = None
    activated_at: datetime | None = None
    # -- spec/19 PSO-1 columns (migration 0100) ----------------------------
    mou_id: str | None = None
    conformance_passed_at: datetime | None = None
    suspended_reason: str | None = None
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.kyb_status not in PARTICIPANT_V2_TABLE.states:
            raise InvalidRequestError(f"unknown kyb_status {self.kyb_status!r}")
        if self.institution_type not in INSTITUTION_TYPES:
            raise InvalidRequestError(
                f"institution_type must be one of {INSTITUTION_TYPES}, "
                f"got {self.institution_type!r}"
            )
        if not self.institution_name:
            raise InvalidRequestError("institution_name must be non-empty")


@dataclass(frozen=True)
class ParticipantDocumentRecord:
    """One ``participant_documents`` row (migration 0100)."""

    document_id: str
    participant_id: str
    document_class: str
    storage_pointer: str
    content_sha256: str
    uploaded_by: str
    uploaded_at: datetime
    metadata: dict[str, Any] = field(default_factory=dict)
    review_status: str = "PENDING"
    reject_reason: str | None = None
    verified_by: str | None = None
    verified_at: datetime | None = None
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.document_class not in DOCUMENT_CLASSES:
            raise InvalidRequestError(
                f"document_class must be one of {DOCUMENT_CLASSES}, "
                f"got {self.document_class!r}"
            )
        if self.review_status not in DOCUMENT_REVIEW_STATUSES:
            raise InvalidRequestError(f"unknown review_status {self.review_status!r}")
        if self.verified_by is not None and self.verified_by == self.uploaded_by:
            # Mirrors the pdoc_verifier_differs DB CHECK (maker-checker).
            raise InvalidRequestError(
                "verified_by must differ from uploaded_by", code="verifier_must_differ"
            )
        if self.review_status == "VERIFIED" and (
            self.verified_by is None or self.verified_at is None
        ):
            raise InvalidRequestError(
                "a VERIFIED document must carry verified_by and verified_at"
            )
        for name in ("storage_pointer", "content_sha256", "uploaded_by"):
            if not getattr(self, name):
                raise InvalidRequestError(f"{name} must be non-empty")


@dataclass(frozen=True)
class ParticipantMouRecord:
    """One ``participant_mous`` row — the founding instrument (migration 0100).

    The three founding flags are structurally immutable ``TRUE`` in v1
    (spec/19 data model): the MOU IS non-binding, contingent-on-license and
    zero-capital by definition. They are plain attributes here so the row
    shape matches the DDL, and construction refuses any other value.
    """

    mou_id: str
    participant_id: str
    mou_template_version: str
    executed_date: date
    signed_document_pointer: str
    signed_document_sha256: str
    counterparty_signatory: dict[str, str]
    recorded_by: str
    recorded_at: datetime
    non_binding: bool = True
    contingent_on_license: bool = True
    zero_capital_commitment: bool = True
    founding_fee_terms_ref: str | None = None  # NULL until the principal sets terms
    schema_version: int = 1

    def __post_init__(self) -> None:
        for flag in ("non_binding", "contingent_on_license", "zero_capital_commitment"):
            if getattr(self, flag) is not True:
                raise InvalidRequestError(
                    f"{flag} is structurally TRUE in v1 (spec/19 data model); "
                    "a binding instrument is a different document class"
                )
        for name in (
            "mou_template_version",
            "signed_document_pointer",
            "signed_document_sha256",
            "recorded_by",
        ):
            if not getattr(self, name):
                raise InvalidRequestError(f"{name} must be non-empty")
        signatory = self.counterparty_signatory
        if not isinstance(signatory, dict) or set(signatory) != {"name", "title"}:
            raise InvalidRequestError(
                "counterparty_signatory must be an object with exactly "
                "{name, title}"
            )


@dataclass(frozen=True)
class ConformanceRunRecord:
    """One ``conformance_runs`` row (migration 0101)."""

    conformance_run_id: str
    participant_id: str
    direction: str
    suite_version: str
    triggered_by: str
    created_at: datetime
    status: str = "QUEUED"
    checks: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    evidence_pointer: str | None = None
    evidence_sha256: str | None = None
    report_pointer: str | None = None
    report_hash: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.direction not in CONFORMANCE_DIRECTIONS:
            raise InvalidRequestError(
                f"direction must be one of {CONFORMANCE_DIRECTIONS}, got {self.direction!r}"
            )
        if self.status not in CONFORMANCE_RUN_TABLE.states:
            raise InvalidRequestError(f"unknown conformance run status {self.status!r}")
        for name in ("suite_version", "triggered_by"):
            if not getattr(self, name):
                raise InvalidRequestError(f"{name} must be non-empty")

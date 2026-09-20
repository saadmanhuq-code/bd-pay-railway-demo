"""CustomerEkycService — the spec/09 kyc-service.

Drives the KycRecord FSM (T1-T26): session intake with the Bengali-locale
NID pipeline (ported from identity-core, see :mod:`bdpay.kernel.kyc.nid`),
biometric attempts against the identity provider (binary matched verdict,
three-attempt cap), tier assignment with sanctions screening, the
``PENDING_HUMAN_REVIEW`` fallback whenever the identity provider is
unavailable (NEVER a hard block — spec/09 cut-last #4), refresh/expiry
scheduling per risk tier (HIGH 1yr / MEDIUM 2yr / LOW 5yr), and the
two-eyes manual-review resolution through
:class:`~bdpay.platform.interfaces.ApprovalPort`.

PII posture: the raw NID exists transiently inside intake/verification
calls only; at rest it is the one-way ``nid_hash`` plus ``nid_encrypted``
(AES-256-GCM via :class:`AesGcmNidCipher`). It never reaches a log, an
audit payload, or an event payload.
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable

from bdpay.kernel.dates import add_years, age_years
from bdpay.kernel.ids_ext import make_kernel_id
from bdpay.kernel.kyc.kyc_models import (
    BiometricAttemptRecord,
    KycManualReviewRecord,
    KycRecordModel,
)
from bdpay.kernel.kyc.kyc_repository import KycStore
from bdpay.kernel.kyc.kyc_states import (
    KYC_MANUAL_REVIEW_TABLE,
    KYC_TABLE,
    RISK_TIER_REFRESH_YEARS,
)
from bdpay.kernel.kyc.nid import dob_cross_check, normalize_nid_number
from bdpay.kernel.kyc.ocr_intake import (
    OCR_UNAVAILABLE_REASON,
    DocumentOcrPort,
    derive_nid_submission,
)
from bdpay.kernel.kyc.review_queue import PorichoyReviewQueueGuard
from bdpay.platform.bangla import normalize_bengali_digits
from bdpay.platform.canonical import sha256_canonical
from bdpay.platform.clock import Clock
from bdpay.platform.errors import (
    ConflictError,
    InvalidRequestError,
    LimitExceededError,
    NotFoundError,
)
from bdpay.platform.ids import make_id
from bdpay.platform.interfaces import (
    ApprovalPort,
    AuditEventSpec,
    AuditPort,
    OutboxPort,
    SanctionsPort,
)
from bdpay.platform.outbox import build_event

__all__ = [
    "AesGcmNidCipher",
    "CustomerEkycService",
    "DocumentOcrPort",
    "IdentityVerificationPort",
    "MAX_BIOMETRIC_ATTEMPTS",
    "NidCipherPort",
    "NidOcrSubmission",
    "OCR_CONFIDENCE_THRESHOLD",
]

PRODUCER = "kyc-service@1"

MAX_BIOMETRIC_ATTEMPTS = 3
OCR_CONFIDENCE_THRESHOLD = Decimal("0.70")
SESSION_TTL = timedelta(hours=48)
BIOMETRIC_RETRY_TTL = timedelta(hours=24)
REFRESH_OVERDUE_GRACE = timedelta(days=30)
HARD_EXPIRY_GRACE = timedelta(days=90)

_DOB_RE = re.compile(r"^(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4})$")


@runtime_checkable
class IdentityVerificationPort(Protocol):
    """The identity-provider surface the kyc-service depends on.

    Shape-compatible with ``connectors.sdk.IdentityConnector.verify_nid``
    (spec/00 §10); the connector lane's porichoy connector satisfies it.
    Returns ``{"matched": bool, "ref": str, "verified_at": str}``; any raised
    exception means "provider unavailable" and routes to human review.
    """

    async def verify_nid(self, nid_number: str, dob: str, selfie_hash: str) -> dict: ...


@runtime_checkable
class NidCipherPort(Protocol):
    """At-rest encryption for the raw NID (vault-lane key custody)."""

    def encrypt(self, plaintext: str) -> bytes: ...

    def decrypt(self, ciphertext: bytes) -> str: ...


class AesGcmNidCipher:
    """AES-256-GCM NID cipher (nonce || ciphertext-with-tag).

    The 32-byte key arrives from key custody (OpenBao via the vault lane);
    the nonce generator is injectable for deterministic tests but defaults
    to cryptographically random 12-byte nonces.
    """

    def __init__(
        self, key: bytes, *, nonce_generator: Callable[[], bytes] | None = None
    ) -> None:
        if len(key) != 32:
            raise ValueError("AES-256-GCM requires a 32-byte key")
        self._key = key
        self._nonce = nonce_generator or (lambda: secrets.token_bytes(12))

    def encrypt(self, plaintext: str) -> bytes:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        nonce = self._nonce()
        if len(nonce) != 12:
            raise ValueError("AES-GCM nonce must be 12 bytes")
        return nonce + AESGCM(self._key).encrypt(nonce, plaintext.encode("utf-8"), None)

    def decrypt(self, ciphertext: bytes) -> str:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        nonce, body = ciphertext[:12], ciphertext[12:]
        return AESGCM(self._key).decrypt(nonce, body, None).decode("utf-8")


@dataclass(frozen=True)
class NidOcrSubmission:
    """POST /v1/kyc/sessions/{id}/nid-ocr body, post-gateway (spec/09 §2)."""

    nid_number_raw: str
    name_en_raw: str
    dob_raw: str
    nid_type: str
    ocr_confidence_score: Decimal
    name_bn_raw: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.ocr_confidence_score, float):
            raise InvalidRequestError(
                "ocr_confidence_score must be Decimal (floats rejected)"
            )


def _normalise_dob(raw: str) -> str:
    """DD/MM/YYYY (Bengali digits accepted) -> ISO YYYY-MM-DD; fails closed."""
    western = normalize_bengali_digits(raw).strip()
    match = _DOB_RE.fullmatch(western)
    if not match:
        raise InvalidRequestError("cannot parse date of birth", code="dob_invalid")
    dd, mm, yyyy = match.groups()
    try:
        return date(int(yyyy), int(mm), int(dd)).isoformat()
    except ValueError as exc:
        raise InvalidRequestError(
            "date of birth is not a valid calendar date", code="dob_invalid"
        ) from exc


def assign_risk_tier(
    *, pep_indicator: bool, dob_iso: str | None, tier: str, as_of: datetime
) -> str:
    """Deterministic risk-tier assignment (spec/09 §KYC Refresh algorithm)."""
    if pep_indicator:
        return "HIGH"
    if dob_iso is not None and age_years(date.fromisoformat(dob_iso), as_of) < 21:
        return "MEDIUM"
    if tier == "REGULAR":
        return "MEDIUM"
    return "LOW"


class CustomerEkycService:
    """The spec/09 kyc-service."""

    def __init__(
        self,
        *,
        store: KycStore,
        identity: IdentityVerificationPort,
        sanctions: SanctionsPort,
        approvals: ApprovalPort,
        audit: AuditPort,
        outbox: OutboxPort,
        cipher: NidCipherPort,
        clock: Clock,
        ocr_confidence_threshold: Decimal = OCR_CONFIDENCE_THRESHOLD,
        review_queue_guard: PorichoyReviewQueueGuard | None = None,
        identity_available: Callable[[], bool] | None = None,
        document_ocr: DocumentOcrPort | None = None,
    ) -> None:
        self._store = store
        self._identity = identity
        self._sanctions = sanctions
        self._approvals = approvals
        self._audit = audit
        self._outbox = outbox
        self._cipher = cipher
        self._clock = clock
        self._threshold = ocr_confidence_threshold
        # spec/16 §I (LR-5): queue-depth cap on the T10 entry path. The
        # availability probe is the Porichoy circuit-breaker view; when it
        # answers False the submission WOULD enter PENDING_HUMAN_REVIEW with
        # PORICHOY_UNAVAILABLE, so the guard runs BEFORE any FSM transition.
        # Probe absent (None) = availability unknown = no pre-refusal.
        self._review_queue_guard = review_queue_guard
        self._identity_available = identity_available
        # Document-OCR provider port (veridyn_ocr_v1 connector shape). None =
        # the OCR intake path is not offered; the direct path is unaffected.
        self._document_ocr = document_ocr

    # ------------------------------------------------------------------
    # T1 — session creation
    # ------------------------------------------------------------------

    def start_session(
        self,
        customer_id: str,
        *,
        tier_requested: str,
        channel: str = "SELF_SERVICE",
        previous_kyc_record_id: str | None = None,
        conn: Any | None = None,
    ) -> KycRecordModel:
        if self._store.get_customer(customer_id, conn=conn) is None:
            raise NotFoundError(f"unknown customer {customer_id}")
        open_session = self._store.find_open_session(customer_id, conn=conn)
        if open_session is not None:
            raise ConflictError(
                "an active KYC session already exists for this customer",
                code="kyc_session_exists",
            )
        now = self._clock.now()
        record = KycRecordModel(
            kyc_record_id=make_id(
                "kyc", {"customer_id": customer_id, "initiated_at": now}
            ),
            customer_id=customer_id,
            state="SUBMITTED",
            tier_requested=tier_requested,
            previous_kyc_record_id=previous_kyc_record_id,
            channel=channel,
            submitted_at=now,
            created_at=now,
            updated_at=now,
        )
        self._store.insert_record(record, conn=conn)
        self._audit_event(
            "KYC_SESSION_CREATED", record, actor_id=customer_id,
            payload={"tier_requested": tier_requested, "channel": channel}, conn=conn,
        )
        self._emit(
            "kyc_record.submitted",
            record,
            {
                "kyc_record_id": record.kyc_record_id,
                "customer_id": customer_id,
                "tier_requested": tier_requested,
                "channel": channel,
            },
            conn=conn,
        )
        return record

    # ------------------------------------------------------------------
    # T2/T3 — NID OCR intake
    # ------------------------------------------------------------------

    def submit_nid_ocr(
        self,
        kyc_record_id: str,
        submission: NidOcrSubmission,
        *,
        conn: Any | None = None,
    ) -> KycRecordModel:
        record = self._require(kyc_record_id, conn=conn)
        if record.state != "SUBMITTED":
            raise ConflictError(
                "NID OCR can only be submitted from SUBMITTED",
                code="invalid_state_transition",
            )
        if submission.ocr_confidence_score < self._threshold:
            raise LimitExceededError(
                "OCR confidence below the acceptance threshold; recapture required",
                code="ocr_confidence_below_threshold",
            )
        nid_digits = normalize_nid_number(submission.nid_number_raw)
        if len(nid_digits) not in (10, 13, 17):
            raise InvalidRequestError(
                "NID must be 10, 13 or 17 digits after normalisation",
                code="nid_format_invalid",
            )
        dob_iso = _normalise_dob(submission.dob_raw)
        if not dob_cross_check(nid_digits, dob_iso):
            # Integrity check fails closed: the embedded birth year contradicts
            # the stated DOB (errata K-12 taxonomy extension).
            raise InvalidRequestError(
                "NID and date of birth do not agree", code="nid_dob_mismatch"
            )
        now = self._clock.now()
        nid_hash = sha256_canonical({"nid_number": nid_digits, "dob": dob_iso})
        is_minor = age_years(date.fromisoformat(dob_iso), now) < 18
        record = replace(
            record,
            nid_hash=nid_hash,
            nid_encrypted=self._cipher.encrypt(nid_digits),
            nid_type=submission.nid_type,
            nid_dob_iso=dob_iso,
            name_en_norm=" ".join(submission.name_en_raw.split()),
            name_bn_norm=(
                " ".join(submission.name_bn_raw.split()) if submission.name_bn_raw else None
            ),
            is_minor=is_minor,
            updated_at=now,
        )
        self._store.update_record(record, conn=conn)
        if is_minor:
            record = self._advance(
                record, "nid_ocr_received", guard="minor", actor_id=record.customer_id,
                conn=conn,
            )
            self._open_review(record, reason="MINOR_ACCOUNT_POLICY", conn=conn)
            return record
        record = self._advance(
            record, "nid_ocr_received", guard="adult", actor_id=record.customer_id, conn=conn
        )
        self._emit(
            "kyc_record.nid_ocr_received",
            record,
            {
                "kyc_record_id": record.kyc_record_id,
                "customer_id": record.customer_id,
                "nid_type": submission.nid_type,
                "ocr_confidence_score": str(submission.ocr_confidence_score),
            },
            conn=conn,
        )
        return record

    # ------------------------------------------------------------------
    # T2/T3 via the document-OCR provider (server-side OCR path)
    # ------------------------------------------------------------------

    async def submit_nid_ocr_document(
        self,
        kyc_record_id: str,
        *,
        document_object_key: str,
        nid_type: str,
        stated_dob_iso: str | None = None,
        fallback_name_en: str | None = None,
        conn: Any | None = None,
    ) -> KycRecordModel:
        """NID intake through the OCR provider port (errata OCR-1).

        The provider turns the uploaded NID image (object-store key) into
        Bengali OCR text; extraction reuses the identity-core port logic
        (10/13/17 forms, DOB cross-check) and the result feeds the SAME
        ``submit_nid_ocr`` intake — T2/T3 guards, hashing, encryption and
        minor routing have exactly one implementation.

        Provider failure of any kind -> ``PENDING_HUMAN_REVIEW`` with reason
        ``OCR_UNAVAILABLE`` (NEVER a hard block — the spec/09 T10 posture).
        Extraction refusals leave the record in ``SUBMITTED`` for recapture.
        """
        record = self._require(kyc_record_id, conn=conn)
        if record.state != "SUBMITTED":
            raise ConflictError(
                "NID OCR can only be submitted from SUBMITTED",
                code="invalid_state_transition",
            )
        if self._document_ocr is None:
            raise InvalidRequestError(
                "no document-OCR provider is configured for this deployment",
                code="ocr_provider_not_configured",
            )
        try:
            extraction = await self._document_ocr.extract_document(document_object_key)
        except Exception:
            # OCR provider unavailable: human review, NEVER a hard block.
            record = self._advance(
                record, "ocr_unavailable", actor_id=record.customer_id, conn=conn
            )
            self._open_review(record, reason=OCR_UNAVAILABLE_REASON, conn=conn)
            return record
        confidence = extraction.confidence
        if isinstance(confidence, float):
            raise InvalidRequestError(
                "OCR confidence must be Decimal (floats rejected)",
                code="ocr_confidence_invalid",
            )
        # Recapture signal outranks extraction refusals: gate confidence first.
        if confidence < self._threshold:
            raise LimitExceededError(
                "OCR confidence below the acceptance threshold; recapture required",
                code="ocr_confidence_below_threshold",
            )
        submission = derive_nid_submission(
            extraction.raw_text,
            nid_type=nid_type,
            confidence=confidence,
            stated_dob_iso=stated_dob_iso,
            fallback_name_en=fallback_name_en,
        )
        # PII-free provenance: object key + content-addressed raw hash only —
        # the OCR text itself never enters an audit payload.
        self._audit_event(
            "KYC_OCR_DOCUMENT_RECEIVED",
            record,
            actor_id=record.customer_id,
            payload={
                "document_object_key": document_object_key,
                "ocr_provider": "veridyn_ocr_v1",
                "ocr_raw_response_hash": extraction.raw_response_hash,
                "ocr_confidence_score": str(confidence),
            },
            conn=conn,
        )
        return self.submit_nid_ocr(kyc_record_id, submission, conn=conn)

    # ------------------------------------------------------------------
    # T5/T7-T12 — biometric attempts
    # ------------------------------------------------------------------

    async def submit_biometric(
        self,
        kyc_record_id: str,
        *,
        selfie_hash: str,
        liveness_token_valid: bool = True,
        liveness_sdk_version: str | None = None,
        conn: Any | None = None,
    ) -> KycRecordModel:
        record = self._require(kyc_record_id, conn=conn)
        if record.state not in ("NID_OCR_RECEIVED", "BIOMETRIC_RETRY_PENDING"):
            raise ConflictError(
                "biometric can only be submitted from NID_OCR_RECEIVED or "
                "BIOMETRIC_RETRY_PENDING",
                code="invalid_state_transition",
            )
        if not liveness_token_valid:
            # fails closed BEFORE any provider call (spec/09 §Liveness)
            raise InvalidRequestError(
                "liveness token is invalid", code="liveness_token_invalid"
            )
        # spec/16 §I (LR-5): when the Porichoy breaker is OPEN this submission
        # WOULD enter PENDING_HUMAN_REVIEW with PORICHOY_UNAVAILABLE — the
        # queue cap runs BEFORE any FSM transition or attempt row. The cap
        # applies ONLY to this entry path; sanctions-adjacent, minor-policy,
        # and retrospective-hit reviews are never refused.
        if (
            self._review_queue_guard is not None
            and self._identity_available is not None
            and not self._identity_available()
        ):
            self._review_queue_guard.check_capacity(conn=conn)
        attempts = self._store.list_attempts(kyc_record_id, conn=conn)
        attempt_number = len(attempts) + 1
        if attempt_number > MAX_BIOMETRIC_ATTEMPTS:
            raise ConflictError(
                "biometric attempt limit exceeded",
                code="biometric_attempt_limit_exceeded",
            )
        record = self._advance(
            record, "biometric_submitted", actor_id=record.customer_id, conn=conn
        )
        if record.nid_encrypted is None or record.nid_dob_iso is None:
            raise ConflictError("NID intake incomplete", code="invalid_state_transition")
        now = self._clock.now()
        try:
            response = await self._identity.verify_nid(
                self._cipher.decrypt(record.nid_encrypted), record.nid_dob_iso, selfie_hash
            )
        except Exception:
            # Identity provider unavailable: human review, NEVER a hard block.
            self._record_attempt(
                record, attempt_number, matched=False,
                response_hash=sha256_canonical({"outcome": "provider_unavailable"}),
                porichoy_ref=None, liveness_sdk_version=liveness_sdk_version,
                attempted_at=now, conn=conn,
            )
            record = self._advance(
                record, "porichoy_unavailable", actor_id=record.customer_id, conn=conn
            )
            self._open_review(record, reason="PORICHOY_UNAVAILABLE", conn=conn)
            return record
        matched = bool(response.get("matched"))
        self._record_attempt(
            record, attempt_number, matched=matched,
            response_hash=sha256_canonical({"matched": matched,
                                            "ref": response.get("ref", "")}),
            porichoy_ref=response.get("ref"), liveness_sdk_version=liveness_sdk_version,
            attempted_at=now, conn=conn,
        )
        record = replace(record, last_biometric_at=now, updated_at=now)
        self._store.update_record(record, conn=conn)
        if matched:
            record = self._advance(
                record, "porichoy_matched", actor_id=record.customer_id, conn=conn
            )
            self._emit(
                "kyc_record.biometric_passed",
                record,
                {
                    "kyc_record_id": record.kyc_record_id,
                    "customer_id": record.customer_id,
                    "attempt_number": attempt_number,
                },
                conn=conn,
            )
            return record
        if attempt_number < MAX_BIOMETRIC_ATTEMPTS:
            return self._advance(
                record, "porichoy_not_matched", guard="retry",
                actor_id=record.customer_id, conn=conn,
            )
        record = self._reject(
            record, "porichoy_not_matched", guard="exhausted",
            reason_code="BIOMETRIC_MAX_RETRIES", conn=conn,
        )
        return record

    def _record_attempt(
        self,
        record: KycRecordModel,
        attempt_number: int,
        *,
        matched: bool,
        response_hash: str,
        porichoy_ref: str | None,
        liveness_sdk_version: str | None,
        attempted_at: datetime,
        conn: Any | None,
    ) -> None:
        self._store.insert_attempt(
            BiometricAttemptRecord(
                attempt_id=make_kernel_id(
                    "kycbio",
                    {"kyc_record_id": record.kyc_record_id, "attempt_number": attempt_number},
                ),
                kyc_record_id=record.kyc_record_id,
                attempt_number=attempt_number,
                attempted_at=attempted_at,
                matched=matched,
                porichoy_ref=porichoy_ref,
                porichoy_response_hash=response_hash,
                liveness_sdk_version=liveness_sdk_version,
            ),
            conn=conn,
        )

    # ------------------------------------------------------------------
    # T13-T15 — tier assignment with sanctions screening
    # ------------------------------------------------------------------

    def assign_tier(
        self,
        kyc_record_id: str,
        *,
        pep_indicator: bool = False,
        conn: Any | None = None,
    ) -> KycRecordModel:
        record = self._require(kyc_record_id, conn=conn)
        if record.state != "BIOMETRIC_PASSED":
            raise ConflictError(
                "tier assignment requires BIOMETRIC_PASSED",
                code="invalid_state_transition",
            )
        screen = self._sanctions.check_sync(
            customer_id=record.customer_id, merchant_id=None
        )
        if screen.hit:
            record = self._advance(
                record, "sanctions_hit", actor_id=PRODUCER, conn=conn
            )
            self._open_review(record, reason="SANCTIONS_ADJACENT", conn=conn)
            return record
        tier = record.tier_requested or "SIMPLIFIED"
        return self._activate(record, tier=tier, pep_indicator=pep_indicator, conn=conn)

    def _activate(
        self,
        record: KycRecordModel,
        *,
        tier: str,
        pep_indicator: bool,
        trigger: str = "tier_assigned",
        guard: str | None = None,
        conn: Any | None,
    ) -> KycRecordModel:
        now = self._clock.now()
        risk_tier = assign_risk_tier(
            pep_indicator=pep_indicator, dob_iso=record.nid_dob_iso, tier=tier, as_of=now
        )
        refresh_due = add_years(now, RISK_TIER_REFRESH_YEARS[risk_tier])
        record = replace(
            record,
            tier=tier,
            tier_assigned_at=now,
            risk_tier=risk_tier,
            refresh_due_at=refresh_due,
            refresh_hard_deadline_at=refresh_due + HARD_EXPIRY_GRACE,
            updated_at=now,
        )
        self._store.update_record(record, conn=conn)
        # Archive the predecessor BEFORE this record turns ACTIVE (the
        # one-ACTIVE-per-customer invariant).
        if record.previous_kyc_record_id is not None:
            self._archive_predecessor(record, conn=conn)
        resolved_guard = guard if guard is not None else tier.lower()
        record = self._advance(
            record, trigger, guard=resolved_guard, actor_id=PRODUCER, conn=conn
        )
        self._emit(
            "kyc_record.tier_assigned",
            record,
            {
                "kyc_record_id": record.kyc_record_id,
                "customer_id": record.customer_id,
                "tier": tier,
                "risk_tier": risk_tier,
                "refresh_due_at": refresh_due,
            },
            conn=conn,
        )
        self._emit(
            "kyc_record.activated",
            record,
            {
                "kyc_record_id": record.kyc_record_id,
                "customer_id": record.customer_id,
                "tier": tier,
            },
            conn=conn,
        )
        return record

    def _archive_predecessor(self, record: KycRecordModel, *, conn: Any | None) -> None:
        predecessor = self._store.get_record(record.previous_kyc_record_id or "", conn=conn)
        if predecessor is None or KYC_TABLE.is_terminal(predecessor.state):
            return
        if predecessor.state not in ("ACTIVE", "REFRESH_OVERDUE", "EXPIRED_HARD"):
            return
        now = self._clock.now()
        rule = KYC_TABLE.resolve(predecessor.state, "new_kyc_cycle_completed")
        predecessor = replace(
            predecessor, state=rule.to_state, archived_at=now, updated_at=now
        )
        self._store.update_record(predecessor, conn=conn)
        self._emit(
            "kyc_record.archived",
            predecessor,
            {
                "kyc_record_id": predecessor.kyc_record_id,
                "customer_id": predecessor.customer_id,
                "successor_kyc_record_id": record.kyc_record_id,
            },
            conn=conn,
        )

    # ------------------------------------------------------------------
    # manual review (PENDING_HUMAN_REVIEW; two-eyes resolution)
    # ------------------------------------------------------------------

    def _open_review(
        self, record: KycRecordModel, *, reason: str, conn: Any | None
    ) -> KycManualReviewRecord:
        approval = self._approvals.request(
            action_type="kyc_manual_review",
            subject_type="KycRecord",
            subject_id=record.kyc_record_id,
            payload={"customer_id": record.customer_id, "review_reason": reason},
            reason=f"kyc manual review: {reason}",
            initiator_id=PRODUCER,
        )
        review = KycManualReviewRecord(
            review_id=make_kernel_id(
                "kycrev",
                {"kyc_record_id": record.kyc_record_id, "created_at": self._clock.now()},
            ),
            kyc_record_id=record.kyc_record_id,
            review_reason=reason,
            approval_request_id=approval.approval_request_id,
            created_at=self._clock.now(),
        )
        self._store.insert_review(review, conn=conn)
        self._emit(
            "kyc_record.pending_human_review",
            record,
            {
                "kyc_record_id": record.kyc_record_id,
                "customer_id": record.customer_id,
                "review_reason": reason,
                "approval_request_id": approval.approval_request_id,
            },
            conn=conn,
        )
        return review

    def resolve_manual_review(
        self,
        kyc_record_id: str,
        *,
        decision: str,
        operator_id: str,
        rejection_reason_code: str | None = None,
        operator_notes: str | None = None,
        pep_indicator: bool = False,
        conn: Any | None = None,
    ) -> KycRecordModel:
        """Two-eyes resolution: the approval gateway enforces that the
        deciding operator differs from the initiator; rejection requires a
        mandatory reason code (spec/09 T18)."""
        record = self._require(kyc_record_id, conn=conn)
        if record.state != "PENDING_HUMAN_REVIEW":
            raise ConflictError(
                "record is not pending human review", code="invalid_state_transition"
            )
        if decision == "REJECT" and rejection_reason_code is None:
            # spec/09 T18: rejection ALWAYS carries a mandatory reason code
            raise InvalidRequestError(
                "rejection requires a reason code", code="rejection_reason_required"
            )
        if decision == "APPROVE_REGULAR" and record.is_minor:
            # spec/09 minors policy: SIMPLIFIED cap, validated BEFORE any decision
            raise ConflictError(
                "minor accounts are capped at SIMPLIFIED", code="minor_simplified_only"
            )
        review = self._store.find_open_review(kyc_record_id, conn=conn)
        if review is None:
            raise NotFoundError("no open manual review for this record")
        if review.approval_request_id is not None:
            self._approvals.decide(
                review.approval_request_id,
                approver_id=operator_id,
                approve=decision != "REJECT",
                decision_reason=operator_notes or decision,
            )
        now = self._clock.now()
        # walk REVIEW_OPEN -> REVIEW_IN_PROGRESS -> REVIEW_CLOSED through the
        # declared 9.2 table (refusal-first; no state is ever assigned directly)
        state = review.review_state
        if state == "REVIEW_OPEN":
            state = KYC_MANUAL_REVIEW_TABLE.resolve(state, "assigned").to_state
        state = KYC_MANUAL_REVIEW_TABLE.resolve(state, "decision_made").to_state
        review = replace(
            review,
            review_state=state,
            assigned_to_operator_id=review.assigned_to_operator_id or operator_id,
            reviewer_notes=operator_notes,
            decision=decision,
            rejection_reason_code=rejection_reason_code,
            decided_at=now,
            decided_by_operator_id=operator_id,
        )
        self._store.update_review(review, conn=conn)
        if decision == "REJECT":
            assert rejection_reason_code is not None  # validated above
            return self._reject(
                record, "manual_review_reject", reason_code=rejection_reason_code, conn=conn
            )
        if decision == "APPROVE_SIMPLIFIED":
            return self._activate(
                record,
                tier="SIMPLIFIED",
                pep_indicator=pep_indicator,
                trigger="manual_review_approve_simplified",
                guard="",
                conn=conn,
            )
        if decision == "APPROVE_REGULAR":
            return self._activate(
                record,
                tier="REGULAR",
                pep_indicator=pep_indicator,
                trigger="manual_review_approve_regular",
                guard="",
                conn=conn,
            )
        raise InvalidRequestError(f"unknown decision {decision!r}")

    # ------------------------------------------------------------------
    # T19/T20 — upgrade + refresh successor creation
    # ------------------------------------------------------------------

    def request_tier_upgrade(
        self, kyc_record_id: str, *, conn: Any | None = None
    ) -> KycRecordModel:
        record = self._require(kyc_record_id, conn=conn)
        record = self._advance(
            record, "tier_upgrade_requested", actor_id=record.customer_id, conn=conn
        )
        successor = self.start_session(
            record.customer_id,
            tier_requested="REGULAR",
            channel=record.channel,
            previous_kyc_record_id=record.kyc_record_id,
            conn=conn,
        )
        return successor

    def trigger_refresh(
        self, kyc_record_id: str, *, conn: Any | None = None
    ) -> KycRecordModel:
        """T20 (errata K-13): the old record stays ACTIVE; the successor is
        created in REFRESH_PENDING."""
        record = self._require(kyc_record_id, conn=conn)
        if record.refresh_due_at is None or self._clock.now() < (
            record.refresh_due_at - REFRESH_OVERDUE_GRACE
        ):
            raise ConflictError("refresh is not due yet", code="refresh_not_due")
        record = self._advance(
            record, "refresh_triggered", actor_id=PRODUCER, conn=conn
        )
        now = self._clock.now()
        successor = KycRecordModel(
            kyc_record_id=make_id(
                "kyc", {"customer_id": record.customer_id, "initiated_at": now}
            ),
            customer_id=record.customer_id,
            state="REFRESH_PENDING",
            tier_requested=record.tier,
            previous_kyc_record_id=record.kyc_record_id,
            channel=record.channel,
            created_at=now,
            updated_at=now,
        )
        self._store.insert_record(successor, conn=conn)
        self._emit(
            "kyc_record.refresh_due",
            record,
            {
                "kyc_record_id": record.kyc_record_id,
                "customer_id": record.customer_id,
                "refresh_due_at": record.refresh_due_at,
                "new_kyc_record_id": successor.kyc_record_id,
            },
            conn=conn,
        )
        return successor

    def start_refresh_session(
        self, kyc_record_id: str, *, conn: Any | None = None
    ) -> KycRecordModel:
        record = self._require(kyc_record_id, conn=conn)
        record = self._advance(
            record, "refresh_session_started", actor_id=record.customer_id, conn=conn
        )
        now = self._clock.now()
        record = replace(record, submitted_at=now, updated_at=now)
        self._store.update_record(record, conn=conn)
        return record

    def record_retrospective_hit(
        self, kyc_record_id: str, *, conn: Any | None = None
    ) -> KycRecordModel:
        """T26 — retrospective sanctions sweep hit on an ACTIVE record."""
        record = self._require(kyc_record_id, conn=conn)
        record = self._advance(
            record, "sanctions_retrospective_hit", actor_id=PRODUCER, conn=conn
        )
        self._open_review(record, reason="SANCTIONS_ADJACENT", conn=conn)
        return record

    # ------------------------------------------------------------------
    # scheduler sweep (timeouts + refresh ladder)
    # ------------------------------------------------------------------

    def sweep(self, *, conn: Any | None = None) -> int:
        """Apply spec/09 timeout transitions; returns records moved."""
        now = self._clock.now()
        moved = 0
        for record in self._store.list_records(conn=conn):
            if record.state in ("SUBMITTED", "NID_OCR_RECEIVED"):
                started = record.submitted_at or record.created_at
                if now >= started + SESSION_TTL:
                    self._reject(
                        record, "session_timeout", reason_code="SESSION_TIMEOUT", conn=conn
                    )
                    moved += 1
            elif record.state == "BIOMETRIC_RETRY_PENDING":
                last = record.last_biometric_at or record.updated_at
                if now >= last + BIOMETRIC_RETRY_TTL:
                    self._reject(
                        record, "biometric_timeout", reason_code="BIOMETRIC_TIMEOUT",
                        conn=conn,
                    )
                    moved += 1
            elif record.state == "ACTIVE" and record.refresh_due_at is not None:
                if now >= record.refresh_due_at + REFRESH_OVERDUE_GRACE:
                    updated = self._advance(
                        record, "refresh_overdue", actor_id=PRODUCER, conn=conn
                    )
                    self._emit(
                        "kyc_record.refresh_overdue",
                        updated,
                        {
                            "kyc_record_id": updated.kyc_record_id,
                            "customer_id": updated.customer_id,
                            "limits_downgraded_to": "SIMPLIFIED",
                        },
                        conn=conn,
                    )
                    moved += 1
            elif (
                record.state == "REFRESH_OVERDUE"
                and record.refresh_hard_deadline_at is not None
                and now >= record.refresh_hard_deadline_at
            ):
                updated = self._advance(
                    record, "refresh_hard_expired", actor_id=PRODUCER, conn=conn
                )
                updated = replace(updated, expired_at=now, updated_at=now)
                self._store.update_record(updated, conn=conn)
                self._emit(
                    "kyc_record.expired_hard",
                    updated,
                    {
                        "kyc_record_id": updated.kyc_record_id,
                        "customer_id": updated.customer_id,
                        "wallet_suspended": True,
                    },
                    conn=conn,
                )
                moved += 1
        return moved

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _require(self, kyc_record_id: str, *, conn: Any | None) -> KycRecordModel:
        record = self._store.get_record(kyc_record_id, conn=conn)
        if record is None:
            raise NotFoundError(f"unknown kyc record {kyc_record_id}")
        return record

    def _advance(
        self,
        record: KycRecordModel,
        trigger: str,
        *,
        guard: str | None = None,
        actor_id: str,
        conn: Any | None,
    ) -> KycRecordModel:
        resolved_guard = guard if guard else None
        rule = KYC_TABLE.resolve(record.state, trigger, guard=resolved_guard)
        now = self._clock.now()
        updated = replace(record, state=rule.to_state, updated_at=now)
        self._store.update_record(updated, conn=conn)
        self._audit_event(
            "KYC_STATE_TRANSITION",
            updated,
            actor_id=actor_id,
            payload={"trigger": trigger},
            from_state=record.state,
            to_state=rule.to_state,
            conn=conn,
        )
        return updated

    def _reject(
        self,
        record: KycRecordModel,
        trigger: str,
        *,
        guard: str | None = None,
        reason_code: str,
        conn: Any | None,
    ) -> KycRecordModel:
        rule = KYC_TABLE.resolve(record.state, trigger, guard=guard)
        now = self._clock.now()
        updated = replace(
            record, state=rule.to_state, rejection_reason_code=reason_code, updated_at=now
        )
        self._store.update_record(updated, conn=conn)
        self._audit_event(
            "KYC_STATE_TRANSITION",
            updated,
            actor_id=PRODUCER,
            payload={"trigger": trigger, "rejection_reason_code": reason_code},
            from_state=record.state,
            to_state=rule.to_state,
            conn=conn,
        )
        self._emit(
            "kyc_record.rejected",
            updated,
            {
                "kyc_record_id": updated.kyc_record_id,
                "customer_id": updated.customer_id,
                "rejection_reason_code": reason_code,
            },
            conn=conn,
        )
        return updated

    def _audit_event(
        self,
        event_type: str,
        record: KycRecordModel,
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
                subject_type="KycRecord",
                subject_id=record.kyc_record_id,
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
        record: KycRecordModel,
        payload: dict[str, object],
        *,
        conn: Any | None,
    ) -> None:
        event = build_event(
            event_type=event_type,
            subject_type="KycRecord",
            subject_id=record.kyc_record_id,
            producer=PRODUCER,
            topic="kyc.events",
            payload=payload,
            occurred_at=self._clock.now(),
        )
        self._outbox.enqueue(event, conn=conn)

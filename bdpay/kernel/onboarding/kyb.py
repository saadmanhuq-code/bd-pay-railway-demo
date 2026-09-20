"""MerchantOnboardingService — the spec/08 onboarding-service for merchants.

Drives the 8-FSM-1 merchant KYB table: application intake with MCC
auto-reject, the document checklist, the UBO graph (acyclic, completeness
rule), the Porichoy verification outcomes (unavailable NEVER hard-blocks —
``PENDING_HUMAN_REVIEW`` fallback), the high-risk-MCC enhanced-due-diligence
branch, sanctions review through :class:`~bdpay.platform.interfaces.SanctionsPort`,
two-eyes activation through :class:`~bdpay.platform.interfaces.ApprovalPort`,
and API-key issuance with a hash-stored secret (plaintext returned exactly
once, never persisted).

Errata notes (SPEC_ERRATA-LANE-A-kernel.md):

- K-11: spec/08 §8.1 says a prohibited-MCC application gets 403 and "do not
  create a record" while 8-FSM-1 declares
  ``APPLICATION_SUBMITTED --mcc_prohibited--> REJECTED`` with audit + event.
  Resolved in favour of the audited FSM path: the record is created and
  immediately rejected (compliance trail preserved), and the caller still
  receives the 403 ``prohibited_mcc`` refusal.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import timedelta
from typing import Any, Protocol, runtime_checkable

from bdpay.kernel.dates import add_months as _add_months
from bdpay.kernel.fsm import TransitionTable
from bdpay.kernel.ids_ext import make_kernel_id
from bdpay.kernel.onboarding.kyb_models import (
    ApiKeyIssuanceRecord,
    KybDocumentRecord,
    KybRecord,
    MerchantRecord,
    UboEdgeRecord,
    UboNodeRecord,
)
from bdpay.kernel.onboarding.kyb_repository import KybStore
from bdpay.kernel.onboarding.kyb_states import (
    DOCUMENT_REVIEW_TABLE,
    MERCHANT_KYB_TABLE,
    REJECTION_REASON_CODES,
    required_document_types,
)
from bdpay.kernel.preflight import MerchantFeeProfile, MerchantGates
from bdpay.platform.bangla import normalize_bengali_digits
from bdpay.platform.canonical import sha256_canonical
from bdpay.platform.clock import Clock
from bdpay.platform.errors import (
    AuthorizationError,
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
    "ApiKeyIssued",
    "DEFAULT_MCC_RULES",
    "GatewayKeyPort",
    "KybStoreMerchantFeeProfiles",
    "KybStoreMerchantGates",
    "MccRule",
    "MerchantApplication",
    "MerchantLedgerPort",
    "MerchantOnboardingService",
    "REFRESH_INTERVAL_MONTHS",
]

PRODUCER = "onboarding-service@1"

#: Periodic KYB refresh cadence per risk tier (spec/08: HIGH 1yr / MEDIUM 2yr / LOW 5yr).
REFRESH_INTERVAL_MONTHS: dict[str, int] = {"HIGH": 12, "MEDIUM": 24, "LOW": 60}

#: TTL windows from the 8-FSM-1 timeout table.
APPLICATION_TTL = timedelta(days=30)
DOCUMENTS_TTL = timedelta(days=14)
AGREEMENT_TTL = timedelta(days=14)
REFRESH_TTL = timedelta(days=21)

_PHONE_RE = re.compile(r"^01[3-9]\d{8}$")
_MCC_RE = re.compile(r"^\d{4}$")
_ROUTING_RE = re.compile(r"^\d{9}$")
_TIN_RE = re.compile(r"^\d{12}$")


@runtime_checkable
class MerchantLedgerPort(Protocol):
    """Activation side-effect: open the merchant's ledger accounts.

    The real implementation is ``_MerchantLedgerAdapter`` in ``bdpay.app``.
    Test doubles can satisfy this protocol with any object that provides the
    method.
    """

    def open_merchant_accounts(
        self, merchant_id: str, *, conn: Any | None = None
    ) -> None: ...


@runtime_checkable
class GatewayKeyPort(Protocol):
    """Activation side-effect: mint a live bcrypt gateway credential.

    Called from ``sign_agreement`` inside the KYB store transaction.  The
    real implementation is ``_GatewayKeyAdapter`` in ``bdpay.app``, which
    delegates to ``ApiKeyService.create_key``.

    ``conn`` is the caller's transaction connection so the gateway api-key
    insert is atomic with the KYB activation (commit-or-rollback together);
    a ``None`` conn means the port runs on its own connection (standalone
    callers, tests).

    Returns ``(gateway_key_id, plaintext_secret)`` — the ``gateway_key_id``
    is the real ``akey_*`` row in ``api_keys`` that gateway bearer/HMAC auth
    resolves, and the plaintext is the once-only bearer credential.
    """

    def issue_live_key(
        self,
        merchant_id: str,
        key_name: str,
        *,
        scopes: tuple[str, ...],
        conn: Any | None = None,
    ) -> tuple[str, str]: ...  # (gateway_key_id, plaintext_secret)


@dataclass(frozen=True)
class MccRule:
    """One ``kyb_mcc_risk_rules`` disposition (spec/08 §MCC risk rules)."""

    mcc_code: str
    disposition: str  # AUTO_REJECT | ENHANCED_DUE_DILIGENCE | STANDARD
    risk_tier: str | None = None  # for EDD/STANDARD rows

    def __post_init__(self) -> None:
        if self.disposition not in ("AUTO_REJECT", "ENHANCED_DUE_DILIGENCE", "STANDARD"):
            raise InvalidRequestError(f"unknown disposition {self.disposition!r}")


#: Seed list per spec/08 DDL §Section 4 (research 03 §5.3 / §6.3).
DEFAULT_MCC_RULES: dict[str, MccRule] = {
    rule.mcc_code: rule
    for rule in (
        MccRule("7995", "AUTO_REJECT"),
        MccRule("7994", "AUTO_REJECT"),
        MccRule("5912", "AUTO_REJECT"),
        MccRule("6211", "AUTO_REJECT"),
        MccRule("7273", "AUTO_REJECT"),
        MccRule("5999", "ENHANCED_DUE_DILIGENCE", "HIGH"),
        MccRule("7841", "ENHANCED_DUE_DILIGENCE", "HIGH"),
        MccRule("5933", "ENHANCED_DUE_DILIGENCE", "HIGH"),
        MccRule("6012", "ENHANCED_DUE_DILIGENCE", "HIGH"),
    )
}


@dataclass(frozen=True)
class MerchantApplication:
    """POST /v1/merchants body, post-gateway (spec/08 §8.1)."""

    legal_name: str
    registration_type: str
    trade_license_number: str
    tin_number: str
    primary_business_mcc: str
    contact_phone: str
    bank_routing_number: str
    requested_services: tuple[str, ...] = field(default_factory=tuple)
    legal_name_bn: str | None = None
    merchant_display_name: str | None = None
    business_city: str | None = None
    registered_address: Mapping[str, Any] = field(default_factory=dict)
    bangla_qr_merchant_pan: str | None = None
    bangla_qr_enabled: bool = False
    website_url: str | None = None


@dataclass(frozen=True)
class ApiKeyIssued:
    """One-shot API-key material: the plaintext leaves this object only."""

    key_id: str
    plaintext_secret: str
    secret_hash: str


class KybStoreMerchantGates:
    """:class:`~bdpay.kernel.preflight.MerchantGatePort` over a KybStore."""

    def __init__(self, store: KybStore) -> None:
        self._store = store

    def gates(self, merchant_id: str) -> MerchantGates | None:
        merchant = self._store.get_merchant(merchant_id)
        if merchant is None:
            return None
        record = (
            self._store.get_record(merchant.kyb_record_id)
            if merchant.kyb_record_id
            else None
        )
        return MerchantGates(
            merchant_id=merchant_id,
            kyb_active=merchant.kyb_status == "ACTIVE",
            payout_blocked=merchant.payout_blocked,
            sanctions_cleared=(
                record is not None and record.sanctions_cleared_at is not None
            ),
            # ACTIVE is only reachable through agreement_signed (8-FSM-1), so
            # the merchant mirror's ACTIVE standing IS the signed-agreement gate.
            agreement_signed=merchant.kyb_status == "ACTIVE",
            mdr_basis_points=merchant.mdr_basis_points or 0,
        )


class KybStoreMerchantFeeProfiles:
    """:class:`~bdpay.kernel.preflight.MerchantFeeProfilePort` over a KybStore.

    ``merchant_class`` is the migration-0091 column default ``STANDARD``:
    MICRO classification is FC-06-gated (VERIFY-BEFORE-EXTERNAL) and KYB does
    not yet record a verified turnover basis, so every onboarded merchant
    resolves at the regulated ceiling — tiering can only lower a fee once the
    classification source lands, never raise one retroactively.
    """

    def __init__(self, store: KybStore) -> None:
        self._store = store

    def fee_profile(self, merchant_id: str) -> MerchantFeeProfile | None:
        merchant = self._store.get_merchant(merchant_id)
        if merchant is None:
            return None
        record = (
            self._store.get_record(merchant.kyb_record_id)
            if merchant.kyb_record_id
            else None
        )
        return MerchantFeeProfile(
            merchant_class="STANDARD",
            mcc=record.primary_business_mcc if record is not None else None,
        )


class MerchantOnboardingService:
    """The spec/08 onboarding-service (merchant variant)."""

    def __init__(
        self,
        *,
        store: KybStore,
        sanctions: SanctionsPort,
        approvals: ApprovalPort,
        audit: AuditPort,
        outbox: OutboxPort,
        clock: Clock,
        ledger: MerchantLedgerPort,
        gateway_keys: GatewayKeyPort | None = None,
        mcc_rules: Mapping[str, MccRule] | None = None,
        secret_generator: Callable[[], str] | None = None,
    ) -> None:
        self._store = store
        self._sanctions = sanctions
        self._approvals = approvals
        self._audit = audit
        self._outbox = outbox
        self._clock = clock
        self._ledger = ledger
        self._gateway_keys = gateway_keys
        self._mcc_rules = dict(mcc_rules if mcc_rules is not None else DEFAULT_MCC_RULES)
        self._secret_generator = secret_generator or (lambda: secrets.token_urlsafe(32))

    # ------------------------------------------------------------------
    # application intake (spec/08 §8.1)
    # ------------------------------------------------------------------

    def submit_application(
        self, application: MerchantApplication, *, actor_id: str, conn: Any | None = None
    ) -> tuple[MerchantRecord, KybRecord]:
        """Create the merchant + KYB record; MCC auto-reject fails closed."""
        if conn is None:
            transaction = getattr(self._store, "transaction", None)
            if callable(transaction):
                with transaction() as tx_conn:
                    return self.submit_application(
                        application, actor_id=actor_id, conn=tx_conn
                    )
        tin = normalize_bengali_digits(application.tin_number).strip()
        if not _TIN_RE.fullmatch(tin):
            raise InvalidRequestError(
                "tin_number must normalize to 12 ASCII digits", code="tin_invalid"
            )
        phone = normalize_bengali_digits(application.contact_phone).strip()
        if phone.startswith("+880"):
            phone = "0" + phone[4:]
        if not _PHONE_RE.fullmatch(phone):
            raise InvalidRequestError(
                "contact_phone must be a valid BD mobile number", code="phone_invalid"
            )
        if not _MCC_RE.fullmatch(application.primary_business_mcc):
            raise InvalidRequestError(
                "primary_business_mcc must be a 4-digit ISO 18245 code",
                code="mcc_invalid",
            )
        if not _ROUTING_RE.fullmatch(
            normalize_bengali_digits(application.bank_routing_number)
        ):
            raise InvalidRequestError(
                "bank_routing_number must be 9 digits", code="routing_number_invalid"
            )
        if "CARD_ACQUIRING" in application.requested_services:
            if not (application.website_url or "").startswith("https://"):
                raise InvalidRequestError(
                    "card acquiring requires an https website",
                    code="website_required_for_card",
                )
        registered_address = dict(application.registered_address)
        if application.business_city is not None:
            registered_address["city"] = application.business_city.strip()
        merchant_display_name = (
            application.merchant_display_name.strip()
            if application.merchant_display_name
            else application.legal_name.strip()
        )
        bangla_qr_pan = (
            application.bangla_qr_merchant_pan.strip()
            if application.bangla_qr_merchant_pan
            else None
        )
        bangla_qr_enabled = application.bangla_qr_enabled or bangla_qr_pan is not None
        if bangla_qr_enabled:
            if bangla_qr_pan is None:
                raise InvalidRequestError(
                    "bangla_qr_merchant_pan is required when Bangla QR is enabled",
                    code="qr_merchant_pan_required",
                )
            if not bangla_qr_pan.isascii():
                raise InvalidRequestError(
                    "bangla_qr_merchant_pan must be ASCII",
                    code="qr_merchant_pan_invalid",
                )
            if not merchant_display_name.isascii():
                raise InvalidRequestError(
                    "merchant_display_name must be ASCII/Latin for QR ID 59",
                    code="qr_display_name_invalid",
                )
            if not str(registered_address.get("city", "")).strip():
                raise InvalidRequestError(
                    "business_city is required when Bangla QR is enabled",
                    code="qr_city_required",
                )
        now = self._clock.now()
        merchant_id = make_id(
            "mrch",
            {
                "legal_name": application.legal_name,
                "tin_number": tin,
                "created_at": now,
            },
        )
        kyb_record_id = make_id(
            "kyb",
            {"subject_type": "MERCHANT", "subject_id": merchant_id, "submitted_at": now},
        )
        merchant = MerchantRecord(
            merchant_id=merchant_id,
            legal_name=application.legal_name,
            kyb_status="APPLICATION_SUBMITTED",
            kyb_record_id=kyb_record_id,
            created_at=now,
            updated_at=now,
        )
        record = KybRecord(
            kyb_record_id=kyb_record_id,
            subject_type="MERCHANT",
            subject_id=merchant_id,
            kyb_status="APPLICATION_SUBMITTED",
            legal_name=application.legal_name,
            legal_name_bn=application.legal_name_bn,
            merchant_display_name=merchant_display_name,
            registration_type=application.registration_type,
            tin_number=tin,
            primary_business_mcc=application.primary_business_mcc,
            requested_services=tuple(application.requested_services),
            contact_phone_e164="+880" + phone[1:],
            registered_address=registered_address,
            bank_routing_number=normalize_bengali_digits(application.bank_routing_number),
            website_url=application.website_url,
            bangla_qr_merchant_pan=bangla_qr_pan,
            bangla_qr_enabled=bangla_qr_enabled,
            application_submitted_at=now,
            created_at=now,
            updated_at=now,
        )
        self._store.insert_merchant(merchant, conn=conn)
        self._store.insert_record(record, conn=conn)
        self._emit(
            "merchant.application_submitted",
            merchant_id,
            {
                "merchant_id": merchant_id,
                "primary_mcc": application.primary_business_mcc,
                "registration_type": application.registration_type,
                "requested_services": list(application.requested_services),
            },
            conn=conn,
        )
        rule = self._mcc_rules.get(application.primary_business_mcc)
        if rule is not None and rule.disposition == "AUTO_REJECT":
            # errata K-11: audited rejection, then the 403 refusal
            record, merchant = self._advance(
                record,
                merchant,
                "mcc_prohibited",
                actor_id=actor_id,
                rejection_reason_code="PROHIBITED_MCC",
                conn=conn,
            )
            raise AuthorizationError(
                "merchant category is prohibited; application rejected",
                code="prohibited_mcc",
            )
        record, merchant = self._advance(
            record, merchant, "application_accepted", actor_id=actor_id, conn=conn
        )
        return merchant, record

    # ------------------------------------------------------------------
    # documents (spec/08 §8.2, 8-FSM-3)
    # ------------------------------------------------------------------

    def upload_document(
        self,
        kyb_record_id: str,
        *,
        document_type: str,
        content_hash: str,
        uploaded_by: str,
        ubo_node_id: str | None = None,
        conn: Any | None = None,
    ) -> KybDocumentRecord:
        """Content-addressed intake; same (type, hash) is idempotent."""
        record = self._require_record(kyb_record_id, conn=conn)
        for existing in self._store.list_documents(kyb_record_id, conn=conn):
            if (
                existing.document_type == document_type
                and existing.content_hash == content_hash
            ):
                return existing  # idempotent re-upload
        now = self._clock.now()
        document_id = make_kernel_id(
            "doc",
            {
                "kyb_record_id": kyb_record_id,
                "document_type": document_type,
                "content_hash": content_hash,
            },
        )
        document = KybDocumentRecord(
            document_id=document_id,
            kyb_record_id=kyb_record_id,
            document_type=document_type,
            content_hash=content_hash,
            storage_pointer=(
                f"s3://bdpay-kyb-docs/{record.subject_id}/{document_id}/"
                f"{content_hash[:8]}"
            ),
            ubo_node_id=ubo_node_id,
            uploaded_at=now,
            uploaded_by=uploaded_by,
        )
        self._store.insert_document(document, conn=conn)
        self._audit_event(
            "KYB_DOCUMENT_UPLOADED",
            record,
            actor_id=uploaded_by,
            payload={"document_id": document_id, "document_type": document_type},
            conn=conn,
        )
        return document

    def review_document(
        self,
        document_id: str,
        *,
        accept: bool,
        operator_id: str,
        notes: str | None = None,
        conn: Any | None = None,
    ) -> KybDocumentRecord:
        documents = {
            d.document_id: d
            for record in self._store.list_records(conn=conn)
            for d in self._store.list_documents(record.kyb_record_id, conn=conn)
        }
        document = documents.get(document_id)
        if document is None:
            raise NotFoundError(f"unknown document {document_id}")
        trigger = "operator_accepted" if accept else "operator_rejected"
        rule = DOCUMENT_REVIEW_TABLE.resolve(document.review_status, trigger)
        if not accept and not notes:
            raise InvalidRequestError(
                "document rejection requires operator notes", code="notes_required"
            )
        now = self._clock.now()
        updated = replace(
            document,
            review_status=rule.to_state,
            reviewed_by=operator_id,
            reviewed_at=now,
            review_notes=notes,
        )
        self._store.update_document(updated, conn=conn)
        return updated

    def check_documents_complete(
        self, kyb_record_id: str, *, actor_id: str, conn: Any | None = None
    ) -> KybRecord:
        """Advance DOCUMENTS_PENDING -> KYB_IN_PROGRESS when the checklist
        is fully present with no REJECTED documents (8-FSM-1 guard)."""
        record = self._require_record(kyb_record_id, conn=conn)
        required = required_document_types(
            record.registration_type or "SOLE_PROPRIETORSHIP",
            record.requested_services,
        )
        documents = self._store.list_documents(kyb_record_id, conn=conn)
        present = {
            d.document_type
            for d in documents
            if d.review_status in ("PENDING", "OCR_COMPLETE", "ACCEPTED")
        }
        rejected = {d.document_type for d in documents if d.review_status == "REJECTED"}
        missing = sorted(required - present)
        if missing or (required & rejected) - present:
            record, _ = self._advance(
                record,
                self._require_merchant(record.subject_id, conn=conn),
                "documents_incomplete",
                actor_id=actor_id,
                conn=conn,
            )
            raise LimitExceededError(
                f"required documents missing: {missing}",
                code="kyb_submission_incomplete",
            )
        merchant = self._require_merchant(record.subject_id, conn=conn)
        record, _ = self._advance(
            record, merchant, "documents_complete", actor_id=actor_id, conn=conn
        )
        return record

    # ------------------------------------------------------------------
    # UBO graph (spec/08 §8.4 / §8.5)
    # ------------------------------------------------------------------

    def add_ubo_node(
        self,
        kyb_record_id: str,
        *,
        node_type: str,
        full_name_en: str,
        created_by: str,
        full_name_bn: str | None = None,
        nid_number: str | None = None,
        dob: str | None = None,
        nationality: str = "BD",
        is_ultimate_beneficial_owner: bool = False,
        ownership_percentage_direct: object | None = None,
        role: str = "SHAREHOLDER",
        parent_node_id: str | None = None,
        conn: Any | None = None,
    ) -> UboNodeRecord:
        from datetime import date as date_type
        from decimal import Decimal

        record = self._require_record(kyb_record_id, conn=conn)
        nid_hash = None
        if nid_number is not None:
            normalized = normalize_bengali_digits(nid_number).strip()
            # NEVER include the raw NID in the ID payload — hash it first (spec/08 DDL)
            nid_hash = sha256_canonical({"nid_number": normalized})
        ubo_node_id = make_kernel_id(
            "ubon",
            {
                "kyb_record_id": kyb_record_id,
                "node_type": node_type,
                "identifier_hash": nid_hash or sha256_canonical({"name": full_name_en}),
            },
        )
        if parent_node_id is not None:
            self._reject_cycles(kyb_record_id, parent_node_id, ubo_node_id, conn=conn)
        now = self._clock.now()
        pct = (
            Decimal(str(ownership_percentage_direct))
            if isinstance(ownership_percentage_direct, int | str)
            else ownership_percentage_direct
        )
        node = UboNodeRecord(
            ubo_node_id=ubo_node_id,
            kyb_record_id=kyb_record_id,
            node_type=node_type,
            full_name_en=full_name_en,
            full_name_bn=full_name_bn,
            nid_number_hash=nid_hash,
            dob=date_type.fromisoformat(dob) if dob else None,
            nationality=nationality,
            is_ultimate_beneficial_owner=is_ultimate_beneficial_owner,
            ownership_percentage_direct=pct,  # type: ignore[arg-type]
            role=role,
            porichoy_status="PENDING" if node_type == "NATURAL_PERSON" else "NOT_REQUIRED",
            created_at=now,
            created_by=created_by,
        )
        self._store.insert_ubo_node(node, conn=conn)
        if parent_node_id is not None:
            edge = UboEdgeRecord(
                edge_id=make_kernel_id(
                    "uboe",
                    {"parent_node_id": parent_node_id, "child_node_id": ubo_node_id},
                ),
                kyb_record_id=kyb_record_id,
                parent_node_id=parent_node_id,
                child_node_id=ubo_node_id,
                ownership_percentage=node.ownership_percentage_direct,
                created_at=now,
            )
            self._store.insert_ubo_edge(edge, conn=conn)
        self._audit_event(
            "KYB_UBO_ADDED",
            record,
            actor_id=created_by,
            payload={"ubo_node_id": ubo_node_id},
            conn=conn,
        )
        if record.kyb_status == "ACTIVE":
            self._emit(
                "ubo.added",
                record.subject_id,
                {"merchant_id": record.subject_id, "ubo_node_id": ubo_node_id},
                conn=conn,
            )
        return node

    def _reject_cycles(
        self,
        kyb_record_id: str,
        parent_node_id: str,
        new_node_id: str,
        *,
        conn: Any | None,
    ) -> None:
        """Ownership chain integrity: the UBO graph must stay acyclic."""
        parents: dict[str, list[str]] = {}
        for edge in self._store.list_ubo_edges(kyb_record_id, conn=conn):
            parents.setdefault(edge.child_node_id, []).append(edge.parent_node_id)
        seen: set[str] = set()
        frontier = [parent_node_id]
        while frontier:
            node = frontier.pop()
            if node == new_node_id:
                raise ConflictError(
                    "ownership cycle detected; UBO graph must be acyclic",
                    code="ubo_cycle_detected",
                )
            if node in seen:
                continue
            seen.add(node)
            frontier.extend(parents.get(node, []))

    def record_porichoy_result(
        self,
        ubo_node_id: str,
        *,
        outcome: str,
        actor_id: str,
        porichoy_ref: str | None = None,
        conn: Any | None = None,
    ) -> UboNodeRecord:
        """Apply a Porichoy verification outcome (spec/08 §8.5 callback rules)."""
        node = self._store.get_ubo_node(ubo_node_id, conn=conn)
        if node is None:
            raise NotFoundError(f"unknown ubo node {ubo_node_id}")
        record = self._require_record(node.kyb_record_id, conn=conn)
        merchant = self._require_merchant(record.subject_id, conn=conn)
        now = self._clock.now()
        if outcome == "matched":
            node = replace(
                node,
                porichoy_status="VERIFIED",
                porichoy_ref=porichoy_ref,
                porichoy_verified_at=now,
            )
            self._store.update_ubo_node(node, conn=conn)
        elif outcome == "mismatch":
            node = replace(node, porichoy_status="MISMATCH")
            self._store.update_ubo_node(node, conn=conn)
            if record.kyb_status == "KYB_IN_PROGRESS":
                self._advance(record, merchant, "ubo_nid_mismatch", actor_id=actor_id, conn=conn)
            self._emit(
                "merchant.ubo_nid_mismatch",
                record.subject_id,
                {"merchant_id": record.subject_id, "ubo_node_id": ubo_node_id},
                conn=conn,
            )
        elif outcome == "unavailable":
            # Identity provider down: route to human review, NEVER hard-block.
            node = replace(node, porichoy_status="UNAVAILABLE")
            self._store.update_ubo_node(node, conn=conn)
            if record.kyb_status == "KYB_IN_PROGRESS":
                self._advance(
                    record, merchant, "porichoy_unavailable", actor_id=actor_id, conn=conn
                )
        else:
            raise InvalidRequestError(f"unknown porichoy outcome {outcome!r}")
        self._audit_event(
            "KYB_UBO_VERIFICATION",
            record,
            actor_id=actor_id,
            payload={"ubo_node_id": ubo_node_id, "outcome": outcome},
            conn=conn,
        )
        return node

    def manual_verify_ubo(
        self,
        ubo_node_id: str,
        *,
        operator_id: str,
        note: str,
        conn: Any | None = None,
    ) -> UboNodeRecord:
        """Operator fallback after provider unavailability (min-50-char note)."""
        if len(note) < 50:
            raise InvalidRequestError(
                "manual verification requires a note of at least 50 characters",
                code="manual_verify_note_too_short",
            )
        node = self._store.get_ubo_node(ubo_node_id, conn=conn)
        if node is None:
            raise NotFoundError(f"unknown ubo node {ubo_node_id}")
        record = self._require_record(node.kyb_record_id, conn=conn)
        merchant = self._require_merchant(record.subject_id, conn=conn)
        now = self._clock.now()
        node = replace(
            node,
            porichoy_status="MANUALLY_VERIFIED",
            manual_verify_by=operator_id,
            manual_verify_at=now,
            manual_verify_note=note,
        )
        self._store.update_ubo_node(node, conn=conn)
        if record.kyb_status == "PENDING_HUMAN_REVIEW":
            self._advance(record, merchant, "operator_manual_verify", actor_id=operator_id,
                          conn=conn)
        return node

    # ------------------------------------------------------------------
    # submission, EDD, sanctions review (spec/08 §8.6, 8-FSM-1)
    # ------------------------------------------------------------------

    def submit_for_review(
        self, kyb_record_id: str, *, actor_id: str, conn: Any | None = None
    ) -> KybRecord:
        record = self._require_record(kyb_record_id, conn=conn)
        merchant = self._require_merchant(record.subject_id, conn=conn)
        failures = self._submission_failures(record, conn=conn)
        if failures:
            raise LimitExceededError(
                f"KYB submission incomplete: {failures}",
                code="kyb_submission_incomplete",
            )
        now = self._clock.now()
        record = replace(record, kyb_submitted_at=now, updated_at=now)
        self._store.update_record(record, conn=conn)
        rule = self._mcc_rules.get(record.primary_business_mcc or "")
        if rule is not None and rule.disposition == "ENHANCED_DUE_DILIGENCE":
            record, merchant = self._advance(
                record, merchant, "high_risk_mcc", actor_id=actor_id, conn=conn
            )
            approval = self._approvals.request(
                action_type="merchant_edd_clearance",
                subject_type="KybRecord",
                subject_id=record.kyb_record_id,
                payload={"merchant_id": record.subject_id, "mcc": record.primary_business_mcc},
                reason="high-risk MCC enhanced due diligence",
                initiator_id=actor_id,
            )
            record = replace(
                record,
                approval_request_id=approval.approval_request_id,
                updated_at=self._clock.now(),
            )
            self._store.update_record(record, conn=conn)
            self._emit(
                "merchant.edd_required",
                record.subject_id,
                {
                    "merchant_id": record.subject_id,
                    "mcc_code": record.primary_business_mcc,
                    "risk_tier": rule.risk_tier,
                },
                conn=conn,
            )
            return record
        record, _ = self._advance(record, merchant, "checks_pass", actor_id=actor_id, conn=conn)
        self._emit(
            "merchant.sanctions_review_started",
            record.subject_id,
            {
                "merchant_id": record.subject_id,
                "ubo_count": len(self._store.list_ubo_nodes(kyb_record_id, conn=conn)),
            },
            conn=conn,
        )
        return record

    def _submission_failures(
        self, record: KybRecord, *, conn: Any | None
    ) -> list[str]:
        failures: list[str] = []
        required = required_document_types(
            record.registration_type or "SOLE_PROPRIETORSHIP", record.requested_services
        )
        documents = self._store.list_documents(record.kyb_record_id, conn=conn)
        present = {
            d.document_type
            for d in documents
            if d.review_status in ("PENDING", "OCR_COMPLETE", "ACCEPTED")
        }
        if required - present:
            failures.append("all_required_docs_present")
        nodes = self._store.list_ubo_nodes(record.kyb_record_id, conn=conn)
        verified_ubo = any(
            n.is_ultimate_beneficial_owner
            and n.porichoy_status in ("VERIFIED", "MANUALLY_VERIFIED")
            for n in nodes
        )
        if not verified_ubo:
            failures.append("at_least_one_ubo_verified")
        if record.bank_routing_number is None:
            failures.append("bank_account_present")
        rule = self._mcc_rules.get(record.primary_business_mcc or "")
        if rule is not None and rule.disposition == "AUTO_REJECT":
            failures.append("mcc_not_prohibited")
        return failures

    def complete_edd(
        self, kyb_record_id: str, *, actor_id: str, conn: Any | None = None
    ) -> KybRecord:
        """Resolve the EDD branch from the CAMLCO approval decision."""
        record = self._require_record(kyb_record_id, conn=conn)
        merchant = self._require_merchant(record.subject_id, conn=conn)
        if record.approval_request_id is None:
            raise ConflictError("no EDD approval request on record", code="edd_not_requested")
        approval = self._approvals.get(record.approval_request_id)
        if approval is None:
            raise NotFoundError("EDD approval request not found")
        if approval.state == "APPROVED":
            record, _ = self._advance(
                record, merchant, "edd_complete", actor_id=actor_id, conn=conn
            )
            self._emit(
                "merchant.sanctions_review_started",
                record.subject_id,
                {
                    "merchant_id": record.subject_id,
                    "ubo_count": len(self._store.list_ubo_nodes(kyb_record_id, conn=conn)),
                },
                conn=conn,
            )
            return record
        if approval.state == "REJECTED":
            record, _ = self._advance(
                record,
                merchant,
                "edd_rejected",
                actor_id=actor_id,
                rejection_reason_code="EDD_REJECTED",
                conn=conn,
            )
            return record
        raise ConflictError(
            f"EDD approval is {approval.state}; decision pending", code="edd_pending"
        )

    def run_sanctions_review(
        self, kyb_record_id: str, *, actor_id: str, conn: Any | None = None
    ) -> KybRecord:
        """Screen the entity and every UBO via SanctionsPort (always sync)."""
        record = self._require_record(kyb_record_id, conn=conn)
        merchant = self._require_merchant(record.subject_id, conn=conn)
        if record.kyb_status != "SANCTIONS_REVIEW":
            raise ConflictError(
                "record is not in SANCTIONS_REVIEW", code="invalid_state_transition"
            )
        now = self._clock.now()
        any_hit = False
        entity_screen = self._sanctions.screen_entity(
            entity_name=record.legal_name or record.subject_id,
            entity_type="ENTITY",
            identifiers={"tin": record.tin_number or ""},
        )
        any_hit = entity_screen.hit
        for node in self._store.list_ubo_nodes(kyb_record_id, conn=conn):
            screen = self._sanctions.screen_entity(
                entity_name=node.full_name_en,
                entity_type="INDIVIDUAL" if node.node_type == "NATURAL_PERSON" else "ENTITY",
                identifiers={
                    "nid_hash": node.nid_number_hash or "",
                    "nationality": node.nationality,
                },
            )
            status = "HIT_UNREVIEWED" if screen.hit else "CLEAR"
            self._store.update_ubo_node(
                replace(
                    node,
                    sanctions_status=status,
                    sanctions_screened_at=now,
                    sanctions_hit_id=screen.hit_id,
                ),
                conn=conn,
            )
            any_hit = any_hit or screen.hit
        record = replace(record, sanctions_screened_at=now, updated_at=now)
        self._store.update_record(record, conn=conn)
        if any_hit:
            record, _ = self._advance(
                record,
                merchant,
                "sanctions_hit",
                actor_id=actor_id,
                rejection_reason_code="SANCTIONS_HIT",
                conn=conn,
            )
            return record
        record = replace(record, sanctions_cleared_at=now, updated_at=now)
        self._store.update_record(record, conn=conn)
        record, _ = self._advance(record, merchant, "sanctions_clear", actor_id=actor_id,
                                  conn=conn)
        self._emit(
            "kyb_record.completed",
            record.subject_id,
            {"merchant_id": record.subject_id, "kyb_record_id": kyb_record_id},
            conn=conn,
        )
        return record

    # ------------------------------------------------------------------
    # two-eyes activation (spec/08 §8.7 / §8.9)
    # ------------------------------------------------------------------

    def request_activation(
        self,
        kyb_record_id: str,
        *,
        initiator_id: str,
        risk_tier: str,
        mdr_basis_points: int,
        settlement_cycle_days: int = 5,
        rolling_reserve_pct: int = 0,
        conn: Any | None = None,
    ) -> KybRecord:
        record = self._require_record(kyb_record_id, conn=conn)
        if record.kyb_status != "RISK_ASSESSED":
            raise ConflictError(
                "activation can only be requested from RISK_ASSESSED",
                code="invalid_state_transition",
            )
        approval = self._approvals.request(
            action_type="merchant_activation",
            subject_type="KybRecord",
            subject_id=kyb_record_id,
            payload={
                "merchant_id": record.subject_id,
                "risk_tier": risk_tier,
                "mdr_basis_points": mdr_basis_points,
            },
            reason="merchant activation (two-eyes gate)",
            initiator_id=initiator_id,
        )
        now = self._clock.now()
        record = replace(
            record,
            risk_tier=risk_tier,
            mdr_basis_points=mdr_basis_points,
            settlement_cycle_days=settlement_cycle_days,
            rolling_reserve_pct=rolling_reserve_pct,
            approval_request_id=approval.approval_request_id,
            updated_at=now,
        )
        self._store.update_record(record, conn=conn)
        return record

    def decide_activation(
        self,
        kyb_record_id: str,
        *,
        approver_id: str,
        approve: bool,
        decision_reason: str | None = None,
        conn: Any | None = None,
    ) -> KybRecord:
        """Second pair of eyes decides; self-approval is denied by the gateway."""
        record = self._require_record(kyb_record_id, conn=conn)
        merchant = self._require_merchant(record.subject_id, conn=conn)
        if record.approval_request_id is None:
            raise ConflictError(
                "no activation approval request on record", code="activation_not_requested"
            )
        self._approvals.decide(
            record.approval_request_id,
            approver_id=approver_id,
            approve=approve,
            decision_reason=decision_reason,
        )
        if approve:
            record, _ = self._advance(
                record, merchant, "two_eyes_approved", actor_id=approver_id, conn=conn
            )
            now = self._clock.now()
            record = replace(record, agreement_requested_at=now, updated_at=now)
            self._store.update_record(record, conn=conn)
            self._emit(
                "merchant.agreement_pending",
                record.subject_id,
                {"merchant_id": record.subject_id, "agreement_version": "v1.0"},
                conn=conn,
            )
            return record
        record, _ = self._advance(
            record,
            merchant,
            "two_eyes_rejected",
            actor_id=approver_id,
            rejection_reason_code="TWO_EYES_REJECTED",
            conn=conn,
        )
        return record

    def sign_agreement(
        self,
        kyb_record_id: str,
        *,
        signer_name: str,
        signer_nid_last4: str,
        actor_id: str,
        conn: Any | None = None,
    ) -> tuple[KybRecord, ApiKeyIssued]:
        """Agreement acceptance — activates the merchant and issues an API key.

        The API-key secret is generated once, hash-stored, and the plaintext
        is returned to the caller exactly once (never persisted, never logged).

        When a ``GatewayKeyPort`` is wired, the returned ``key_id`` is the real
        gateway ``akey_*`` id (the row gateway bearer/HMAC auth resolves) and
        the plaintext is the live bcrypt bearer credential; otherwise the
        kernel ``apik_*`` audit id and an internal-secret plaintext are used.

        Atomicity: when conn=None we wrap the entire activation + ledger
        account creation + gateway-key issuance in a single KybStore
        transaction so that a ledger or gateway-key or outbox failure cannot
        leave the merchant ACTIVE without settlement accounts OR leave an
        orphan live gateway key.  The gateway-key insert is threaded onto the
        caller's connection so it commits-or-rolls-back with the KYB unit.
        """
        if conn is None:
            transaction = getattr(self._store, "transaction", None)
            if callable(transaction):
                with transaction() as tx_conn:
                    return self.sign_agreement(
                        kyb_record_id,
                        signer_name=signer_name,
                        signer_nid_last4=signer_nid_last4,
                        actor_id=actor_id,
                        conn=tx_conn,
                    )
        record = self._require_record(kyb_record_id, conn=conn)
        merchant = self._require_merchant(record.subject_id, conn=conn)
        if record.kyb_status != "AGREEMENT_PENDING":
            raise ConflictError(
                "agreement can only be signed from AGREEMENT_PENDING",
                code="invalid_state_transition",
            )
        nodes = self._store.list_ubo_nodes(kyb_record_id, conn=conn)
        if not any(n.nid_number_hash is not None for n in nodes):
            raise ConflictError(
                "signer cannot be cross-checked against UBO records",
                code="signer_not_identified",
            )
        now = self._clock.now()
        record, merchant = self._advance(
            record, merchant, "agreement_signed", actor_id=actor_id, conn=conn
        )
        interval = REFRESH_INTERVAL_MONTHS[record.risk_tier or "MEDIUM"]
        record = replace(
            record,
            activated_at=now,
            approved_services=record.requested_services,
            next_refresh_due_at=_add_months(now, interval),
            refresh_interval_months=interval,
            updated_at=now,
        )
        self._store.update_record(record, conn=conn)
        merchant = replace(
            merchant,
            activated_at=now,
            risk_tier=record.risk_tier,
            mdr_basis_points=record.mdr_basis_points,
            settlement_cycle_days=record.settlement_cycle_days,
            updated_at=now,
        )
        self._store.update_merchant(merchant, conn=conn)
        self._ledger.open_merchant_accounts(merchant.merchant_id, conn=conn)
        # -- API-key issuance -------------------------------------------------
        # When a GatewayKeyPort is wired (production path), mint a live bcrypt
        # gateway credential *inside this transaction* so the gateway api_keys
        # row commits-or-rolls-back atomically with the KYB activation.  The
        # returned gateway_key_id is the real akey_* id that gateway bearer/HMAC
        # auth resolves; it becomes the key_id carried in the KYB issuance audit
        # record and returned to the caller.  Threading conn keeps the insert on
        # the caller's transaction (no orphan live key on a later failure).
        if self._gateway_keys is not None:
            key_id, plaintext = self._gateway_keys.issue_live_key(
                record.subject_id,
                "KYB activation",
                scopes=(
                    "payment:write",
                    "payment:read",
                    "refund:write",
                    "refund:read",
                    "webhook:write",
                    "webhook:read",
                    "settlement:read",
                ),
                conn=conn,
            )
        else:
            # No gateway port wired (test doubles, sandbox path, or legacy
            # callers).  Fall back to the internal secret generator; the KYB
            # audit record is written but the key will NOT authenticate to the
            # gateway bcrypt store.
            plaintext = self._secret_generator()
            key_id = make_kernel_id(
                "apik", {"merchant_id": record.subject_id, "issued_at": now}
            )
        secret_hash = hashlib.sha256(plaintext.encode("utf-8")).hexdigest()
        issuance = ApiKeyIssuanceRecord(
            issuance_id=make_kernel_id(
                "apik",
                {"merchant_id": record.subject_id, "key_id": key_id, "kind": "issuance"},
            ),
            merchant_id=record.subject_id,
            key_id=key_id,
            secret_hash=secret_hash,
            issued_at=now,
            issued_by=actor_id,
        )
        self._store.insert_api_key_issuance(issuance, conn=conn)
        self._audit_event(
            "KYB_AGREEMENT_SIGNED",
            record,
            actor_id=actor_id,
            payload={
                "signer_name_present": bool(signer_name),
                "signer_nid_last4": signer_nid_last4[-4:],
                "key_id": key_id,
            },
            conn=conn,
        )
        self._emit(
            "merchant.activated",
            record.subject_id,
            {
                "merchant_id": record.subject_id,
                "risk_tier": record.risk_tier,
                "mdr_basis_points": record.mdr_basis_points,
                "settlement_cycle_days": record.settlement_cycle_days,
                "approved_services": list(record.approved_services),
            },
            conn=conn,
        )
        return record, ApiKeyIssued(
            key_id=key_id, plaintext_secret=plaintext, secret_hash=secret_hash
        )

    # ------------------------------------------------------------------
    # post-activation lifecycle
    # ------------------------------------------------------------------

    def suspend(
        self, kyb_record_id: str, *, reason: str, actor_id: str, conn: Any | None = None
    ) -> KybRecord:
        record = self._require_record(kyb_record_id, conn=conn)
        merchant = self._require_merchant(record.subject_id, conn=conn)
        record, merchant = self._advance(
            record, merchant, "aml_triggered", actor_id=actor_id, conn=conn
        )
        now = self._clock.now()
        record = replace(record, suspended_at=now, suspension_reason=reason, updated_at=now)
        self._store.update_record(record, conn=conn)
        self._emit(
            "merchant.suspended",
            record.subject_id,
            {"merchant_id": record.subject_id, "suspension_reason": reason},
            conn=conn,
        )
        return record

    def unsuspend(
        self, kyb_record_id: str, *, actor_id: str, conn: Any | None = None
    ) -> KybRecord:
        record = self._require_record(kyb_record_id, conn=conn)
        merchant = self._require_merchant(record.subject_id, conn=conn)
        record, _ = self._advance(record, merchant, "camlco_cleared", actor_id=actor_id,
                                  conn=conn)
        self._emit(
            "merchant.unsuspended",
            record.subject_id,
            {"merchant_id": record.subject_id},
            conn=conn,
        )
        return record

    def terminate(
        self,
        kyb_record_id: str,
        *,
        reason: str,
        actor_id: str,
        conn: Any | None = None,
    ) -> KybRecord:
        record = self._require_record(kyb_record_id, conn=conn)
        merchant = self._require_merchant(record.subject_id, conn=conn)
        trigger = "license_revoked" if record.kyb_status == "ACTIVE" else "camlco_terminated"
        record, merchant = self._advance(record, merchant, trigger, actor_id=actor_id, conn=conn)
        now = self._clock.now()
        record = replace(record, terminated_at=now, termination_reason=reason, updated_at=now)
        self._store.update_record(record, conn=conn)
        self._emit(
            "merchant.terminated",
            record.subject_id,
            {"merchant_id": record.subject_id, "termination_reason": reason},
            conn=conn,
        )
        return record

    def mark_refresh_due(
        self, kyb_record_id: str, *, actor_id: str, conn: Any | None = None
    ) -> KybRecord:
        record = self._require_record(kyb_record_id, conn=conn)
        merchant = self._require_merchant(record.subject_id, conn=conn)
        if record.next_refresh_due_at is None or self._clock.now() < record.next_refresh_due_at:
            raise ConflictError("KYB refresh is not due yet", code="refresh_not_due")
        record, _ = self._advance(record, merchant, "kyb_refresh_due", actor_id=actor_id,
                                  conn=conn)
        self._emit(
            "merchant.kyb_refresh_due",
            record.subject_id,
            {
                "merchant_id": record.subject_id,
                "last_refreshed_at": record.last_refreshed_at,
                "risk_tier": record.risk_tier,
            },
            conn=conn,
        )
        return record

    def complete_refresh(
        self, kyb_record_id: str, *, actor_id: str, conn: Any | None = None
    ) -> KybRecord:
        record = self._require_record(kyb_record_id, conn=conn)
        merchant = self._require_merchant(record.subject_id, conn=conn)
        record, _ = self._advance(record, merchant, "refresh_complete", actor_id=actor_id,
                                  conn=conn)
        now = self._clock.now()
        interval = REFRESH_INTERVAL_MONTHS[record.risk_tier or "MEDIUM"]
        record = replace(
            record,
            last_refreshed_at=now,
            next_refresh_due_at=_add_months(now, interval),
            updated_at=now,
        )
        self._store.update_record(record, conn=conn)
        self._emit(
            "merchant.kyb_refreshed",
            record.subject_id,
            {"merchant_id": record.subject_id, "new_risk_tier": record.risk_tier},
            conn=conn,
        )
        return record

    def sweep_expired(self, *, conn: Any | None = None) -> int:
        """Apply the 8-FSM-1 TTL table; returns the number of records moved."""
        now = self._clock.now()
        moved = 0
        for record in self._store.list_records(conn=conn):
            merchant = self._store.get_merchant(record.subject_id, conn=conn)
            if merchant is None:
                continue
            if (
                record.kyb_status == "APPLICATION_SUBMITTED"
                and now >= record.application_submitted_at + APPLICATION_TTL
            ):
                self._advance(
                    record,
                    merchant,
                    "auto_expire",
                    actor_id="system:kyb-ttl-sweep",
                    rejection_reason_code="APPLICATION_TIMEOUT",
                    conn=conn,
                )
            elif (
                record.kyb_status == "DOCUMENTS_PENDING"
                and now >= (record.last_document_request_at or record.updated_at)
                + DOCUMENTS_TTL
            ):
                self._advance(
                    record,
                    merchant,
                    "auto_expire",
                    actor_id="system:kyb-ttl-sweep",
                    rejection_reason_code="DOCUMENTS_NOT_PROVIDED",
                    conn=conn,
                )
            elif (
                record.kyb_status == "AGREEMENT_PENDING"
                and record.agreement_requested_at is not None
                and now >= record.agreement_requested_at + AGREEMENT_TTL
            ):
                self._advance(
                    record,
                    merchant,
                    "auto_expire",
                    actor_id="system:kyb-ttl-sweep",
                    rejection_reason_code="AGREEMENT_NOT_SIGNED",
                    conn=conn,
                )
            elif (
                record.kyb_status == "KYB_REFRESH_PENDING"
                and now >= record.updated_at + REFRESH_TTL
            ):
                self._advance(
                    record,
                    merchant,
                    "refresh_failed",
                    actor_id="system:kyb-ttl-sweep",
                    conn=conn,
                )
            else:
                continue
            moved += 1
        return moved

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _require_record(self, kyb_record_id: str, *, conn: Any | None) -> KybRecord:
        record = self._store.get_record(kyb_record_id, conn=conn)
        if record is None:
            raise NotFoundError(f"unknown KYB record {kyb_record_id}")
        return record

    def _require_merchant(self, merchant_id: str, *, conn: Any | None) -> MerchantRecord:
        merchant = self._store.get_merchant(merchant_id, conn=conn)
        if merchant is None:
            raise NotFoundError(f"unknown merchant {merchant_id}")
        return merchant

    def _advance(
        self,
        record: KybRecord,
        merchant: MerchantRecord,
        trigger: str,
        *,
        actor_id: str,
        rejection_reason_code: str | None = None,
        conn: Any | None,
        table: TransitionTable = MERCHANT_KYB_TABLE,
    ) -> tuple[KybRecord, MerchantRecord]:
        """Resolve the transition, then update record + merchant mirror + audit
        in the caller's transaction (spec/08 F-07: never two transactions)."""
        rule = table.resolve(record.kyb_status, trigger)
        now = self._clock.now()
        updates: dict[str, object] = {"kyb_status": rule.to_state, "updated_at": now}
        if rule.to_state == "REJECTED":
            if rejection_reason_code is not None and (
                rejection_reason_code not in REJECTION_REASON_CODES
            ):
                raise InvalidRequestError(
                    f"unknown rejection_reason_code {rejection_reason_code!r}"
                )
            updates["rejected_at"] = now
            updates["rejection_reason_code"] = rejection_reason_code or "OTHER"
            self._emit(
                "merchant.kyb_rejected",
                record.subject_id,
                {
                    "merchant_id": record.subject_id,
                    "rejection_reason_code": rejection_reason_code or "OTHER",
                },
                conn=conn,
            )
        record = replace(record, **updates)  # type: ignore[arg-type]
        self._store.update_record(record, conn=conn)
        payout_blocked = merchant.payout_blocked
        if rule.to_state in ("SUSPENDED", "TERMINATED"):
            payout_blocked = True
        elif rule.to_state == "ACTIVE":
            payout_blocked = False
        merchant = replace(
            merchant,
            kyb_status=rule.to_state,
            payout_blocked=payout_blocked,
            updated_at=now,
        )
        self._store.update_merchant(merchant, conn=conn)
        self._audit_event(
            "KYB_STATE_TRANSITION",
            record,
            actor_id=actor_id,
            payload={"trigger": trigger},
            from_state=rule.from_state,
            to_state=rule.to_state,
            conn=conn,
        )
        return record, merchant

    def _audit_event(
        self,
        event_type: str,
        record: KybRecord,
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
                subject_type="KybRecord",
                subject_id=record.kyb_record_id,
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
        merchant_id: str,
        payload: dict[str, object],
        *,
        conn: Any | None,
    ) -> None:
        event = build_event(
            event_type=event_type,
            subject_type="Merchant",
            subject_id=merchant_id,
            producer=PRODUCER,
            topic="kyc.events",
            payload=payload,
            occurred_at=self._clock.now(),
        )
        self._outbox.enqueue(event, conn=conn)

"""QrService — acquirer issuance, issuer scan-to-pay, lifecycle (spec/13).

Implements the service layer behind the spec/13 API surface:
issue static/dynamic, suspend/reactivate/revoke, resolve (§C steps 7-8),
pay (static-cap guard), merchant-event cascade, intent-event consumption,
TTL sweep, interop matrix run, and the operator dashboard counts.

The qr package never moves money: ``/pay`` creates (or returns) a
PaymentIntent through the injected port; kernel owns the lifecycle.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import Enum

from bdpay.platform.clock import Clock
from bdpay.platform.errors import (
    ConflictError,
    InvalidRequestError,
    LimitExceededError,
    NotFoundError,
)
from bdpay.qr.codec import (
    AdditionalData,
    LanguageTemplate,
    MerchantAccountInfo,
    ParsedPayload,
    QrEncodeRequest,
    QrType,
    encode_payload,
    parse_and_validate,
    payload_hash_of,
)
from bdpay.qr.config import (
    DYNAMIC_TTL_MAX_SECONDS,
    DYNAMIC_TTL_MIN_SECONDS,
    MAX_STATIC_QRS_PER_MERCHANT,
    RESOLUTION_TTL_SECONDS,
    STATIC_LIMIT_MINOR,
    BanglaQrConfig,
)
from bdpay.qr.crc import verify_crc
from bdpay.qr.errors import QrValidationError
from bdpay.qr.fsm import advance_dynamic_payload, advance_merchant_qr
from bdpay.qr.model import (
    DynamicPayloadState,
    InteropDirection,
    MerchantQr,
    MerchantQrState,
    QrInteropTestCase,
    QrPayload,
    QrScanResolution,
    ValidationResult,
    qr_make_id,
)
from bdpay.qr.ports import (
    AuditPort,
    EventPublisher,
    FeeQuote,
    FeeQuoter,
    IntentRecord,
    MerchantDirectory,
    PaymentIntentPort,
    ZeroFeeQuoter,
)
from bdpay.qr.render import content_sha256, render_kit_pdf, render_png, render_svg
from bdpay.qr.repository import QrRepository

__all__ = [
    "DashboardStats",
    "DynamicQrIssueResult",
    "InteropRunResult",
    "QrService",
    "RenderAsset",
    "ResolveResult",
    "StaticQrIssueResult",
]

#: GET /v1/qr-codes/{id}/<asset> — kind -> media type (spec/13 render contract).
RENDER_KINDS: dict[str, str] = {
    "png": "image/png",
    "svg": "image/svg+xml",
    "kit_pdf": "application/pdf",
}

_INTENT_PAYABLE_STATES = {"REQUIRES_PAYMENT_METHOD", "REQUIRES_CONFIRMATION"}


@dataclass(frozen=True)
class StaticQrIssueResult:
    merchant_qr_id: str
    state: str
    payload: str
    payload_hash: str  # "sha256:<hex>" API form
    static_limit_minor: int


@dataclass(frozen=True)
class DynamicQrIssueResult:
    payload_id: str
    payment_intent_id: str
    payload: str
    payload_hash: str
    amount_minor: int
    expires_at: datetime


@dataclass(frozen=True)
class ResolveResult:
    """The POST /v1/qr/resolve response body (spec/13 issuer side)."""

    resolution_id: str
    qr_type: str
    on_us: bool
    merchant_name: str  # Bengali when ID 64 carries it
    merchant_name_alt: str | None  # the ID 59 Latin form when 64 is present
    merchant_city: str
    mcc: str
    acquirer_scheme: str  # BANGLA_QR | CARD_SCHEME
    acquirer_id: str | None
    merchant_pan_ref: str | None
    amount_minor: int | None
    tip_indicator: str | None
    static_limit_minor: int | None
    fee_quote: FeeQuote
    expires_at: datetime  # resolution validity (scanned_at + 5 min)


@dataclass(frozen=True)
class RenderAsset:
    """One deterministic render (spec/13): bytes + media type + content hash."""

    content: bytes
    media_type: str
    content_sha256: str  # hex digest — the X-BDPay-Content-Sha256 header value


@dataclass(frozen=True)
class InteropRunResult:
    pass_count: int
    fail_count: int
    counterparty_class: str | None


@dataclass(frozen=True)
class DashboardStats:
    """GET /v1/qr/dashboard (spec/13 operator surface)."""

    static_qr_count: int
    payload_count: int
    scan_count: int
    scan_valid_count: int
    failure_breakdown: dict[str, int]


class QrService:
    def __init__(
        self,
        *,
        config: BanglaQrConfig,
        repository: QrRepository,
        merchants: MerchantDirectory,
        intents: PaymentIntentPort,
        audit: AuditPort,
        events: EventPublisher,
        clock: Clock,
        fee_quoter: FeeQuoter | None = None,
        scan_only: bool = False,
    ) -> None:
        self._config = config
        self._repo = repository
        self._merchants = merchants
        self._intents = intents
        self._audit = audit
        self._events = events
        self._clock = clock
        self._fees = fee_quoter if fee_quoter is not None else ZeroFeeQuoter()
        # Soft-launch posture (spec/13 ARCHITECTURE R2): when True, the
        # acquirer issuance surface refuses while issuer resolve/pay stays live.
        self._scan_only = scan_only

    # ------------------------------------------------------------------
    # Acquirer side
    # ------------------------------------------------------------------

    def issue_static(
        self,
        merchant_id: str,
        *,
        label: str,
        store_id: str | None = None,
        terminal_id: str | None = None,
    ) -> StaticQrIssueResult:
        """POST /v1/merchants/{id}/qr-codes — issue + activate a static QR."""
        self._refuse_if_scan_only()
        merchant = self._merchants.get(merchant_id)
        if merchant is None:
            raise NotFoundError(f"merchant {merchant_id} not found")
        if merchant.state != "ACTIVE" or not merchant.mcc:
            raise ConflictError(
                "merchant must be ACTIVE with an MCC to issue QR codes",
                code="merchant_not_active",
            )
        if not merchant.bangla_qr_allowed:
            raise ConflictError(
                "BANGLA_QR is not an allowed method for this merchant",
                code="qr_method_not_allowed",
            )
        if len(self._repo.list_merchant_qrs(merchant_id)) >= MAX_STATIC_QRS_PER_MERCHANT:
            raise LimitExceededError(
                f"merchant already has {MAX_STATIC_QRS_PER_MERCHANT} static QR codes",
                code="qr_count_limit",
            )
        if self._repo.find_merchant_qr_by_label(merchant_id, label) is not None:
            raise ConflictError(f"label {label!r} already used", code="qr_label_exists")

        now = self._clock.now()
        payload_string = encode_payload(
            QrEncodeRequest(
                qr_type=QrType.STATIC,
                merchant_account=self._merchant_account(merchant.merchant_pan),
                mcc=merchant.mcc,
                merchant_name=merchant.name,
                merchant_city=merchant.city,
                additional=AdditionalData(store_label=store_id, terminal_label=terminal_id),
                language=(
                    LanguageTemplate(merchant_name=merchant.name_bn, merchant_city=None)
                    if merchant.name_bn
                    else None
                ),
            )
        )
        # FSM activate guard: payload encoded + CRC verified (self-check).
        if not verify_crc(payload_string):
            raise ConflictError("encoded payload failed CRC self-check", code="qr_encode_failed")
        parsed = parse_and_validate(payload_string, self._config)
        if self._repo.get_payload_by_hash(parsed.payload_hash) is not None:
            # Content-addressed payloads are UNIQUE (spec/13 data model); an
            # identical store/terminal combination already has this exact QR.
            raise ConflictError(
                "an identical QR payload already exists for this merchant "
                "(same store/terminal content)",
                code="qr_payload_exists",
            )

        merchant_qr_id = qr_make_id(
            "mqr",
            {
                "merchant_id": merchant_id,
                "store_id": store_id,
                "terminal_id": terminal_id,
                "label": label,
            },
        )
        payload_row = self._new_payload_row(
            parsed,
            qr_type=QrType.STATIC,
            merchant_qr_id=merchant_qr_id,
            payment_intent_id=None,
            amount_minor=None,
            expires_at=None,
            created_at=now,
        )
        row = MerchantQr(
            merchant_qr_id=merchant_qr_id,
            merchant_id=merchant_id,
            state=advance_merchant_qr(MerchantQrState.DRAFT, "activate"),
            label=label,
            store_id=store_id,
            terminal_id=terminal_id,
            current_payload_id=payload_row.payload_id,
            suspend_reason=None,
            created_at=now,
            updated_at=now,
        )
        with self._repo.transaction():
            self._repo.insert_merchant_qr(row)
            self._repo.insert_payload(payload_row)
        self._audit.record(
            action="QR_ISSUED",
            subject_type="MerchantQr",
            subject_id=merchant_qr_id,
            occurred_at=now,
            details={"merchant_id": merchant_id, "payload_hash": payload_row.payload_hash},
        )
        self._publish(
            "merchant_qr.activated",
            "MerchantQr",
            merchant_qr_id,
            now,
            {"merchant_qr_id": merchant_qr_id, "merchant_id": merchant_id},
        )
        self._publish(
            "qr_payload.issued",
            "QrPayload",
            payload_row.payload_id,
            now,
            {
                "payload_id": payload_row.payload_id,
                "qr_type": "STATIC",
                "payload_hash": payload_row.payload_hash,
            },
        )
        return StaticQrIssueResult(
            merchant_qr_id=merchant_qr_id,
            state=row.state.value,
            payload=payload_string,
            payload_hash=f"sha256:{payload_row.payload_hash}",
            static_limit_minor=STATIC_LIMIT_MINOR,
        )

    def issue_dynamic(self, payment_intent_id: str, *, ttl_seconds: int) -> DynamicQrIssueResult:
        """POST /v1/qr-codes/dynamic — per-intent payload with TTL 60-900s.

        Amount/currency come from the intent, never from the request
        (single source of truth, spec/13).
        """
        self._refuse_if_scan_only()
        if (
            isinstance(ttl_seconds, bool)
            or not isinstance(ttl_seconds, int)
            or not DYNAMIC_TTL_MIN_SECONDS <= ttl_seconds <= DYNAMIC_TTL_MAX_SECONDS
        ):
            raise InvalidRequestError(
                f"ttl_seconds must be an integer in "
                f"{DYNAMIC_TTL_MIN_SECONDS}-{DYNAMIC_TTL_MAX_SECONDS}",
                code="qr_invalid_ttl",
            )
        intent = self._intents.get(payment_intent_id)
        if intent is None:
            raise NotFoundError(f"payment intent {payment_intent_id} not found")
        if intent.state not in _INTENT_PAYABLE_STATES:
            raise ConflictError(
                f"intent state {intent.state} cannot take a dynamic QR",
                code="intent_not_payable",
            )
        if self._repo.find_dynamic_by_intent(payment_intent_id) is not None:
            raise ConflictError(
                "a dynamic QR already exists for this intent (1:1 mapping)",
                code="qr_intent_already_has_payload",
            )
        merchant = self._merchants.get(intent.merchant_id)
        if merchant is None:
            raise NotFoundError(f"merchant {intent.merchant_id} not found")
        if merchant.state != "ACTIVE" or not merchant.mcc:
            raise ConflictError("merchant is not ACTIVE", code="merchant_not_active")
        if not merchant.bangla_qr_allowed:
            raise ConflictError(
                "BANGLA_QR is not an allowed method for this merchant",
                code="qr_method_not_allowed",
            )

        now = self._clock.now()
        expires_at = now + timedelta(seconds=ttl_seconds)
        payload_string = encode_payload(
            QrEncodeRequest(
                qr_type=QrType.DYNAMIC,
                merchant_account=self._merchant_account(merchant.merchant_pan),
                mcc=merchant.mcc,
                merchant_name=merchant.name,
                merchant_city=merchant.city,
                amount_minor=intent.amount_minor,
                additional=AdditionalData(reference_label=payment_intent_id),
                language=(
                    LanguageTemplate(merchant_name=merchant.name_bn, merchant_city=None)
                    if merchant.name_bn
                    else None
                ),
            )
        )
        parsed = parse_and_validate(payload_string, self._config)
        payload_row = self._new_payload_row(
            parsed,
            qr_type=QrType.DYNAMIC,
            merchant_qr_id=None,
            payment_intent_id=payment_intent_id,
            amount_minor=intent.amount_minor,
            expires_at=expires_at,
            created_at=now,
        )
        self._repo.insert_payload(payload_row)
        self._publish(
            "qr_payload.issued",
            "QrPayload",
            payload_row.payload_id,
            now,
            {
                "payload_id": payload_row.payload_id,
                "qr_type": "DYNAMIC",
                "payload_hash": payload_row.payload_hash,
                "amount_minor": intent.amount_minor,
            },
        )
        return DynamicQrIssueResult(
            payload_id=payload_row.payload_id,
            payment_intent_id=payment_intent_id,
            payload=payload_string,
            payload_hash=f"sha256:{payload_row.payload_hash}",
            amount_minor=intent.amount_minor,
            expires_at=expires_at,
        )

    # ------------------------------------------------------------------
    # MerchantQr lifecycle
    # ------------------------------------------------------------------

    def suspend(self, merchant_qr_id: str, *, reason: str) -> MerchantQr:
        return self._lifecycle(merchant_qr_id, "suspend", reason=reason)

    def reactivate(self, merchant_qr_id: str) -> MerchantQr:
        row = self._require_merchant_qr(merchant_qr_id)
        merchant = self._merchants.get(row.merchant_id)
        if merchant is None or merchant.state != "ACTIVE":
            raise ConflictError(
                "merchant must be ACTIVE to reactivate its QR",
                code="merchant_not_active",
            )
        return self._lifecycle(merchant_qr_id, "reactivate", reason=None)

    def revoke(self, merchant_qr_id: str, *, reason: str) -> MerchantQr:
        return self._lifecycle(merchant_qr_id, "revoke", reason=reason)

    def on_merchant_suspended(self, merchant_id: str) -> int:
        """Consume ``merchant.suspended`` — cascade-suspend ACTIVE QRs."""
        count = 0
        for row in self._repo.list_merchant_qrs(merchant_id):
            if row.state is MerchantQrState.ACTIVE:
                self._lifecycle(row.merchant_qr_id, "suspend", reason="merchant_suspended")
                count += 1
        return count

    def on_merchant_terminated(self, merchant_id: str) -> int:
        """Consume ``merchant.terminated`` — cascade-revoke all live QRs."""
        count = 0
        for row in self._repo.list_merchant_qrs(merchant_id):
            if row.state in (MerchantQrState.ACTIVE, MerchantQrState.SUSPENDED):
                self._lifecycle(row.merchant_qr_id, "revoke", reason="merchant_terminated")
                count += 1
        return count

    # ------------------------------------------------------------------
    # Read surface + deterministic renders (spec/13 GET /v1/qr-codes/{id})
    # ------------------------------------------------------------------

    def get_merchant_qr(self, merchant_qr_id: str) -> MerchantQr:
        """GET /v1/qr-codes/{merchant_qr_id} — metadata read."""
        return self._require_merchant_qr(merchant_qr_id)

    def list_merchant_qrs(self, merchant_id: str) -> list[MerchantQr]:
        """All static QRs for one merchant (acquirer console list)."""
        return self._repo.list_merchant_qrs(merchant_id)

    def get_payload_for_qr(self, merchant_qr_id: str) -> QrPayload:
        """The current payload row behind a static QR."""
        row = self._require_merchant_qr(merchant_qr_id)
        payload_row = (
            self._repo.get_payload(row.current_payload_id)
            if row.current_payload_id
            else None
        )
        if payload_row is None:  # repo invariant: every issued QR has a payload
            raise NotFoundError(
                f"qr {merchant_qr_id} has no payload", code="qr_payload_missing"
            )
        return payload_row

    def render_asset(self, merchant_qr_id: str, kind: str) -> RenderAsset:
        """Deterministic render (spec/13): pure function of the stored payload."""
        media_type = RENDER_KINDS.get(kind)
        if media_type is None:
            raise InvalidRequestError(
                f"unknown render kind {kind!r}; one of {sorted(RENDER_KINDS)}",
                code="qr_render_kind_unknown",
            )
        payload_row = self.get_payload_for_qr(merchant_qr_id)
        if kind == "png":
            content = render_png(payload_row.payload_string)
        elif kind == "svg":
            content = render_svg(payload_row.payload_string)
        else:
            content = render_kit_pdf(
                payload_row.payload_string,
                merchant_name=payload_row.merchant_name,
                merchant_name_bn=payload_row.merchant_name_bn or payload_row.merchant_name,
            )
        return RenderAsset(
            content=content,
            media_type=media_type,
            content_sha256=content_sha256(content),
        )

    # ------------------------------------------------------------------
    # Issuer side: resolve + pay (spec/13 §C steps 7-8)
    # ------------------------------------------------------------------

    def resolve(self, payload: str, *, customer_ref: str) -> ResolveResult:
        """POST /v1/qr/resolve — validate, record, quote.

        Every scan is persisted as a QrScanResolution, refusals included
        (spec/13 §C step 8 — fraud telemetry).
        """
        scanned_at = self._clock.now()
        try:
            parsed = parse_and_validate(payload, self._config)
        except QrValidationError as exc:
            self._record_scan(
                payload_hash=payload_hash_of(payload),
                payload_id=None,
                on_us=False,
                customer_ref=customer_ref,
                qr_type="UNKNOWN",
                result=ValidationResult.INVALID,
                failure_code=exc.code,
                quoted_fee_minor=None,
                scanned_at=scanned_at,
            )
            raise

        ours = self._repo.get_payload_by_hash(parsed.payload_hash)
        on_us = ours is not None
        try:
            if ours is not None:
                self._check_on_us(ours, scanned_at)
        except ConflictError as exc:
            self._record_scan(
                payload_hash=parsed.payload_hash,
                payload_id=ours.payload_id if ours else None,
                on_us=on_us,
                customer_ref=customer_ref,
                qr_type=parsed.qr_type.value,
                result=ValidationResult.INVALID,
                failure_code=exc.code,
                quoted_fee_minor=None,
                scanned_at=scanned_at,
            )
            raise

        if ours is not None and ours.qr_type is QrType.DYNAMIC:
            if ours.state is DynamicPayloadState.ISSUED:
                ours = replace(ours, state=advance_dynamic_payload(ours.state, "scanned"))
                self._repo.save_payload(ours)
            # Re-scan while SCANNED records a fresh resolution, no transition.

        quote = self._fees.quote(
            qr_type=parsed.qr_type.value, on_us=on_us, amount_minor=parsed.amount_minor
        )
        resolution = self._record_scan(
            payload_hash=parsed.payload_hash,
            payload_id=ours.payload_id if ours else None,
            on_us=on_us,
            customer_ref=customer_ref,
            qr_type=parsed.qr_type.value,
            result=ValidationResult.VALID,
            failure_code=None,
            quoted_fee_minor=quote.fee_minor,
            scanned_at=scanned_at,
        )
        bengali_name = parsed.language.merchant_name if parsed.language else None
        template = parsed.bangla_template
        return ResolveResult(
            resolution_id=resolution.resolution_id,
            qr_type=parsed.qr_type.value,
            on_us=on_us,
            merchant_name=bengali_name or parsed.merchant_name,
            merchant_name_alt=parsed.merchant_name if bengali_name else None,
            merchant_city=parsed.merchant_city,
            mcc=parsed.mcc,
            acquirer_scheme=parsed.route_class.value,
            acquirer_id=template.acquirer_id if template else None,
            merchant_pan_ref=template.merchant_id if template else None,
            amount_minor=parsed.amount_minor,
            tip_indicator=parsed.tip_indicator,
            static_limit_minor=(
                STATIC_LIMIT_MINOR if parsed.qr_type is QrType.STATIC else None
            ),
            fee_quote=quote,
            expires_at=scanned_at + timedelta(seconds=RESOLUTION_TTL_SECONDS),
        )

    def pay(self, resolution_id: str, *, amount_minor: int | None = None) -> IntentRecord:
        """POST /v1/qr/resolutions/{id}/pay — static cap guard, intent creation.

        Static: ``amount_minor`` is the customer-entered amount, capped at
        BDT 20,200 = 2,020,000 paisa. Dynamic: amount must be absent; for an
        on-us dynamic payload the linked intent already exists (1:1 mapping)
        and is returned rather than re-created.
        """
        now = self._clock.now()
        resolution = self._repo.get_resolution(resolution_id)
        if resolution is None:
            raise NotFoundError(f"resolution {resolution_id} not found")
        if resolution.validation_result is not ValidationResult.VALID:
            raise ConflictError("resolution was a refusal", code="qr_resolution_invalid")
        if now > resolution.scanned_at + timedelta(seconds=RESOLUTION_TTL_SECONDS):
            raise ConflictError(
                "resolution validity window (5 min) elapsed",
                code="qr_resolution_expired",
            )
        if resolution.payment_intent_id is not None:
            raise ConflictError("resolution already paid", code="qr_already_paid")

        payload_row = (
            self._repo.get_payload(resolution.payload_id) if resolution.payload_id else None
        )

        if resolution.qr_type == "DYNAMIC":
            if amount_minor is not None:
                raise InvalidRequestError(
                    "amount is fixed by the dynamic QR; omit amount_minor",
                    code="qr_amount_not_allowed",
                )
            if payload_row is not None:
                self._check_on_us(payload_row, now)
                intent = self._intents.get(payload_row.payment_intent_id or "")
                if intent is None:
                    raise ConflictError(
                        "linked intent no longer exists", code="qr_cancelled"
                    )
                self._repo.set_resolution_intent(resolution_id, intent.payment_intent_id)
                return intent
            raise ConflictError(
                "off-us dynamic payment requires NPSB routing context not yet "
                "configured (BB GUID/sub-ID pending)",
                code="qr_no_routable_template",
            )

        # STATIC: customer-entered amount, validated.
        if isinstance(amount_minor, bool) or not isinstance(amount_minor, int):
            raise InvalidRequestError(
                "amount_minor must be an integer paisa amount (floats rejected)",
                code="qr_amount_required",
            )
        if amount_minor <= 0:
            raise InvalidRequestError(
                "amount_minor must be positive", code="qr_amount_required"
            )
        if amount_minor > STATIC_LIMIT_MINOR:
            raise LimitExceededError(
                f"static QR cap is {STATIC_LIMIT_MINOR} paisa (BDT 20,200)",
                code="qr_static_over_limit",
            )
        merchant_id = ""
        if payload_row is not None and payload_row.merchant_qr_id is not None:
            merchant_qr = self._require_merchant_qr(payload_row.merchant_qr_id)
            self._check_merchant_qr_live(merchant_qr)
            merchant_id = merchant_qr.merchant_id
        intent = self._intents.create(
            merchant_id=merchant_id,
            amount_minor=amount_minor,
            method="BANGLA_QR",
            metadata={
                "qr_payload_hash": resolution.payload_hash,
                "qr_type": resolution.qr_type,
                "resolution_id": resolution_id,
            },
        )
        self._repo.set_resolution_intent(resolution_id, intent.payment_intent_id)
        self._audit.record(
            action="QR_PAY_INITIATED",
            subject_type="QrScanResolution",
            subject_id=resolution_id,
            occurred_at=now,
            details={"payment_intent_id": intent.payment_intent_id},
        )
        return intent

    # ------------------------------------------------------------------
    # Event consumption + sweeps
    # ------------------------------------------------------------------

    def on_intent_event(self, payment_intent_id: str, outcome: str) -> None:
        """Consume payment.events — advance the linked dynamic payload FSM.

        ``outcome``: succeeded -> paid; failed/cancelled -> cancelled.
        """
        trigger = {"succeeded": "paid", "failed": "cancelled", "cancelled": "cancelled"}.get(
            outcome
        )
        if trigger is None:
            raise InvalidRequestError(f"unknown intent outcome {outcome!r}")
        row = self._repo.find_dynamic_by_intent(payment_intent_id)
        if row is None or row.state not in (
            DynamicPayloadState.ISSUED,
            DynamicPayloadState.SCANNED,
        ):
            return  # idempotent: nothing live to advance
        new_state = advance_dynamic_payload(row.state, trigger)
        self._repo.save_payload(replace(row, state=new_state))
        event = "qr_payload.paid" if trigger == "paid" else "qr_payload.cancelled"
        self._publish(
            event,
            "QrPayload",
            row.payload_id,
            self._clock.now(),
            {"payload_id": row.payload_id, "payment_intent_id": payment_intent_id},
        )

    def sweep_expired(self) -> int:
        """TTL sweep (30s cadence per spec/13 FSM 2; injectable clock)."""
        now = self._clock.now()
        count = 0
        for row in self._repo.list_expirable(now):
            new_state = advance_dynamic_payload(row.state, "ttl_expired")
            self._repo.save_payload(replace(row, state=new_state))
            self._publish(
                "qr_payload.expired",
                "QrPayload",
                row.payload_id,
                now,
                {"payload_id": row.payload_id, "payment_intent_id": row.payment_intent_id},
            )
            count += 1
        return count

    # ------------------------------------------------------------------
    # Interop matrix + dashboard (operator surface)
    # ------------------------------------------------------------------

    def load_interop_cases(self, cases: list[QrInteropTestCase]) -> None:
        for case in cases:
            self._repo.upsert_interop_case(case)

    def list_interop_cases(
        self, counterparty_class: str | None = None
    ) -> list[QrInteropTestCase]:
        """GET /v1/qr/interop-tests — the matrix status read."""
        return self._repo.list_interop_cases(counterparty_class)

    def run_interop(self, *, counterparty_class: str | None = None) -> InteropRunResult:
        """POST /v1/qr/interop-tests/run — execute WE_SCAN golden cases."""
        now = self._clock.now()
        pass_count = 0
        fail_count = 0
        for case in self._repo.list_interop_cases(counterparty_class):
            if case.direction is not InteropDirection.WE_SCAN:
                continue
            outcome = self._execute_interop_case(case)
            if outcome:
                pass_count += 1
            else:
                fail_count += 1
            self._repo.upsert_interop_case(
                replace(case, last_result="PASS" if outcome else "FAIL", last_run_at=now)
            )
        self._publish(
            "qr_interop.run_completed",
            "QrInteropTestCase",
            counterparty_class or "ALL",
            now,
            {
                "pass_count": pass_count,
                "fail_count": fail_count,
                "counterparty_class": counterparty_class,
            },
        )
        return InteropRunResult(pass_count, fail_count, counterparty_class)

    def _execute_interop_case(self, case: QrInteropTestCase) -> bool:
        expected = case.expected
        try:
            parsed = parse_and_validate(case.payload_string, self._config)
        except QrValidationError as exc:
            return not expected.get("valid", True) and exc.code == expected.get("failure_code")
        if not expected.get("valid", False):
            return False
        for field, value in expected.get("fields", {}).items():
            actual = getattr(parsed, field, None)
            if isinstance(actual, Enum):
                actual = actual.value
            if actual != value:
                return False
        return True

    def dashboard(self) -> DashboardStats:
        resolutions = self._repo.list_resolutions()
        breakdown: dict[str, int] = {}
        valid = 0
        for row in resolutions:
            if row.validation_result is ValidationResult.VALID:
                valid += 1
            elif row.failure_code:
                breakdown[row.failure_code] = breakdown.get(row.failure_code, 0) + 1
        static_count = sum(
            1
            for row in self._repo.list_resolutions()
            if row.qr_type == "STATIC" and row.on_us
        )
        return DashboardStats(
            static_qr_count=static_count,
            payload_count=len({r.payload_id for r in resolutions if r.payload_id}),
            scan_count=len(resolutions),
            scan_valid_count=valid,
            failure_breakdown=breakdown,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _refuse_if_scan_only(self) -> None:
        if self._scan_only:
            raise ConflictError(
                "platform is in scan-only soft-launch mode (QR_SCAN_ONLY); "
                "QR issuance is disabled — issuer scan-to-pay remains live",
                code="qr_scan_only_mode",
            )

    def _merchant_account(self, merchant_pan: str) -> MerchantAccountInfo:
        return MerchantAccountInfo(
            root_id=self._config.template_root_id,
            guid=self._config.scheme_guid,
            acquirer_id=self._config.acquirer_id,
            merchant_id=merchant_pan,
            psp_sub_id=self._config.psp_sub_id,
        )

    def _new_payload_row(
        self,
        parsed: ParsedPayload,
        *,
        qr_type: QrType,
        merchant_qr_id: str | None,
        payment_intent_id: str | None,
        amount_minor: int | None,
        expires_at: datetime | None,
        created_at: datetime,
    ) -> QrPayload:
        return QrPayload(
            payload_id=qr_make_id("qrp", {"payload_string": parsed.payload_string}),
            qr_type=qr_type,
            merchant_qr_id=merchant_qr_id,
            payment_intent_id=payment_intent_id,
            state=DynamicPayloadState.ISSUED,
            payload_string=parsed.payload_string,
            payload_hash=parsed.payload_hash,
            amount_minor=amount_minor,
            currency="BDT",
            mcc=parsed.mcc,
            merchant_name=parsed.merchant_name,
            merchant_name_bn=parsed.language.merchant_name if parsed.language else None,
            merchant_city=parsed.merchant_city,
            expires_at=expires_at,
            created_at=created_at,
        )

    def _require_merchant_qr(self, merchant_qr_id: str) -> MerchantQr:
        row = self._repo.get_merchant_qr(merchant_qr_id)
        if row is None:
            raise NotFoundError(f"merchant qr {merchant_qr_id} not found")
        return row

    def _lifecycle(self, merchant_qr_id: str, trigger: str, *, reason: str | None) -> MerchantQr:
        row = self._require_merchant_qr(merchant_qr_id)
        now = self._clock.now()
        new_state = advance_merchant_qr(row.state, trigger)
        row = replace(
            row,
            state=new_state,
            suspend_reason=reason if trigger in ("suspend", "revoke") else None,
            updated_at=now,
        )
        self._repo.save_merchant_qr(row)
        event_by_trigger = {
            "suspend": "merchant_qr.suspended",
            "reactivate": "merchant_qr.activated",
            "revoke": "merchant_qr.revoked",
            "activate": "merchant_qr.activated",
        }
        payload: dict = {"merchant_qr_id": merchant_qr_id, "merchant_id": row.merchant_id}
        if reason:
            payload["reason"] = reason
        self._audit.record(
            action=f"QR_{trigger.upper()}",
            subject_type="MerchantQr",
            subject_id=merchant_qr_id,
            occurred_at=now,
            details=payload,
        )
        self._publish(event_by_trigger[trigger], "MerchantQr", merchant_qr_id, now, payload)
        return row

    def _check_merchant_qr_live(self, merchant_qr: MerchantQr) -> None:
        if merchant_qr.state is MerchantQrState.REVOKED:
            raise ConflictError("QR has been revoked", code="qr_revoked")
        if merchant_qr.state is not MerchantQrState.ACTIVE:
            raise ConflictError("QR is suspended", code="qr_merchant_suspended")
        merchant = self._merchants.get(merchant_qr.merchant_id)
        if merchant is None or merchant.state != "ACTIVE":
            raise ConflictError("merchant is not ACTIVE", code="qr_merchant_suspended")

    def _check_on_us(self, ours: QrPayload, now: datetime) -> None:
        """spec/13 §C step 7 — on-us payload liveness checks."""
        if ours.qr_type is QrType.STATIC:
            if ours.merchant_qr_id is not None:
                self._check_merchant_qr_live(self._require_merchant_qr(ours.merchant_qr_id))
            return
        # Dynamic payload: FSM + TTL.
        if ours.state is DynamicPayloadState.PAID:
            raise ConflictError("dynamic QR already paid", code="qr_already_paid")
        if ours.state is DynamicPayloadState.EXPIRED:
            raise ConflictError("dynamic QR expired", code="qr_expired")
        if ours.state is DynamicPayloadState.CANCELLED:
            raise ConflictError("dynamic QR cancelled", code="qr_cancelled")
        if ours.expires_at is not None and now > ours.expires_at:
            new_state = advance_dynamic_payload(ours.state, "ttl_expired")
            self._repo.save_payload(replace(ours, state=new_state))
            self._publish(
                "qr_payload.expired",
                "QrPayload",
                ours.payload_id,
                now,
                {"payload_id": ours.payload_id, "payment_intent_id": ours.payment_intent_id},
            )
            raise ConflictError("dynamic QR expired", code="qr_expired")

    def _record_scan(
        self,
        *,
        payload_hash: str,
        payload_id: str | None,
        on_us: bool,
        customer_ref: str,
        qr_type: str,
        result: ValidationResult,
        failure_code: str | None,
        quoted_fee_minor: int | None,
        scanned_at: datetime,
    ) -> QrScanResolution:
        resolution = QrScanResolution(
            resolution_id=qr_make_id(
                "qres",
                {
                    "payload_hash": payload_hash,
                    "customer_ref": customer_ref,
                    "scanned_at": scanned_at,
                },
            ),
            payload_hash=payload_hash,
            payload_id=payload_id,
            on_us=on_us,
            customer_ref=customer_ref,
            qr_type=qr_type,
            validation_result=result,
            failure_code=failure_code,
            quoted_fee_minor=quoted_fee_minor,
            payment_intent_id=None,
            scanned_at=scanned_at,
        )
        self._repo.insert_resolution(resolution)
        self._publish(
            "qr_payload.scanned",
            "QrScanResolution",
            resolution.resolution_id,
            scanned_at,
            {
                "resolution_id": resolution.resolution_id,
                "payload_hash": payload_hash,
                "on_us": on_us,
                "validation_result": result.value,
                **({"failure_code": failure_code} if failure_code else {}),
            },
        )
        return resolution

    def _publish(
        self,
        event_type: str,
        subject_type: str,
        subject_id: str,
        occurred_at: datetime,
        payload: dict,
    ) -> None:
        self._events.publish(
            event_type=event_type,
            subject_type=subject_type,
            subject_id=subject_id,
            occurred_at=occurred_at,
            payload=payload,
        )

"""NotificationService — template registry (EN/BN), durable queue, retries.

Implements the spec/01 §J dispatch surface and §State machines /
"Notification Dispatch FSM" (refusal-first; terminal states never move)::

    -        --[dispatch_requested / template+recipient valid]--> QUEUED
    QUEUED   --[worker picks up]----------------------------> SENDING
    SENDING  --[connector success]--------------------------> DELIVERED (terminal)
    SENDING  --[connector failure / attempts < 3]-----------> QUEUED (+60s delay)
    SENDING  --[connector failure / attempts >= 3]----------> FAILED (terminal)

Delivery is routed through the
:class:`~bdpay.platform.interfaces.NotificationPort` transport (the SMS/email
connector from spec/12 is supplied later; tests use a recording fake). The
transport receives an opaque ``recipient_ref`` entity id — never a raw phone
number or email — and a deterministic ``connector_ref`` per attempt so the
connector's own idempotency holds across retries.

PII fails closed at intake: template variables whose VALUES match the
Bangladesh PII patterns (mobile, NID, PAN, email, long account runs) are
rejected before anything is queued — an identifier that never enters the
queue can never leak into a log or a message body.

ID note (errata row PR-3 in SPEC_ERRATA-LANE-A-platform-runtime.md): spec/01
assigns prefix ``ndsp`` to NotificationDispatch, but ``ndsp`` is not in the
spec/00 §3 table nor in the E18 fold-in list, so the frozen ``make_id``
registry rejects it. ``_make_dispatch_id`` below is a format-parity helper
(same ``<prefix>_<sha256_canonical[:24]>`` scheme) that collapses to a plain
``make_id`` call at the next prefix fold-in.

Storage: :class:`NotificationStore` protocol + in-memory implementation. The
durable Postgres table (``notification_dispatches``) is declared in the
gateway schema (spec/01 data model) and ships with the gateway lane's
migrations; this module's store protocol is what that implementation plugs
into.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Protocol, runtime_checkable

from bdpay.platform.bangla import normalize_bengali_digits
from bdpay.platform.canonical import sha256_canonical
from bdpay.platform.clock import Clock
from bdpay.platform.interfaces import NotificationPort
from bdpay.platform.pii import redact

__all__ = [
    "CHANNELS",
    "DEFAULT_TEMPLATES",
    "LANGUAGES",
    "InMemoryNotificationStore",
    "NotificationDispatch",
    "NotificationError",
    "NotificationService",
    "NotificationStore",
    "NotificationTemplate",
    "RecordingNotificationPort",
    "TemplateRegistry",
]

CHANNELS = ("sms", "email", "push")
LANGUAGES = ("en", "bn")
RECIPIENT_TYPES = ("customer", "merchant", "operator")

MAX_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 60


class NotificationError(ValueError):
    """Raised on any refused notification operation (fails closed)."""


def _make_dispatch_id(payload: Mapping[str, object]) -> str:
    """Format-parity dispatch id: ``ndsp_<sha256_canonical(payload)[:24]>``.

    See the module docstring / errata row PR-3 — same content-addressed
    scheme as ``make_id``, pending the ``ndsp`` prefix fold-in.
    """
    return f"ndsp_{sha256_canonical(dict(payload))[:24]}"


@dataclass(frozen=True)
class NotificationTemplate:
    """A bilingual message template with a closed variable set."""

    template_id: str
    body_en: str
    body_bn: str
    required_vars: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.template_id:
            raise NotificationError("template_id must be non-empty")
        if not self.body_en or not self.body_bn:
            raise NotificationError(
                f"template {self.template_id!r} needs both EN and BN bodies "
                "(spec/15 rule: no late translation passes)"
            )

    def validate_vars(self, variables: Mapping[str, str]) -> None:
        """Exact-set variable check; non-string or PII-bearing values refused."""
        provided = set(variables)
        required = set(self.required_vars)
        if provided != required:
            missing = sorted(required - provided)
            extra = sorted(provided - required)
            raise NotificationError(
                f"template {self.template_id!r} variable mismatch: "
                f"missing={missing} unexpected={extra}"
            )
        for key, value in variables.items():
            if not isinstance(value, str):
                raise NotificationError(
                    f"template var {key!r} must be str, got {type(value).__name__}"
                )
            # Compare against the digit-normalized form so Bengali numerals in
            # legitimate display strings do not trip the check, while PII
            # written in Bengali digits is still caught.
            normalized = normalize_bengali_digits(value)
            if redact(normalized) != normalized:
                raise NotificationError(
                    f"template var {key!r} carries a redactable identifier; "
                    "PII never enters the notification queue (fails closed)"
                )

    def render(self, language: str, variables: Mapping[str, str]) -> str:
        """Render the body in ``language`` after validating the variable set."""
        if language not in LANGUAGES:
            raise NotificationError(
                f"unsupported language {language!r}; supported: {LANGUAGES}"
            )
        self.validate_vars(variables)
        body = self.body_en if language == "en" else self.body_bn
        try:
            return body.format(**dict(variables))
        except (KeyError, IndexError, ValueError) as exc:
            raise NotificationError(
                f"template {self.template_id!r} failed to render: {exc}"
            ) from exc


#: Seed templates for the spec/01 §J template_id enum (config data — product
#: copy is finalized by ops; structure and variable sets are the contract).
DEFAULT_TEMPLATES: tuple[NotificationTemplate, ...] = (
    NotificationTemplate(
        "payment_succeeded",
        body_en="Your payment of BDT {amount_bdt} to {merchant_name} succeeded.",
        body_bn="{merchant_name}-কে আপনার {amount_bdt} টাকার পেমেন্ট সফল হয়েছে।",
        required_vars=("amount_bdt", "merchant_name"),
    ),
    NotificationTemplate(
        "payment_received",
        body_en="Payment of BDT {amount_bdt} received.",
        body_bn="{amount_bdt} টাকার পেমেন্ট গৃহীত হয়েছে।",
        required_vars=("amount_bdt",),
    ),
    NotificationTemplate(
        "payment_failed",
        body_en="Your payment of BDT {amount_bdt} to {merchant_name} failed.",
        body_bn="{merchant_name}-কে আপনার {amount_bdt} টাকার পেমেন্ট ব্যর্থ হয়েছে।",
        required_vars=("amount_bdt", "merchant_name"),
    ),
    NotificationTemplate(
        "otp_challenge",
        body_en="Your verification code is {otp_code}. Valid for 5 minutes.",
        body_bn="আপনার যাচাইকরণ কোড {otp_code}। ৫ মিনিটের জন্য বৈধ।",
        required_vars=("otp_code",),
    ),
    NotificationTemplate(
        "kyc_approved",
        body_en="Your identity verification is complete. Tier: {tier}.",
        body_bn="আপনার পরিচয় যাচাই সম্পন্ন হয়েছে। স্তর: {tier}।",
        required_vars=("tier",),
    ),
    NotificationTemplate(
        "kyc_rejected",
        body_en="Your identity verification was unsuccessful. Reason code: {reason_code}.",
        body_bn="আপনার পরিচয় যাচাই সফল হয়নি। কারণ কোড: {reason_code}।",
        required_vars=("reason_code",),
    ),
    NotificationTemplate(
        "settlement_complete",
        body_en="Settlement of BDT {amount_bdt} for batch {batch_ref} is complete.",
        body_bn="ব্যাচ {batch_ref}-এর {amount_bdt} টাকার নিষ্পত্তি সম্পন্ন হয়েছে।",
        required_vars=("amount_bdt", "batch_ref"),
    ),
    NotificationTemplate(
        "tcsa_shortfall_alert",
        body_en=(
            "URGENT: TCSA shortfall detected. Coverage gap BDT {gap_bdt}. "
            "Immediate treasury action required."
        ),
        body_bn=(
            "জরুরি: TCSA ঘাটতি শনাক্ত হয়েছে। কভারেজ ঘাটতি {gap_bdt} টাকা। "
            "অবিলম্বে ট্রেজারি পদক্ষেপ প্রয়োজন।"
        ),
        required_vars=("gap_bdt",),
    ),
    # -- spec/16 LR-5 month-one templates (consumer-extension table) ---------
    NotificationTemplate(
        "payment_deferred",
        body_en=(
            "Your payment is queued and will be processed at {scheduled_for} "
            "(Dhaka time). No action is needed."
        ),
        body_bn=(
            "আপনার পেমেন্ট সারিতে রয়েছে এবং {scheduled_for}-এ (ঢাকা সময়) "
            "প্রক্রিয়া করা হবে। কোনো পদক্ষেপের প্রয়োজন নেই।"
        ),
        required_vars=("scheduled_for",),
    ),
    NotificationTemplate(
        "refund_failed",
        body_en=(
            "Your refund could not be completed automatically. Our team will "
            "resolve it within 5 business days."
        ),
        body_bn=(
            "আপনার রিফান্ড স্বয়ংক্রিয়ভাবে সম্পন্ন করা যায়নি। আমাদের দল ৫ "
            "কর্মদিবসের মধ্যে এটি সমাধান করবে।"
        ),
        required_vars=(),
    ),
    NotificationTemplate(
        "dispute_opened",
        body_en=(
            "A dispute has been opened on your payment. We will keep you "
            "updated; resolution follows the 30-day dispute timeline."
        ),
        body_bn=(
            "আপনার পেমেন্টের উপর একটি বিরোধ খোলা হয়েছে। আমরা আপনাকে আপডেট "
            "জানাব; ৩০ দিনের বিরোধ সময়সীমা অনুযায়ী সমাধান হবে।"
        ),
        required_vars=(),
    ),
    NotificationTemplate(
        "dispute_resolved",
        body_en="Your payment dispute has been resolved. Outcome: {resolution}.",
        body_bn="আপনার পেমেন্ট বিরোধের সমাধান হয়েছে। ফলাফল: {resolution}।",
        required_vars=("resolution",),
    ),
    # Merchant email, dispatched with language="en" (spec/16: the webhook
    # channel itself is dead — email is the carrier). A Bengali body is still
    # registered because the template registry's no-late-translation invariant
    # requires both bodies (errata E-S16-10).
    NotificationTemplate(
        "webhook_exhausted",
        body_en=(
            "Webhook delivery for event type {event_type} was exhausted after "
            "{attempt_count} attempts. Deliveries to your endpoint are failing. "
            "Recover missed events via GET /v1/webhook-deliveries and repair "
            "your endpoint, then re-enable delivery from the developer portal."
        ),
        body_bn=(
            "ইভেন্ট টাইপ {event_type}-এর ওয়েবহুক ডেলিভারি {attempt_count} বার "
            "চেষ্টার পর নিঃশেষ হয়েছে। আপনার এন্ডপয়েন্টে ডেলিভারি ব্যর্থ হচ্ছে। "
            "GET /v1/webhook-deliveries দিয়ে মিস হওয়া ইভেন্ট পুনরুদ্ধার করুন এবং "
            "এন্ডপয়েন্ট ঠিক করে ডেভেলপার পোর্টাল থেকে ডেলিভারি পুনরায় চালু করুন।"
        ),
        required_vars=("event_type", "attempt_count"),
    ),
)


class TemplateRegistry:
    """Closed template registry; unknown template ids are refused."""

    def __init__(
        self, templates: tuple[NotificationTemplate, ...] = DEFAULT_TEMPLATES
    ) -> None:
        self._templates: dict[str, NotificationTemplate] = {}
        for template in templates:
            self.register(template)

    def register(self, template: NotificationTemplate) -> None:
        if template.template_id in self._templates:
            raise NotificationError(
                f"template {template.template_id!r} is already registered"
            )
        self._templates[template.template_id] = template

    def get(self, template_id: str) -> NotificationTemplate:
        template = self._templates.get(template_id)
        if template is None:
            raise NotificationError(
                f"unknown template {template_id!r}; the registry is closed (fails closed)"
            )
        return template

    def template_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._templates))


@dataclass(frozen=True)
class NotificationDispatch:
    """One dispatch record (mirrors spec/01 ``notification_dispatches``).

    The DB row stores ``template_vars_hash`` only; the raw variables ride the
    in-process record so the body renders at send time and never persists.
    ``next_attempt_at`` is the queue-scheduling instant for retries.
    """

    dispatch_id: str
    idempotency_key: str
    recipient_type: str
    recipient_id: str
    channel: str
    template_id: str
    template_vars: Mapping[str, str]
    template_vars_hash: str
    language: str
    status: str  # QUEUED | SENDING | DELIVERED | FAILED
    attempt_count: int
    created_at: datetime
    updated_at: datetime
    next_attempt_at: datetime | None = None
    connector_ref: str | None = None
    connector_message_id: str | None = None
    delivered_at: datetime | None = None
    failed_reason: str | None = None
    schema_version: int = 1


@runtime_checkable
class NotificationStore(Protocol):
    """Storage contract for the dispatch queue."""

    def insert(self, record: NotificationDispatch) -> None: ...

    def save(self, record: NotificationDispatch) -> None: ...

    def get(self, dispatch_id: str) -> NotificationDispatch | None: ...

    def by_idempotency_key(self, idempotency_key: str) -> NotificationDispatch | None: ...

    def due(self, *, now: datetime, limit: int) -> list[NotificationDispatch]: ...


class InMemoryNotificationStore:
    """Deterministic in-memory queue store for unit tests."""

    def __init__(self) -> None:
        self._rows: dict[str, NotificationDispatch] = {}
        self._order: list[str] = []

    def insert(self, record: NotificationDispatch) -> None:
        if record.dispatch_id in self._rows:
            raise NotificationError(f"dispatch {record.dispatch_id!r} already exists")
        self._rows[record.dispatch_id] = record
        self._order.append(record.dispatch_id)

    def save(self, record: NotificationDispatch) -> None:
        if record.dispatch_id not in self._rows:
            raise NotificationError(f"unknown dispatch {record.dispatch_id!r}")
        self._rows[record.dispatch_id] = record

    def get(self, dispatch_id: str) -> NotificationDispatch | None:
        return self._rows.get(dispatch_id)

    def by_idempotency_key(self, idempotency_key: str) -> NotificationDispatch | None:
        for did in self._order:
            if self._rows[did].idempotency_key == idempotency_key:
                return self._rows[did]
        return None

    def due(self, *, now: datetime, limit: int) -> list[NotificationDispatch]:
        out: list[NotificationDispatch] = []
        for did in self._order:
            row = self._rows[did]
            if (
                row.status == "QUEUED"
                and row.next_attempt_at is not None
                and row.next_attempt_at <= now
            ):
                out.append(row)
            if len(out) >= limit:
                break
        return out


class RecordingNotificationPort:
    """Recording NotificationPort fake for tests.

    Records every send in order; ``fail_next`` is a count-down of attempts to
    fail before succeeding, so retry paths run deterministically.
    """

    def __init__(self, *, fail_next: int = 0) -> None:
        self.sent: list[dict[str, str]] = []
        self.fail_next = fail_next

    async def send(
        self,
        *,
        channel: str,
        recipient_ref: str,
        template_id: str,
        body: str,
        connector_ref: str,
    ):  # -> NotificationDeliveryResult
        from bdpay.platform.interfaces import NotificationDeliveryResult

        record = {
            "channel": channel,
            "recipient_ref": recipient_ref,
            "template_id": template_id,
            "body": body,
            "connector_ref": connector_ref,
        }
        self.sent.append(record)
        if self.fail_next > 0:
            self.fail_next -= 1
            return NotificationDeliveryResult(
                delivered=False, error_code="connector_unavailable"
            )
        return NotificationDeliveryResult(
            delivered=True, provider_message_id=f"prov-{len(self.sent)}"
        )


class NotificationService:
    """Routes dispatch requests through the queue + NotificationPort.

    Idempotent on the client-supplied ``idempotency_key``: a replay with
    identical parameters returns the existing record; a replay with different
    parameters is refused (the spec/00 §4 mismatched-replay rule).
    """

    def __init__(
        self,
        store: NotificationStore,
        registry: TemplateRegistry,
        port: NotificationPort,
        *,
        clock: Clock,
        max_attempts: int = MAX_ATTEMPTS,
        retry_delay_seconds: int = RETRY_DELAY_SECONDS,
        logger: logging.Logger | None = None,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if retry_delay_seconds < 0:
            raise ValueError("retry_delay_seconds must be >= 0")
        self._store = store
        self._registry = registry
        self._port = port
        self._clock = clock
        self._max_attempts = max_attempts
        self._retry_delay = timedelta(seconds=retry_delay_seconds)
        self._log = logger or logging.getLogger("bdpay.platform.notifications")

    def dispatch(
        self,
        *,
        recipient_type: str,
        recipient_id: str,
        channel: str,
        template_id: str,
        template_vars: Mapping[str, str],
        idempotency_key: str,
        language: str = "en",
    ) -> NotificationDispatch:
        """Validate and enqueue (QUEUED). Refusal-first on every input."""
        if recipient_type not in RECIPIENT_TYPES:
            raise NotificationError(
                f"recipient_type must be one of {RECIPIENT_TYPES}, got {recipient_type!r}"
            )
        if channel not in CHANNELS:
            raise NotificationError(
                f"channel must be one of {CHANNELS}, got {channel!r}"
            )
        if language not in LANGUAGES:
            raise NotificationError(
                f"language must be one of {LANGUAGES}, got {language!r}"
            )
        if not recipient_id or not idempotency_key:
            raise NotificationError(
                "recipient_id and idempotency_key must be non-empty"
            )
        template = self._registry.get(template_id)
        template.validate_vars(template_vars)  # fails closed before queueing

        vars_dict = dict(template_vars)
        vars_hash = sha256_canonical(vars_dict)
        existing = self._store.by_idempotency_key(idempotency_key)
        if existing is not None:
            same = (
                existing.recipient_type == recipient_type
                and existing.recipient_id == recipient_id
                and existing.channel == channel
                and existing.template_id == template_id
                and existing.template_vars_hash == vars_hash
                and existing.language == language
            )
            if same:
                return existing
            raise NotificationError(
                f"idempotency_key {idempotency_key!r} was already used with "
                "different parameters (replay with mismatched params is refused)"
            )

        now = self._clock.now()
        dispatch_id = _make_dispatch_id(
            {
                "recipient_id": recipient_id,
                "template_id": template_id,
                "idempotency_key": idempotency_key,
            }
        )
        record = NotificationDispatch(
            dispatch_id=dispatch_id,
            idempotency_key=idempotency_key,
            recipient_type=recipient_type,
            recipient_id=recipient_id,
            channel=channel,
            template_id=template_id,
            template_vars=vars_dict,
            template_vars_hash=vars_hash,
            language=language,
            status="QUEUED",
            attempt_count=0,
            created_at=now,
            updated_at=now,
            next_attempt_at=now,
        )
        self._store.insert(record)
        return record

    async def process_due(self, *, limit: int = 50) -> int:
        """Drain due QUEUED dispatches through the transport; returns count.

        FSM per spec/01: SENDING -> DELIVERED on success; on failure re-queue
        with the retry delay until the attempt cap, then FAILED (terminal,
        with an ops alert log when the template is ``otp_challenge``).
        A transport exception counts as a failure — fails closed, never lost.
        """
        now = self._clock.now()
        processed = 0
        for record in self._store.due(now=now, limit=limit):
            attempt_no = record.attempt_count + 1
            connector_ref = f"ntf-{record.dispatch_id.split('_', 1)[1]}-a{attempt_no}"
            sending = replace(
                record,
                status="SENDING",
                connector_ref=connector_ref,
                updated_at=now,
                next_attempt_at=None,
            )
            self._store.save(sending)
            template = self._registry.get(record.template_id)
            body = template.render(record.language, record.template_vars)
            try:
                result = await self._port.send(
                    channel=record.channel,
                    recipient_ref=record.recipient_id,
                    template_id=record.template_id,
                    body=body,
                    connector_ref=connector_ref,
                )
                delivered = result.delivered
                error_code = result.error_code
                message_id = result.provider_message_id
            except Exception as exc:  # noqa: BLE001 - transport isolation
                delivered = False
                error_code = f"{type(exc).__name__}"
                message_id = None
                self._log.warning(
                    "notification transport raised for %s: %s",
                    record.dispatch_id,
                    error_code,
                )
            done_at = self._clock.now()
            if delivered:
                self._store.save(
                    replace(
                        sending,
                        status="DELIVERED",
                        attempt_count=attempt_no,
                        connector_message_id=message_id,
                        delivered_at=done_at,
                        updated_at=done_at,
                    )
                )
            elif attempt_no >= self._max_attempts:
                if record.template_id == "otp_challenge":
                    self._log.error(
                        "OTP notification %s FAILED after %d attempts (ops alert)",
                        record.dispatch_id,
                        attempt_no,
                    )
                self._store.save(
                    replace(
                        sending,
                        status="FAILED",
                        attempt_count=attempt_no,
                        failed_reason=error_code or "connector_failure",
                        updated_at=done_at,
                    )
                )
            else:
                self._store.save(
                    replace(
                        sending,
                        status="QUEUED",
                        attempt_count=attempt_no,
                        failed_reason=error_code or "connector_failure",
                        next_attempt_at=done_at + self._retry_delay,
                        updated_at=done_at,
                    )
                )
            processed += 1
        return processed

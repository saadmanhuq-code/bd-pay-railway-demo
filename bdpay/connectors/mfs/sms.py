"""``sms_otp_v1`` — notification-service SMS delivery (spec/12 §J).

Idempotency (binding, DB-enforced): one message per
``(event_id, recipient_hash, template_id)`` — a replay is SUPPRESSED.
NotificationMessage FSM (spec/12 §State Machines 4, refusal-first):
``QUEUED -> RENDERED -> SENT -> DELIVERED | FAILED``, with ``SUPPRESSED`` on a
dedupe hit and ``FAILED`` after three send attempts (retry ladder 10s/30s/90s
through the injected sleep). A FAILED OTP message fires the fallback channel
exactly once (template policy; the fallback sender is an injected port —
``email_smtp_v1`` is outside this build group).

Bengali SMS encoding rule (binding algorithm, ported verbatim from spec/12):
ANY character outside GSM 03.38 (every Bengali character) makes the whole
message UCS-2 — 70 units single segment, 67 per segment concatenated; GSM-7 is
160/153 with extension characters counting two septets.

PII: ``notification_messages`` rows carry ``recipient_hash`` (sha256) and
``body_hash`` only; the rendered body goes to the object store and is never
logged (CERT-N1) — this module emits no log lines at all.

Live-mode Twilio gateway (``TwilioSmsGateway``):
  Reads credentials via ``CredentialResolver`` using the config env-name refs
  ``TWILIO_ACCOUNT_SID``, ``TWILIO_AUTH_TOKEN``, ``TWILIO_FROM``
  (connector registry config maps these to OpenBao paths; unit tests supply a
  ``StaticCredentialResolver``).  Wire shape: HTTP POST to the Twilio Messages
  REST API v2010 with ``application/x-www-form-urlencoded`` body and HTTP
  Basic auth.  Twilio error codes are mapped to canonical connector error codes
  via ``TWILIO_ERROR_MAP``; unknown codes fall through to ``twilio_error_{code}``.
"""

from __future__ import annotations

import base64
import hashlib
import re
import urllib.parse
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from bdpay.connectors.mfs.spec12_ids import make_spec12_id
from bdpay.connectors.mfs.wire import WireTransport, parse_json_body
from bdpay.connectors.ports import (
    CredentialResolver,
    EventSink,
    InMemoryEventSink,
    InMemoryObjectStore,
    ObjectStore,
)
from bdpay.connectors.registry import ConnectorMode
from bdpay.platform.canonical import canonical_json, sha256_canonical
from bdpay.platform.clock import Clock
from bdpay.platform.errors import ConflictError, ConnectorError, InvalidRequestError

__all__ = [
    "CONNECTOR_ID",
    "GSM7_BASIC",
    "GSM7_EXT",
    "DeterministicSmsGateway",
    "GatewayOutcome",
    "HttpSmsGateway",
    "NotificationMessageRow",
    "NotificationTemplate",
    "SMS_MAX_SEGMENTS",
    "SmsGateway",
    "SmsOtpConnector",
    "TWILIO_ERROR_MAP",
    "TwilioSmsGateway",
    "sms_encoding_and_segments",
    "twilio_canonical_error",
]

# ---------------------------------------------------------------------------
# Twilio Messages API error taxonomy (binding, spec/12 §J live-mode).
# Twilio integer error codes -> canonical connector error_code strings.
# Source: https://www.twilio.com/docs/api/errors (public documentation).
# Unmapped codes fall through to ``twilio_error_<code>`` so the taxonomy is
# always machine-readable and the registry config can add rows without a code
# change.
# ---------------------------------------------------------------------------
TWILIO_ERROR_MAP: dict[int, str] = {
    # Authentication / configuration
    20003: "twilio_auth_failed",           # authentication error (bad SID or token)
    20404: "twilio_account_not_found",     # account SID does not exist
    20429: "twilio_rate_limit_exceeded",   # too many requests; retry later
    # Sender / number config
    21201: "twilio_from_required",         # no From number supplied
    21210: "twilio_from_not_valid",        # From number not a valid Twilio number
    21211: "twilio_to_not_valid",          # To number not a valid E.164 number
    21212: "twilio_to_not_sms_capable",    # destination not SMS-capable
    21408: "twilio_permission_denied",     # sending to this country not permitted
    21601: "twilio_phone_not_verified",    # unverified number on trial account
    # Message / body
    21602: "twilio_message_body_empty",    # message body is empty
    21610: "twilio_recipient_opted_out",   # recipient unsubscribed (hard stop; do not retry)
    21614: "twilio_to_not_mobile",         # To is not a mobile number
    # Delivery / downstream
    30001: "twilio_queue_overflow",        # message queue full; treat as FAILED
    30002: "twilio_account_suspended",     # account suspended
    30003: "twilio_unreachable",           # handset unreachable; may retry
    30004: "twilio_message_blocked",       # message blocked by carrier
    30005: "twilio_unknown_destination",   # unknown destination handset
    30006: "twilio_landline_unreachable",  # landline / unreachable carrier
    30007: "twilio_carrier_violation",     # message filtered by carrier
    30008: "twilio_unknown_error",         # unknown carrier error
}


def twilio_canonical_error(twilio_code: int | None) -> str:
    """Return the canonical error_code for a Twilio integer error code.

    Unmapped codes produce ``twilio_error_<code>`` so they remain
    machine-readable at the log/alert layer without a code change.
    """
    if twilio_code is None:
        return "twilio_unknown_error"
    return TWILIO_ERROR_MAP.get(twilio_code, f"twilio_error_{twilio_code}")

CONNECTOR_ID = "sms_otp_v1"

#: Activation-time cost guard (spec/12 §J): bn renders above this are refused.
SMS_MAX_SEGMENTS = 3

#: Send retry ladder (spec/12 FSM 4): three attempts, then FAILED.
SEND_RETRY_BACKOFF_S = (10, 30, 90)

# GSM 03.38 basic charset + extension table (extension chars cost 2 septets).
GSM7_BASIC = set(
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑܧ¿abcdefghijklmnopqrstuvwxyzäöñüà"
)
GSM7_EXT = set("^{}\\[~]|€")  # count as 2 septets


def sms_encoding_and_segments(body: str) -> tuple[str, int]:
    """Binding segmentation algorithm (spec/12 §J, ported verbatim)."""
    if all(ch in GSM7_BASIC or ch in GSM7_EXT for ch in body):
        septets = sum(2 if ch in GSM7_EXT else 1 for ch in body)
        # 160 single-segment; 153 per segment when concatenated
        segs = 1 if septets <= 160 else -(-septets // 153)
        return "GSM7", segs
    # ANY char outside GSM 03.38 (every Bengali char) => whole message UCS-2
    units = len(body.encode("utf-16-be")) // 2  # UTF-16 code units (surrogates count 2)
    # 70 single-segment; 67 per segment concatenated
    segs = 1 if units <= 70 else -(-units // 67)
    return "UCS2", segs


_RENDER_VAR_RE = re.compile(r"\{([a-z][a-z0-9_]*)\}")

_MESSAGE_STATES = frozenset(
    {"QUEUED", "RENDERED", "SENT", "DELIVERED", "FAILED", "SUPPRESSED"}
)
_MESSAGE_ALLOWED = frozenset(
    {
        ("QUEUED", "RENDERED"),
        ("QUEUED", "SUPPRESSED"),
        ("RENDERED", "SENT"),
        ("RENDERED", "FAILED"),
        ("SENT", "DELIVERED"),
        ("SENT", "FAILED"),
    }
)


class TemplateActivationError(InvalidRequestError):
    default_code = "template_segment_budget_exceeded"


class NotificationTransitionDeniedError(ConflictError):
    default_code = "notification_transition_denied"


class NotificationDisabledError(ConnectorError):
    default_code = "connector_disabled"


@dataclass(frozen=True)
class NotificationTemplate:
    """One ``notification_templates`` row (bilingual, versioned)."""

    template_id: str
    name: str
    version: int
    channel: str
    body_en: str
    body_bn: str
    fallback_channel: str = "NONE"
    is_otp: bool = False
    active: bool = True
    created_by: str = "system"


def make_template(
    name: str,
    *,
    body_en: str,
    body_bn: str,
    version: int = 1,
    channel: str = "SMS",
    fallback_channel: str = "NONE",
    is_otp: bool = False,
    created_by: str = "system",
) -> NotificationTemplate:
    """Build + ACTIVATE a template; the segment cost guard runs HERE, not at
    send time (spec/12 §J): a ``body_bn`` that renders beyond
    ``SMS_MAX_SEGMENTS`` is refused at activation."""
    _, bn_segments = sms_encoding_and_segments(body_bn)
    if bn_segments > SMS_MAX_SEGMENTS:
        raise TemplateActivationError(
            f"template {name} body_bn renders to {bn_segments} segments "
            f"(max {SMS_MAX_SEGMENTS}); refused at activation"
        )
    return NotificationTemplate(
        template_id=make_spec12_id("ntpl", {"name": name, "version": version}),
        name=name,
        version=version,
        channel=channel,
        body_en=body_en,
        body_bn=body_bn,
        fallback_channel=fallback_channel,
        is_otp=is_otp,
        created_by=created_by,
    )


@dataclass
class NotificationMessageRow:
    """One ``notification_messages`` row (append-only; FSM advances state)."""

    message_id: str
    event_id: str
    template_id: str
    channel: str
    locale: str
    recipient_hash: str
    state: str = "QUEUED"
    encoding: str | None = None
    segment_count: int | None = None
    body_hash: str | None = None
    body_pointer: str | None = None
    provider_message_id: str | None = None
    failure_code: str | None = None
    queued_at: datetime | None = None
    sent_at: datetime | None = None
    delivered_at: datetime | None = None
    fallback_fired: bool = False
    schema_version: int = 1


@dataclass(frozen=True)
class GatewayOutcome:
    """One aggregator send outcome."""

    ok: bool
    provider_message_id: str | None = None
    failure_code: str | None = None


@runtime_checkable
class SmsGateway(Protocol):
    """Aggregator delivery port; live implementation is :class:`HttpSmsGateway`."""

    async def send(self, recipient: str, body: str) -> GatewayOutcome: ...


class HttpSmsGateway:
    """Real aggregator HTTP API gateway (SANDBOX/PRODUCTION wire path)."""

    def __init__(
        self,
        *,
        transport: WireTransport,
        credentials: CredentialResolver,
        config: dict,
    ) -> None:
        self._transport = transport
        self._credentials = credentials
        self._config = dict(config)

    async def send(self, recipient: str, body: str) -> GatewayOutcome:
        api_key_ref = self._config.get(
            "api_key_ref", "openbao:secret/connectors/sms_otp/api_key"
        )
        payload = canonical_json(
            {
                "to": recipient,
                "message": body,
                "sender_id": str(self._config.get("sender_id", "BDPAY")),
            }
        )
        try:
            response = await self._transport.request(
                "POST",
                str(self._config.get("send_url", "")),
                headers={
                    "authorization": f"Bearer {self._credentials.resolve(api_key_ref)}",
                    "content-type": "application/json",
                },
                content=payload,
            )
        except Exception:
            return GatewayOutcome(ok=False, failure_code="transport_error")
        try:
            parsed = parse_json_body(response.body)
        except Exception:
            return GatewayOutcome(ok=False, failure_code="aggregator_response_invalid")
        if response.status_code == 200 and parsed.get("message_id"):
            return GatewayOutcome(ok=True, provider_message_id=str(parsed["message_id"]))
        return GatewayOutcome(
            ok=False, failure_code=str(parsed.get("error", "aggregator_rejected"))
        )


class TwilioSmsGateway:
    """Twilio Messages REST API v2010 gateway (live-mode wire path, spec/12 §J).

    Wire shape (binding):
    - URL: ``https://api.twilio.com/2010-04-01/Accounts/{AccountSID}/Messages.json``
    - Auth: HTTP Basic (``AccountSID:AuthToken`` base64-encoded).
    - Body: ``application/x-www-form-urlencoded`` with fields ``To``, ``From``,
      ``Body``.
    - Success: HTTP 201 ``{"sid": "SM...", ...}`` — ``sid`` is the provider id.
    - Error: HTTP 4xx/5xx ``{"code": <int>, "message": "...", ...}``.

    Credential refs (config keys; values come from the credential resolver —
    never hardcoded here):
    - ``"account_sid_ref"``  -> OpenBao path for ``TWILIO_ACCOUNT_SID``
    - ``"auth_token_ref"``   -> OpenBao path for ``TWILIO_AUTH_TOKEN``
    - ``"from_ref"``         -> OpenBao path for ``TWILIO_FROM``

    Defaults for the three refs use the conventional paths:
    ``openbao:secret/connectors/sms_otp/twilio/{account_sid,auth_token,from}``

    Idempotency note: the connector guarantees exactly one delivery attempt
    per ``(event_id, recipient_hash, template_id)``; the retry ladder above
    this layer is the retry mechanism.  Twilio's own idempotency header is
    NOT used because the connector already holds the per-triple dedupe.
    """

    _BASE_URL = "https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"

    def __init__(
        self,
        *,
        transport: WireTransport,
        credentials: CredentialResolver,
        config: dict,
    ) -> None:
        self._transport = transport
        self._credentials = credentials
        self._config = dict(config)

    def _resolve(self, key: str, default: str) -> str:
        ref = self._config.get(key, default)
        return self._credentials.resolve(ref)

    async def send(self, recipient: str, body: str) -> GatewayOutcome:
        try:
            account_sid = self._resolve(
                "account_sid_ref",
                "openbao:secret/connectors/sms_otp/twilio/account_sid",
            )
            auth_token = self._resolve(
                "auth_token_ref",
                "openbao:secret/connectors/sms_otp/twilio/auth_token",
            )
            from_number = self._resolve(
                "from_ref",
                "openbao:secret/connectors/sms_otp/twilio/from",
            )
        except Exception:
            return GatewayOutcome(ok=False, failure_code="twilio_auth_failed")

        # HTTP Basic auth: base64(AccountSID:AuthToken) — no randomness, no clock.
        raw_cred = f"{account_sid}:{auth_token}".encode()
        auth_header = "Basic " + base64.b64encode(raw_cred).decode("ascii")

        # Form-encoded body (Twilio wire format).
        form_body = urllib.parse.urlencode(
            {"To": recipient, "From": from_number, "Body": body}
        ).encode("utf-8")

        url = self._BASE_URL.format(sid=account_sid)

        try:
            response = await self._transport.request(
                "POST",
                url,
                headers={
                    "authorization": auth_header,
                    "content-type": "application/x-www-form-urlencoded",
                    "accept": "application/json",
                },
                content=form_body,
            )
        except Exception:
            return GatewayOutcome(ok=False, failure_code="transport_error")

        try:
            parsed = parse_json_body(response.body)
        except Exception:
            return GatewayOutcome(ok=False, failure_code="aggregator_response_invalid")

        # Twilio returns 201 on success; ``sid`` is the provider message id.
        if response.status_code == 201 and parsed.get("sid"):
            return GatewayOutcome(ok=True, provider_message_id=str(parsed["sid"]))

        # Map Twilio integer error code to canonical connector error_code.
        raw_code = parsed.get("code")
        twilio_code: int | None = int(raw_code) if raw_code is not None else None
        return GatewayOutcome(ok=False, failure_code=twilio_canonical_error(twilio_code))


class DeterministicSmsGateway:
    """SIMULATOR gateway: outcome is a pure function of the recipient.

    ``sha256(recipient)`` bucket 7 of 10 fails with ``sim_gateway_failure``;
    everything else succeeds with a content-addressed provider id. An explicit
    ``force_fail`` set overrides for scenario scripting. No randomness.
    """

    def __init__(self, *, force_fail: frozenset[str] = frozenset(), buckets: int = 10) -> None:
        self._force_fail = force_fail
        self._buckets = buckets
        self.sent: list[tuple[str, str]] = []

    async def send(self, recipient: str, body: str) -> GatewayOutcome:
        digest = hashlib.sha256(recipient.encode("utf-8")).hexdigest()
        if recipient in self._force_fail or int(digest, 16) % self._buckets == 7:
            return GatewayOutcome(ok=False, failure_code="sim_gateway_failure")
        self.sent.append((recipient, body))
        return GatewayOutcome(ok=True, provider_message_id=f"SIMSMS-{digest[:10]}")


class SmsOtpConnector:
    """The ``sms_otp_v1`` notification connector (bespoke surface, spec/12 §J)."""

    connector_id = CONNECTOR_ID

    def __init__(
        self,
        *,
        mode: ConnectorMode | str,
        clock: Clock,
        gateway: SmsGateway | None = None,
        templates: dict[str, NotificationTemplate] | None = None,
        object_store: ObjectStore | None = None,
        events: EventSink | None = None,
        fallback_sender=None,
        sleep=None,
    ) -> None:
        self._mode = mode if isinstance(mode, ConnectorMode) else ConnectorMode(str(mode))
        self._clock = clock
        if gateway is not None:
            self._gateway = gateway
        elif self._mode is ConnectorMode.SIMULATOR:
            self._gateway = DeterministicSmsGateway()
        else:
            self._gateway = None
        self._templates: dict[str, NotificationTemplate] = dict(templates or {})
        self._objects = object_store if object_store is not None else InMemoryObjectStore()
        self._events = events if events is not None else InMemoryEventSink()
        self._fallback = fallback_sender
        self._sleep = sleep if sleep is not None else _no_sleep
        self._rows: dict[str, NotificationMessageRow] = {}
        self._by_unique: dict[tuple[str, str, str], str] = {}
        self._by_provider_id: dict[str, str] = {}

    # -- template registry ----------------------------------------------------------

    def register_template(self, template: NotificationTemplate) -> None:
        self._templates[template.name] = template

    def template(self, name: str) -> NotificationTemplate:
        template = self._templates.get(name)
        if template is None or not template.active:
            raise InvalidRequestError(
                f"no active template named {name}", code="template_not_found"
            )
        return template

    # -- FSM advance (the only state writer) -------------------------------------------

    def _advance(self, row: NotificationMessageRow, to_state: str) -> None:
        if to_state not in _MESSAGE_STATES:
            raise ValueError(f"unknown notification state {to_state!r}")
        if (row.state, to_state) not in _MESSAGE_ALLOWED:
            raise NotificationTransitionDeniedError(
                f"notification FSM transition {row.state} -> {to_state} is DENIED"
            )
        row.state = to_state

    def _emit(self, event_type: str, row: NotificationMessageRow) -> None:
        self._events.emit(
            event_type,
            {
                "message_id": row.message_id,
                "channel": row.channel,
                "template_id": row.template_id,
                "segment_count": row.segment_count,
            },
        )

    # -- rendering -----------------------------------------------------------------------

    @staticmethod
    def render_body(template_body: str, variables: dict[str, str]) -> str:
        """Render ``{name}`` slots; unknown slots and non-string values refuse."""
        names = set(_RENDER_VAR_RE.findall(template_body))
        for key, value in variables.items():
            if not isinstance(value, str):
                raise InvalidRequestError(
                    f"render variable {key} must be str", code="render_variable_invalid"
                )
        missing = names - set(variables)
        if missing:
            raise InvalidRequestError(
                f"render variables missing: {sorted(missing)}", code="render_variable_missing"
            )

        def substitute(match: re.Match[str]) -> str:
            return variables[match.group(1)]

        return _RENDER_VAR_RE.sub(substitute, template_body)

    # -- send path ------------------------------------------------------------------------

    def rows(self) -> list[NotificationMessageRow]:
        return list(self._rows.values())

    def row(self, message_id: str) -> NotificationMessageRow:
        return self._rows[message_id]

    async def send_notification(
        self,
        *,
        event_id: str,
        template_name: str,
        locale: str,
        recipient: str,
        variables: dict[str, str] | None = None,
    ) -> NotificationMessageRow:
        """Queue + render + deliver one message (idempotent on the spec triple)."""
        if self._mode is ConnectorMode.DISABLED:
            raise NotificationDisabledError("sms_otp_v1 is DISABLED; refusing to queue")
        if locale not in {"bn", "en"}:
            raise InvalidRequestError(f"unknown locale {locale!r}", code="locale_invalid")
        template = self.template(template_name)
        recipient_hash = hashlib.sha256(recipient.encode("utf-8")).hexdigest()
        unique = (event_id, recipient_hash, template.template_id)
        existing_id = self._by_unique.get(unique)
        if existing_id is not None:
            # Dedupe hit (event_id, recipient_hash, template) => SUPPRESSED row.
            suppressed = NotificationMessageRow(
                message_id=make_spec12_id(
                    "ntfm",
                    {
                        "event_id": event_id,
                        "recipient_hash": recipient_hash,
                        "template_id": template.template_id,
                        "replay": True,
                    },
                ),
                event_id=event_id,
                template_id=template.template_id,
                channel=template.channel,
                locale=locale,
                recipient_hash=recipient_hash,
                state="SUPPRESSED",
                queued_at=self._clock.now(),
            )
            return suppressed
        row = NotificationMessageRow(
            message_id=make_spec12_id(
                "ntfm",
                {
                    "event_id": event_id,
                    "recipient_hash": recipient_hash,
                    "template_id": template.template_id,
                },
            ),
            event_id=event_id,
            template_id=template.template_id,
            channel=template.channel,
            locale=locale,
            recipient_hash=recipient_hash,
            queued_at=self._clock.now(),
        )
        self._rows[row.message_id] = row
        self._by_unique[unique] = row.message_id
        body_template = template.body_bn if locale == "bn" else template.body_en
        body = self.render_body(body_template, dict(variables or {}))
        encoding, segments = sms_encoding_and_segments(body)
        row.encoding = encoding
        row.segment_count = segments
        row.body_hash = sha256_canonical({"body": body})
        row.body_pointer = f"notifications/{CONNECTOR_ID}/{row.body_hash}"
        self._objects.put(row.body_pointer, body.encode("utf-8"))
        self._advance(row, "RENDERED")
        # Delivery with the bounded retry ladder.
        outcome: GatewayOutcome | None = None
        for attempt, backoff_s in enumerate(SEND_RETRY_BACKOFF_S):
            outcome = await self._gateway.send(recipient, body)
            if outcome.ok:
                break
            if attempt < len(SEND_RETRY_BACKOFF_S) - 1:
                await self._sleep(backoff_s)
        if outcome is not None and outcome.ok:
            row.provider_message_id = outcome.provider_message_id
            if outcome.provider_message_id:
                self._by_provider_id[outcome.provider_message_id] = row.message_id
            row.sent_at = self._clock.now()
            self._advance(row, "SENT")
            self._emit("notification_message.sent", row)
            return row
        row.failure_code = outcome.failure_code if outcome is not None else "send_failed"
        self._advance(row, "FAILED")
        self._emit("notification_message.failed", row)
        await self._fire_fallback(row, template, body)
        return row

    async def _fire_fallback(
        self, row: NotificationMessageRow, template: NotificationTemplate, body: str
    ) -> None:
        """A FAILED OTP message triggers the fallback channel exactly once."""
        if not template.is_otp or template.fallback_channel == "NONE":
            return
        if row.fallback_fired or self._fallback is None:
            return
        row.fallback_fired = True
        await self._fallback(row, body)

    # -- DLR path -----------------------------------------------------------------------

    def process_dlr(self, provider_message_id: str, *, delivered: bool) -> NotificationMessageRow:
        """Advance SENT -> DELIVERED/FAILED from an aggregator DLR."""
        message_id = self._by_provider_id.get(provider_message_id)
        if message_id is None:
            raise InvalidRequestError(
                "DLR references an unknown provider message id", code="dlr_unknown_message"
            )
        row = self._rows[message_id]
        if delivered:
            row.delivered_at = self._clock.now()
            self._advance(row, "DELIVERED")
            self._emit("notification_message.delivered", row)
        else:
            row.failure_code = "undeliverable"
            self._advance(row, "FAILED")
            self._emit("notification_message.failed", row)
        return row

    def expire_undelivered(self) -> list[str]:
        """No DLR within 24h of send => FAILED (spec/12 FSM 4)."""
        expired: list[str] = []
        now = self._clock.now()
        for row in self._rows.values():
            if row.state != "SENT" or row.sent_at is None:
                continue
            if (now - row.sent_at).total_seconds() >= 24 * 3600:
                row.failure_code = "dlr_timeout"
                self._advance(row, "FAILED")
                self._emit("notification_message.failed", row)
                expired.append(row.message_id)
        return expired

    async def health_check(self) -> bool:
        if self._mode is ConnectorMode.DISABLED:
            return False
        return self._gateway is not None


async def _no_sleep(_seconds: float) -> None:
    return None

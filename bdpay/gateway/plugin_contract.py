"""Plugin API contract — WooCommerce + OpenCart (spec/16 §H, LR-4).

The shop plugins are pure API consumers. This module is the binding contract
they are built against: the closed consumed-operation set, the spec/01
webhook-signature scheme (canonical reference implementation — the outbound
delivery machinery is not yet built, so THIS is the pinned scheme), the
exact-paisa cart-total conversion, the closed order-state mapping, the
key/base-URL environment guard, and the PLG-01..PLG-08 conformance suite.

Signature preimage (pinned from spec/01 "HMAC-SHA256 signing algorithm" and
the merchant verification example ``signed = f"{ts}.{payload.decode()}"``):

    preimage  = ascii(timestamp) + "." + raw_request_body_bytes
    signature = hex(HMAC-SHA256(secret, preimage))
    header    = "t={timestamp},v1={signature}"

i.e. the signature covers the exact body bytes as delivered. During §D
rotation grace the header carries two entries — ``t={ts},v1={new},v1={old}``
— and the verifier accepts if ANY ``v1`` matches (constant-time compares).

No wall-clock reads: callers supply ``now_ts`` / an injectable
:class:`bdpay.platform.clock.Clock`. No floats anywhere near money.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Protocol

from bdpay.gateway.credentials import KEY_PREFIX_LIVE, KEY_PREFIX_TEST
from bdpay.platform.bangla import normalize_bengali_digits
from bdpay.platform.clock import Clock
from bdpay.platform.errors import InvalidRequestError
from bdpay.platform.money import Money, MoneyError

__all__ = [
    "BASE_URL_ENV_LIVE",
    "BASE_URL_ENV_SANDBOX",
    "CONFORMANCE_CHECKLIST",
    "CONSUMED_OPERATIONS",
    "ConformanceCheck",
    "ConformanceRunner",
    "ConsumedOperation",
    "IdempotentWebhookConsumer",
    "ORDER_STATE_MAP",
    "PLUGIN_WEBHOOK_EVENTS",
    "PluginApiClient",
    "SIGNATURE_TOLERANCE_SECONDS",
    "WEBHOOK_TIMEOUT_FALLBACK_SECONDS",
    "build_grace_header",
    "cart_total_to_paisa",
    "map_event_to_order_action",
    "sign_webhook",
    "validate_key_against_base_url",
    "verify_webhook_signature",
]

#: spec/01 / spec/16 §H rule 3 — reject if |now − t| > 300s (exactly 300 passes).
SIGNATURE_TOLERANCE_SECONDS = 300

#: spec/16 §H PLG-07 — no webhook within 120s ⇒ the plugin polls and reconciles.
WEBHOOK_TIMEOUT_FALLBACK_SECONDS = 120

# ---------------------------------------------------------------------------
# Consumed API surface (closed set — spec/16 §H table)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConsumedOperation:
    """One row of the closed consumed-API table."""

    operation: str
    method: str
    path: str
    notes: str


#: The four webhook event types a plugin registers for (closed set).
PLUGIN_WEBHOOK_EVENTS: tuple[str, ...] = (
    "payment_intent.succeeded",
    "payment_intent.failed",
    "refund.succeeded",
    "refund.failed",
)

CONSUMED_OPERATIONS: Mapping[str, ConsumedOperation] = MappingProxyType(
    {
        "create_payment": ConsumedOperation(
            operation="create_payment",
            method="POST",
            path="/v1/payment-intents",
            notes=(
                "Server-side only, merchant bdpk_ key; "
                "Idempotency-Key = plugin order ID derivative."
            ),
        ),
        "confirm_payment": ConsumedOperation(
            operation="confirm_payment",
            method="POST",
            path="/v1/payment-intents/{id}/confirm",
            notes="Hosted flow: follow next_action.redirect_to_url.",
        ),
        "poll_payment": ConsumedOperation(
            operation="poll_payment",
            method="GET",
            path="/v1/payment-intents/{id}",
            notes="Status poll — the webhook-timeout fallback path (PLG-07).",
        ),
        "create_refund": ConsumedOperation(
            operation="create_refund",
            method="POST",
            path="/v1/refunds",
            notes="Refund from store admin; Idempotency-Key required.",
        ),
        "register_webhook": ConsumedOperation(
            operation="register_webhook",
            method="POST",
            path="/v1/webhook-endpoints",
            notes="events: " + ", ".join(PLUGIN_WEBHOOK_EVENTS),
        ),
    }
)

# ---------------------------------------------------------------------------
# Webhook signing / verification (spec/01 scheme, spec/16 §D grace format)
# ---------------------------------------------------------------------------

_HEX_SIG_RE = re.compile(r"[0-9a-f]{64}")


def _compute_signature(secret: str, timestamp: int, body: bytes) -> str:
    preimage = str(timestamp).encode("ascii") + b"." + body
    return hmac.new(secret.encode("utf-8"), preimage, hashlib.sha256).hexdigest()


def _require_sign_inputs(secret: str, timestamp: int, body: bytes) -> None:
    if not isinstance(secret, str) or not secret:
        raise InvalidRequestError("signing secret must be a non-empty string")
    if isinstance(timestamp, bool) or not isinstance(timestamp, int) or timestamp < 0:
        raise InvalidRequestError("timestamp must be a non-negative integer (unix seconds)")
    if not isinstance(body, bytes):
        raise InvalidRequestError("body must be bytes (the exact delivered payload)")


def sign_webhook(secret: str, timestamp: int, body: bytes) -> str:
    """Produce the ``BDPay-Signature`` header value ``t={ts},v1={sig}``.

    Preimage is ``ascii(timestamp) + "." + body`` — the exact body bytes as
    delivered, per the spec/01 signing algorithm and verification example.
    """
    _require_sign_inputs(secret, timestamp, body)
    return f"t={timestamp},v1={_compute_signature(secret, timestamp, body)}"


def build_grace_header(secret_new: str, secret_old: str, timestamp: int, body: bytes) -> str:
    """Dual-signature header during §D rotation grace: ``t={ts},v1={new},v1={old}``."""
    _require_sign_inputs(secret_new, timestamp, body)
    _require_sign_inputs(secret_old, timestamp, body)
    sig_new = _compute_signature(secret_new, timestamp, body)
    sig_old = _compute_signature(secret_old, timestamp, body)
    return f"t={timestamp},v1={sig_new},v1={sig_old}"


def verify_webhook_signature(
    header: str,
    body: bytes,
    secret: str,
    *,
    now_ts: int,
    tolerance_seconds: int = SIGNATURE_TOLERANCE_SECONDS,
) -> bool:
    """Verify a ``t=...,v1=...[,v1=...]`` signature header. Never raises.

    - parses ``t`` plus ALL ``v1`` entries (rotation grace requires accepting
      if ANY ``v1`` matches);
    - every candidate is compared with ``hmac.compare_digest``;
    - rejects when ``|now_ts − t| > tolerance_seconds`` (exactly at the
      boundary passes);
    - fails closed (returns ``False``) on any malformed input.
    """
    if (
        not isinstance(header, str)
        or not isinstance(body, bytes)
        or not isinstance(secret, str)
        or not secret
        or isinstance(now_ts, bool)
        or not isinstance(now_ts, int)
    ):
        return False
    timestamp: int | None = None
    candidates: list[str] = []
    for part in header.split(","):
        key, sep, value = part.partition("=")
        if not sep or not value:
            return False
        if key == "t":
            if timestamp is not None or not value.isascii() or not value.isdigit():
                return False  # duplicate or non-numeric t → malformed
            timestamp = int(value)
        elif key == "v1":
            candidates.append(value)
        # unknown schemes (e.g. a future v2) are ignored, per the
        # any-matching-v1 verification loop in spec/01.
    if timestamp is None or not candidates:
        return False
    if abs(now_ts - timestamp) > tolerance_seconds:
        return False
    expected = _compute_signature(secret, timestamp, body)
    matched = False
    for candidate in candidates:
        # compare every candidate (no early exit) — constant-time per entry.
        if hmac.compare_digest(expected, candidate):
            matched = True
    return matched


# ---------------------------------------------------------------------------
# Money — cart total → integer paisa (spec/16 §H rule 2, spec/00 §6, E17)
# ---------------------------------------------------------------------------

#: Non-negative plain decimal string (after Bengali-digit normalization).
_CART_TOTAL_RE = re.compile(r"\d+(?:\.\d+)?")

#: Golden conversion vectors (input → paisa). PLG-04 verifies all of them.
PAISA_GOLDEN_VECTORS: tuple[tuple[str, int], ...] = (
    ("10.01", 1001),
    ("১০.০১", 1001),  # Bengali-numeral locale input, normalized before parse
    ("10", 1000),
    ("0.50", 50),
    ("০.০১", 1),
    ("20300.50", 2030050),
    ("99999999.99", 9999999999),
)

#: Inputs that MUST be refused (sub-paisa, negative, non-decimal forms).
PAISA_REFUSAL_VECTORS: tuple[str, ...] = (
    "0.001",
    "-10.01",
    "১০.০০৫",
    "1e2",
    "NaN",
    "10.01.02",
    "",
)


def cart_total_to_paisa(total: str) -> int:
    """Convert a cart-total decimal string to integer paisa, exactly once.

    Exact ``Decimal`` parsing — never float. Bengali numerals are normalized
    first. Raises :class:`bdpay.platform.errors.InvalidRequestError` (the
    one documented refusal type for this module) on anything that is not a
    plain non-negative decimal string, and on sub-paisa precision.
    Golden vector: ``"10.01" → 1001``.
    """
    if not isinstance(total, str):
        raise InvalidRequestError(
            f"cart total must be a decimal string, got {type(total).__name__}",
            code="invalid_cart_total",
        )
    cleaned = normalize_bengali_digits(total).strip()
    if not _CART_TOTAL_RE.fullmatch(cleaned):
        raise InvalidRequestError(
            "cart total must be a plain non-negative decimal string",
            code="invalid_cart_total",
        )
    try:
        money = Money.from_decimal_bdt(Decimal(cleaned))
    except InvalidOperation as exc:  # pragma: no cover — regex precludes this
        raise InvalidRequestError(
            "cart total is not a valid decimal", code="invalid_cart_total"
        ) from exc
    except MoneyError as exc:
        raise InvalidRequestError(
            "cart total has sub-paisa precision; refuse the cart",
            code="sub_paisa_cart_total",
        ) from exc
    return money.amount_minor


# ---------------------------------------------------------------------------
# Order-state mapping (spec/16 §H rule 4 — closed)
# ---------------------------------------------------------------------------

ORDER_STATE_MAP: Mapping[str, str] = MappingProxyType(
    {
        "payment_intent.succeeded": "order_paid",
        "payment_intent.failed": "order_failed",
        "refund.succeeded": "refund_noted",
        "refund.failed": "admin_notice",  # admin notice + order note — never silent
    }
)


def map_event_to_order_action(event_type: str) -> str | None:
    """Closed mapping; unknown event types return ``None`` (acknowledge + ignore)."""
    return ORDER_STATE_MAP.get(event_type)


# ---------------------------------------------------------------------------
# Key / base-URL environment guard (spec/16 §H rule 1)
# ---------------------------------------------------------------------------

BASE_URL_ENV_LIVE = "live"
BASE_URL_ENV_SANDBOX = "sandbox"


def validate_key_against_base_url(api_key: str, base_url_env: str) -> None:
    """Hard-fail plugin activation on key/environment mismatch (rule 1).

    ``base_url_env`` is the plugin's explicit base-URL setting, resolved to
    ``"live"`` or ``"sandbox"``. A ``bdpk_test_`` key against the live base
    URL raises; so does ``bdpk_live_`` against the sandbox base URL.
    """
    if base_url_env not in (BASE_URL_ENV_LIVE, BASE_URL_ENV_SANDBOX):
        raise InvalidRequestError(
            f"unknown base URL environment {base_url_env!r} (expected 'live' or 'sandbox')",
            code="unknown_base_url_environment",
        )
    if not isinstance(api_key, str):
        raise InvalidRequestError("api_key must be a string", code="invalid_api_key")
    if api_key.startswith(KEY_PREFIX_TEST):
        key_env = BASE_URL_ENV_SANDBOX
    elif api_key.startswith(KEY_PREFIX_LIVE):
        key_env = BASE_URL_ENV_LIVE
    else:
        raise InvalidRequestError(
            "api key has an unknown prefix (expected bdpk_test_ or bdpk_live_)",
            code="invalid_api_key",
        )
    if key_env != base_url_env:
        raise InvalidRequestError(
            f"{key_env} api key configured against the {base_url_env} base URL; "
            "plugin activation refused",
            code="key_environment_mismatch",
        )


# ---------------------------------------------------------------------------
# Conformance suite — PLG-01 .. PLG-08 (spec/16 §H checklist)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConformanceCheck:
    check_id: str
    name: str
    description: str


CONFORMANCE_CHECKLIST: tuple[ConformanceCheck, ...] = (
    ConformanceCheck(
        "PLG-01",
        "idempotent create",
        "Re-submitting the same order (same Idempotency-Key) returns the same intent.",
    ),
    ConformanceCheck(
        "PLG-02",
        "signature rejection",
        "A tampered v1 signature is rejected by the verifier; the delivery is not honored.",
    ),
    ConformanceCheck(
        "PLG-03",
        "rotation grace",
        "The dual-signature header t={ts},v1={new},v1={old} verifies with either secret.",
    ),
    ConformanceCheck(
        "PLG-04",
        "paisa exactness",
        "BDT 10.01 cart → 1001 paisa golden vectors, incl. Bengali-numeral input; "
        "sub-paisa and negative totals refused.",
    ),
    ConformanceCheck(
        "PLG-05",
        "webhook replay dedupe",
        "The same event_id delivered twice is processed exactly once (idempotent consumer).",
    ),
    ConformanceCheck(
        "PLG-06",
        "refund round-trip",
        "Create → confirm → refund via POST /v1/refunds; refund replay is idempotent.",
    ),
    ConformanceCheck(
        "PLG-07",
        "timeout fallback",
        "No webhook within 120s → GET /v1/payment-intents/{id} poll reconciles order state.",
    ),
    ConformanceCheck(
        "PLG-08",
        "test-key-on-live refusal",
        "A bdpk_test_ key against the live base URL hard-fails activation (and vice versa).",
    ),
)


class PluginApiClient(Protocol):
    """Minimal HTTP surface the conformance runner drives. No network here —
    tests inject an in-memory sandbox-shaped fake; the shipped suite injects
    a real HTTPS client pointed at the public sandbox."""

    def post(
        self,
        path: str,
        *,
        headers: Mapping[str, str],
        json_body: Mapping[str, object],
    ) -> tuple[int, dict[str, object]]: ...

    def get(
        self, path: str, *, headers: Mapping[str, str]
    ) -> tuple[int, dict[str, object]]: ...


class IdempotentWebhookConsumer:
    """Reference merchant-side webhook consumer harness (PLG-05).

    verify signature → parse → dedupe on ``event_id`` → map to order action.
    Deliveries are at-least-once: a duplicate is acknowledged (200) but not
    re-processed. 2xx is returned only after the event is recorded locally.
    """

    def __init__(
        self,
        secret: str,
        clock: Clock,
        *,
        tolerance_seconds: int = SIGNATURE_TOLERANCE_SECONDS,
    ) -> None:
        self._secret = secret
        self._clock = clock
        self._tolerance = tolerance_seconds
        self._processed: dict[str, str | None] = {}
        self._order_actions: list[str] = []

    @property
    def processed_event_ids(self) -> tuple[str, ...]:
        return tuple(self._processed)

    @property
    def order_actions(self) -> tuple[str, ...]:
        return tuple(self._order_actions)

    def handle(self, signature_header: str, body: bytes) -> int:
        """Process one delivery; returns the HTTP status the plugin would send."""
        now_ts = int(self._clock.now().timestamp())
        if not verify_webhook_signature(
            signature_header,
            body,
            self._secret,
            now_ts=now_ts,
            tolerance_seconds=self._tolerance,
        ):
            return 400
        try:
            event = json.loads(body)
        except ValueError:
            return 400
        event_id = event.get("event_id") if isinstance(event, dict) else None
        if not isinstance(event_id, str) or not event_id:
            return 400
        if event_id in self._processed:
            return 200  # at-least-once delivery: acknowledge, do not re-apply
        event_type = event.get("type")
        action = map_event_to_order_action(event_type) if isinstance(event_type, str) else None
        # durable local persistence happens BEFORE the 2xx acknowledgement.
        self._processed[event_id] = action
        if action is not None:
            self._order_actions.append(action)
        return 200


_RUNNER_TEST_KEY = KEY_PREFIX_TEST + "0" * 64
_RUNNER_LIVE_KEY = KEY_PREFIX_LIVE + "0" * 64
_RUNNER_SECRET_NEW = "whsec_" + "a1" * 32
_RUNNER_SECRET_OLD = "whsec_" + "b2" * 32
_MAX_WAIT_READS = 10_000
_OK_STATUSES = (200, 201)
_TERMINAL_INTENT_STATUSES = ("SUCCEEDED", "FAILED", "CANCELLED")


class ConformanceRunner:
    """Runs PLG-01..PLG-08 against an injected client + injectable clock.

    Everything is deterministic: no network, no wall clock, no randomness.
    """

    def __init__(
        self,
        client: PluginApiClient,
        *,
        clock: Clock,
        api_key: str = _RUNNER_TEST_KEY,
    ) -> None:
        # The suite runs against the sandbox; a non-sandbox key is refused
        # up front (the same guard PLG-08 exercises).
        validate_key_against_base_url(api_key, BASE_URL_ENV_SANDBOX)
        self._client = client
        self._clock = clock
        self._api_key = api_key

    # -- helpers -------------------------------------------------------------

    def _now_ts(self) -> int:
        return int(self._clock.now().timestamp())

    def _headers(self, idempotency_key: str | None = None) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self._api_key}"}
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        return headers

    def _create_intent(
        self, idempotency_key: str, amount_minor: int, order_id: str
    ) -> tuple[int, dict[str, object]]:
        return self._client.post(
            "/v1/payment-intents",
            headers=self._headers(idempotency_key),
            json_body={
                "amount_minor": amount_minor,
                "currency": "BDT",
                "metadata": {"order_id": order_id},  # order numbers only — rule 5
            },
        )

    # -- checks ---------------------------------------------------------------

    def check_plg01_idempotent_create(self) -> tuple[bool, str]:
        idem = "plg01-order-10001-attempt-1"
        status_1, body_1 = self._create_intent(idem, 1001, "10001")
        status_2, body_2 = self._create_intent(idem, 1001, "10001")
        if status_1 not in _OK_STATUSES or status_2 not in _OK_STATUSES:
            return False, f"unexpected statuses {status_1}/{status_2}"
        intent_1 = body_1.get("payment_intent_id")
        intent_2 = body_2.get("payment_intent_id")
        if not intent_1 or intent_1 != intent_2:
            return False, f"intent ids diverged on replay: {intent_1!r} != {intent_2!r}"
        return True, f"same Idempotency-Key replay returned the same intent {intent_1}"

    def check_plg02_tampered_signature_rejected(self) -> tuple[bool, str]:
        timestamp = self._now_ts()
        body = b'{"event_id":"obx_plg02","type":"payment_intent.succeeded"}'
        header = sign_webhook(_RUNNER_SECRET_NEW, timestamp, body)
        prefix, signature = header.rsplit("=", 1)
        flipped = "0" if signature[-1] != "0" else "1"
        tampered = f"{prefix}={signature[:-1]}{flipped}"
        good = verify_webhook_signature(
            header, body, _RUNNER_SECRET_NEW, now_ts=timestamp
        )
        bad = verify_webhook_signature(
            tampered, body, _RUNNER_SECRET_NEW, now_ts=timestamp
        )
        if not good:
            return False, "genuine signature failed verification"
        if bad:
            return False, "tampered v1 was accepted — delivery would be honored"
        return True, "genuine v1 accepted; tampered v1 rejected"

    def check_plg03_rotation_grace(self) -> tuple[bool, str]:
        timestamp = self._now_ts()
        body = b'{"event_id":"obx_plg03","type":"refund.succeeded"}'
        header = build_grace_header(_RUNNER_SECRET_NEW, _RUNNER_SECRET_OLD, timestamp, body)
        with_new = verify_webhook_signature(
            header, body, _RUNNER_SECRET_NEW, now_ts=timestamp
        )
        with_old = verify_webhook_signature(
            header, body, _RUNNER_SECRET_OLD, now_ts=timestamp
        )
        with_other = verify_webhook_signature(
            header, body, "whsec_" + "c3" * 32, now_ts=timestamp
        )
        if not (with_new and with_old):
            return False, f"dual-sig header verified new={with_new} old={with_old}"
        if with_other:
            return False, "an unrelated secret verified the grace header"
        return True, "dual-signature header verifies with either rotation secret"

    def check_plg04_paisa_exactness(self) -> tuple[bool, str]:
        for text, expected in PAISA_GOLDEN_VECTORS:
            got = cart_total_to_paisa(text)
            if got != expected:
                return False, f"{text!r} → {got}, expected {expected}"
        for text in PAISA_REFUSAL_VECTORS:
            try:
                got = cart_total_to_paisa(text)
            except InvalidRequestError:
                continue
            return False, f"{text!r} was accepted ({got}); it must be refused"
        return True, (
            f"{len(PAISA_GOLDEN_VECTORS)} golden vectors exact "
            f"(incl. Bengali ১০.০১ → 1001); "
            f"{len(PAISA_REFUSAL_VECTORS)} refusal vectors refused"
        )

    def check_plg05_webhook_replay_dedupe(self) -> tuple[bool, str]:
        status, body = self._client.post(
            "/v1/webhook-endpoints",
            headers=self._headers("plg05-webhook-register-1"),
            json_body={
                "url": "https://store.example/wc-api/bdpay",
                "enabled_events": list(PLUGIN_WEBHOOK_EVENTS),
            },
        )
        if status not in _OK_STATUSES:
            return False, f"webhook-endpoint registration failed with {status}"
        secret = body.get("signing_secret")
        if not isinstance(secret, str) or not secret.startswith("whsec_"):
            return False, "registration response missing whsec_ signing_secret"
        consumer = IdempotentWebhookConsumer(secret, self._clock)
        event = {
            "event_id": "obx_plg05replaydedupe0a0b0c01",
            "type": "payment_intent.succeeded",
            "data": {"object": {"payment_intent_id": "pi_plg05"}},
        }
        payload = json.dumps(event, separators=(",", ":"), sort_keys=True).encode("utf-8")
        header = sign_webhook(secret, self._now_ts(), payload)
        ack_1 = consumer.handle(header, payload)
        ack_2 = consumer.handle(header, payload)  # redelivery of the same event_id
        if ack_1 != 200 or ack_2 != 200:
            return False, f"deliveries acked {ack_1}/{ack_2}; both must be 2xx"
        if len(consumer.processed_event_ids) != 1:
            return False, f"event processed {len(consumer.processed_event_ids)} times, want 1"
        if consumer.order_actions != ("order_paid",):
            return False, f"order actions applied {consumer.order_actions!r}"
        return True, "duplicate event_id acked twice, processed once (order_paid applied once)"

    def check_plg06_refund_round_trip(self) -> tuple[bool, str]:
        status, body = self._create_intent("plg06-order-20001-attempt-1", 5000, "20001")
        intent_id = body.get("payment_intent_id")
        if status not in _OK_STATUSES or not isinstance(intent_id, str):
            return False, f"intent create failed with {status}"
        status, _ = self._client.post(
            f"/v1/payment-intents/{intent_id}/confirm",
            headers=self._headers("plg06-confirm-1"),
            json_body={},
        )
        if status not in _OK_STATUSES:
            return False, f"confirm failed with {status}"
        refund_body = {
            "payment_intent_id": intent_id,
            "amount_minor": 5000,
            "currency": "BDT",
        }
        status_1, refund_1 = self._client.post(
            "/v1/refunds", headers=self._headers("plg06-refund-1"), json_body=refund_body
        )
        status_2, refund_2 = self._client.post(
            "/v1/refunds", headers=self._headers("plg06-refund-1"), json_body=refund_body
        )
        if status_1 not in _OK_STATUSES or status_2 not in _OK_STATUSES:
            return False, f"refund statuses {status_1}/{status_2}"
        refund_id_1 = refund_1.get("refund_id")
        refund_id_2 = refund_2.get("refund_id")
        if not refund_id_1 or refund_id_1 != refund_id_2:
            return False, f"refund replay diverged: {refund_id_1!r} != {refund_id_2!r}"
        return True, f"refund {refund_id_1} created against {intent_id}; replay idempotent"

    def check_plg07_timeout_fallback(self) -> tuple[bool, str]:
        status, body = self._create_intent("plg07-order-30001-attempt-1", 2500, "30001")
        intent_id = body.get("payment_intent_id")
        if status not in _OK_STATUSES or not isinstance(intent_id, str):
            return False, f"intent create failed with {status}"
        status, _ = self._client.post(
            f"/v1/payment-intents/{intent_id}/confirm",
            headers=self._headers("plg07-confirm-1"),
            json_body={},
        )
        if status not in _OK_STATUSES:
            return False, f"confirm failed with {status}"
        # Simulate "no webhook arrives": wait out the 120s timeout on the
        # injected clock, then poll.
        started_ts = self._now_ts()
        elapsed = 0
        for _ in range(_MAX_WAIT_READS):
            elapsed = self._now_ts() - started_ts
            if elapsed >= WEBHOOK_TIMEOUT_FALLBACK_SECONDS:
                break
        else:
            return False, "injected clock never reached the 120s webhook timeout"
        status, polled = self._client.get(
            f"/v1/payment-intents/{intent_id}", headers=self._headers()
        )
        intent_status = polled.get("status")
        if status != 200 or intent_status not in _TERMINAL_INTENT_STATUSES:
            return False, f"poll returned {status}/{intent_status!r}; state not reconciled"
        return True, (
            f"no webhook for {elapsed}s; poll reconciled {intent_id} to {intent_status}"
        )

    def check_plg08_test_key_on_live_refusal(self) -> tuple[bool, str]:
        failures: list[str] = []
        try:
            validate_key_against_base_url(_RUNNER_TEST_KEY, BASE_URL_ENV_LIVE)
            failures.append("bdpk_test_ key accepted against the live base URL")
        except InvalidRequestError:
            pass
        try:
            validate_key_against_base_url(_RUNNER_LIVE_KEY, BASE_URL_ENV_SANDBOX)
            failures.append("bdpk_live_ key accepted against the sandbox base URL")
        except InvalidRequestError:
            pass
        try:
            validate_key_against_base_url(self._api_key, BASE_URL_ENV_SANDBOX)
        except InvalidRequestError:
            failures.append("matching key/environment pair was refused")
        if failures:
            return False, "; ".join(failures)
        return True, "key/base-URL mismatches hard-fail activation; matching pair accepted"

    # -- driver ---------------------------------------------------------------

    _CHECK_METHODS: Mapping[str, str] = MappingProxyType(
        {
            "PLG-01": "check_plg01_idempotent_create",
            "PLG-02": "check_plg02_tampered_signature_rejected",
            "PLG-03": "check_plg03_rotation_grace",
            "PLG-04": "check_plg04_paisa_exactness",
            "PLG-05": "check_plg05_webhook_replay_dedupe",
            "PLG-06": "check_plg06_refund_round_trip",
            "PLG-07": "check_plg07_timeout_fallback",
            "PLG-08": "check_plg08_test_key_on_live_refusal",
        }
    )

    def run_all(self) -> list[tuple[str, bool, str]]:
        """Run every check in checklist order; a raising check is a failure."""
        results: list[tuple[str, bool, str]] = []
        for check in CONFORMANCE_CHECKLIST:
            method = getattr(self, self._CHECK_METHODS[check.check_id])
            try:
                passed, detail = method()
            except Exception as exc:  # noqa: BLE001 — a crashing check must fail, not abort
                passed, detail = False, f"check raised {type(exc).__name__}: {exc}"
            results.append((check.check_id, passed, detail))
        return results

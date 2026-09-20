"""Deterministic SMS-aggregator host simulator (spec/12 §J, §K).

Three deterministic counterparties for ``sms_otp_v1``:

- :class:`SmsAggregatorSimulatorHost` — a fake aggregator HTTP endpoint behind
  the ``WireTransport`` port so the REAL :class:`~bdpay.connectors.mfs.sms.
  HttpSmsGateway` wire code runs unmodified in tests. Bearer credentials are
  checked on every call; provider message ids are content-addressed from
  ``(recipient, message)``; failures are scripted (``fail_next`` /
  ``force_fail`` recipients) — never random. Delivered DLRs are pulled from
  ``dlr_outbox()`` in deterministic order.
- :class:`TwilioSimulatorHost` — a fake Twilio Messages API endpoint behind
  the ``WireTransport`` port so the REAL :class:`~bdpay.connectors.mfs.sms.
  TwilioSmsGateway` wire code runs unmodified in tests. Verifies HTTP Basic
  auth on every call; provider message ids are ``SM`` + content-addressed hash;
  failures and Twilio error-code responses are scripted for taxonomy testing.
- :class:`DeterministicSmsGateway` (re-export) — the in-process SIMULATOR-mode
  gateway the connector defaults to: outcome is a pure function of the
  recipient hash.

The ``dlr_failed_fallback`` and ``bengali_segmentation`` §K scenarios are
exercised in the test suite against these counterparties (segmentation golden
vectors live with the connector's binding algorithm).
"""

from __future__ import annotations

import base64
import hashlib
import urllib.parse

from bdpay.connectors.mfs.sms import DeterministicSmsGateway
from bdpay.connectors.mfs.wire import WireResponse, parse_json_body
from bdpay.platform.canonical import canonical_json
from bdpay.platform.clock import Clock

__all__ = ["DeterministicSmsGateway", "SmsAggregatorSimulatorHost", "TwilioSimulatorHost"]


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class SmsAggregatorSimulatorHost:
    """Deterministic SMS aggregator endpoint behind the ``WireTransport`` port."""

    def __init__(
        self,
        *,
        clock: Clock,
        api_key: str = "sms-sim-key",
        undeliverable: frozenset[str] = frozenset(),
    ) -> None:
        self._clock = clock
        self._api_key = api_key
        self._undeliverable = undeliverable
        self._fail_next = 0
        self._force_fail: set[str] = set()
        self.sent: list[dict] = []  # accepted sends in order
        self._dlr_outbox: list[dict] = []

    # -- scripting -----------------------------------------------------------------

    def fail_next(self, count: int) -> None:
        """The next ``count`` send calls answer a 5xx (driving the retry ladder)."""
        self._fail_next = count

    def force_fail(self, recipient: str) -> None:
        """Every send to this recipient is rejected by the aggregator."""
        self._force_fail.add(recipient)

    def dlr_outbox(self) -> list[dict]:
        """Pending delivery reports: ``{provider_message_id, delivered}`` in
        deterministic send order (undeliverable recipients report failure)."""
        pending = list(self._dlr_outbox)
        self._dlr_outbox.clear()
        return pending

    # -- the wire ---------------------------------------------------------------------

    async def request(
        self, method: str, url: str, *, headers: dict, content: bytes
    ) -> WireResponse:
        if headers.get("authorization", "") != f"Bearer {self._api_key}":
            return _json_response(401, {"error": "aggregator_auth_invalid"})
        body = parse_json_body(content)
        recipient = str(body.get("to", ""))
        message = str(body.get("message", ""))
        if self._fail_next > 0:
            self._fail_next -= 1
            return _json_response(503, {"error": "aggregator_unavailable"})
        if recipient in self._force_fail:
            return _json_response(200, {"error": "recipient_blocked"})
        provider_message_id = f"SMS{_sha(f'{recipient}:{message}')[:12]}"
        self.sent.append(
            {
                "provider_message_id": provider_message_id,
                "recipient": recipient,
                "message": message,
            }
        )
        self._dlr_outbox.append(
            {
                "provider_message_id": provider_message_id,
                "delivered": recipient not in self._undeliverable,
            }
        )
        return _json_response(200, {"message_id": provider_message_id, "status": "queued"})


def _json_response(status_code: int, payload: dict) -> WireResponse:
    return WireResponse(status_code=status_code, body=canonical_json(payload))


class TwilioSimulatorHost:
    """Deterministic fake Twilio Messages API endpoint behind the ``WireTransport`` port.

    Validates HTTP Basic auth (``AccountSID:AuthToken``) on every call.
    Provider message ids are ``SM`` + sha256(recipient:body)[:12] (content-
    addressed; no randomness).  Failures are scripted — ``fail_next`` causes
    the next N calls to return a configurable Twilio error code; individual
    recipients can be forced to a specific Twilio error code via
    ``force_twilio_error``.  DLR simulation is not implemented here (Twilio
    DLRs arrive as status-callback webhooks; the send path completes once
    Twilio accepts the message).

    This counterparty lets the REAL ``TwilioSmsGateway`` wire code run
    unmodified against a controlled, deterministic server.
    """

    def __init__(
        self,
        *,
        account_sid: str = "ACsimulator00000000000000000000000",
        auth_token: str = "twilio-sim-token",
        from_number: str = "+8801000000000",
        undeliverable: frozenset[str] = frozenset(),
    ) -> None:
        self._account_sid = account_sid
        self._auth_token = auth_token
        self._from_number = from_number
        self._undeliverable = undeliverable
        self._fail_next_code: int | None = None
        self._fail_next_count: int = 0
        self._force_errors: dict[str, int] = {}  # recipient -> Twilio error code
        self.sent: list[dict] = []  # accepted sends in order

    # -- scripting helpers --------------------------------------------------------

    def fail_next(self, count: int, twilio_error_code: int = 21610) -> None:
        """The next ``count`` send calls return a Twilio error response."""
        self._fail_next_count = count
        self._fail_next_code = twilio_error_code

    def force_twilio_error(self, recipient: str, twilio_error_code: int) -> None:
        """Every send to this recipient returns the given Twilio error code."""
        self._force_errors[recipient] = twilio_error_code

    def expected_url(self) -> str:
        """The URL the real gateway will POST to (for assertion in tests)."""
        return (
            f"https://api.twilio.com/2010-04-01/Accounts/{self._account_sid}/Messages.json"
        )

    def expected_auth_header(self) -> str:
        """The Authorization header value the real gateway will send."""
        raw = f"{self._account_sid}:{self._auth_token}".encode()
        return "Basic " + base64.b64encode(raw).decode("ascii")

    # -- the wire -----------------------------------------------------------------

    async def request(
        self, method: str, url: str, *, headers: dict, content: bytes
    ) -> WireResponse:
        # Validate Basic auth.
        expected_auth = self.expected_auth_header()
        if headers.get("authorization", "") != expected_auth:
            return _json_response(
                401,
                {
                    "code": 20003,
                    "message": "Authenticate",
                    "more_info": "https://www.twilio.com/docs/errors/20003",
                    "status": 401,
                },
            )
        # Validate URL structure.
        expected_url = self.expected_url()
        if url != expected_url:
            return _json_response(
                404,
                {"code": 20404, "message": "The requested resource was not found", "status": 404},
            )
        # Parse form-encoded body.
        try:
            params = dict(urllib.parse.parse_qsl(content.decode("utf-8")))
        except Exception:
            return _json_response(
                400,
                {"code": 21602, "message": "Message body is required", "status": 400},
            )
        recipient = params.get("To", "")
        body_text = params.get("Body", "")
        from_field = params.get("From", "")

        # Validate From matches configured number.
        if from_field != self._from_number:
            return _json_response(
                400,
                {"code": 21210, "message": "The From phone number is not valid", "status": 400},
            )

        # Scripted failures — per-recipient override.
        if recipient in self._force_errors:
            code = self._force_errors[recipient]
            return _json_response(
                400,
                {
                    "code": code,
                    "message": f"Scripted Twilio error {code}",
                    "status": 400,
                },
            )

        # Scripted failures — global next-N override.
        if self._fail_next_count > 0:
            self._fail_next_count -= 1
            code = self._fail_next_code if self._fail_next_code is not None else 30001
            return _json_response(
                503,
                {
                    "code": code,
                    "message": f"Scripted Twilio error {code}",
                    "status": 503,
                },
            )

        # Success: content-addressed SID (no randomness).
        sid = "SM" + _sha(f"{recipient}:{body_text}")[:12]
        self.sent.append(
            {
                "sid": sid,
                "to": recipient,
                "from": from_field,
                "body": body_text,
            }
        )
        return _json_response(
            201,
            {
                "sid": sid,
                "status": "queued",
                "to": recipient,
                "from": from_field,
            },
        )

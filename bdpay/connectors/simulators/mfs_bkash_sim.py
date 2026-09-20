"""Deterministic bKash Tokenized-Checkout PGW host simulator (spec/12 §A, §K).

Like the rails simulators, this is a fake RAIL HOST, not a fake adapter: it
implements :class:`~bdpay.connectors.mfs.wire.WireTransport` and speaks the
bKash PGW JSON surface (token grant/refresh, create, execute, payment/status,
refund), so the REAL ``BkashPgwConnector`` SANDBOX/PRODUCTION wire code runs
unmodified in tests with only the transport swapped. It enforces the same
discipline the live rail does:

- token-gated calls: any call carrying a token that is not the host's current
  one answers ``statusCode 2117`` (token_invalid) — driving the adapter's
  EXPIRED -> one re-grant -> single retry path (``token_expiry_midflight``);
- merchant RSA request signatures are verified (PKCS#1 v1.5 / SHA-256) —
  a missing/invalid ``x-bkash-signature`` answers ``statusCode 2001``;
- rail-side invoice idempotency: a second ``create`` for the same
  ``merchantInvoiceNumber`` answers ``statusCode 2029`` (duplicate_invoice);
- exactly one rail-side effect per executed invoice (``effect_count``).

No randomness anywhere: payment ids / trx ids / tokens are content-addressed
from the connector_ref (sha256), time comes from the injected Clock, and all
failure branching is explicit scripting (``fail_grants``, ``script_create``,
``drop_execute``, ``serve_500_execute``, ``expire_token``). RSA/HMAC key
material is injected fixture state — observable branching never depends on it.

``build_ipn`` emits HMAC-signed IPN bodies (the scenario-engine callback
signature scheme, which is what ``BkashIpnWebhookHandler`` verifies in
SIMULATOR mode) including forged-but-validly-signed bodies for the CERT-M1
"IPN alone never settles" check and duplicates for the duplicate-IPN
certification scenario. ``build_bkash_spec12_scenarios`` adds the spec/12 §K
engine-mode scenarios (``ipn_before_execute_response``,
``execute_timeout_query_truth``) on top of the spec/10 baseline set.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from bdpay.connectors.mfs.nagad import rsa_verify_b64
from bdpay.connectors.mfs.wire import WireResponse, parse_json_body
from bdpay.connectors.scenarios import SimulatorScenario, make_scenario
from bdpay.connectors.simulator import sign_callback
from bdpay.platform.canonical import canonical_json
from bdpay.platform.clock import Clock

__all__ = [
    "BkashSimulatorGateway",
    "build_bkash_spec12_scenarios",
    "generate_rsa_keypair_pem",
]

_SIGNATURE_HEADER = "x-bkash-signature"


def generate_rsa_keypair_pem(key_size: int = 2048) -> tuple[str, str]:
    """``(private_pem, public_pem)`` test/simulator keypair (cryptography lib).

    Key material is fixture state: simulator OUTCOMES are pure functions of the
    connector_ref and explicit scripts, never of key bits.
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=key_size)
    private_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")
    public_pem = (
        key.public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode("ascii")
    )
    return private_pem, public_pem


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _rfc3339(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class BkashSimulatorGateway:
    """Deterministic bKash PGW endpoint behind the ``WireTransport`` port."""

    def __init__(
        self,
        *,
        clock: Clock,
        merchant_public_pem: str | None = None,
        webhook_key: bytes = b"sim-cert-verify-key",
        token_seed: str = "bkash-sim-token",
    ) -> None:
        self._clock = clock
        self._merchant_public_pem = merchant_public_pem
        self._webhook_key = webhook_key
        self._token_seed = token_seed
        self._token_counter = 0
        self._current_token: str | None = None
        self._fail_grants = 0
        self._fail_refreshes = 0
        self._create_status: dict[str, str] = {}
        self._execute_status: dict[str, str] = {}
        self._drop_execute: set[str] = set()
        self._serve_500_execute: set[str] = set()
        self._created: dict[str, str] = {}  # ref -> paymentID
        self._ref_by_payment_id: dict[str, str] = {}
        self._txn_status: dict[str, str] = {}  # ref -> Initiated|Completed|Cancelled|Failed
        self._refunds: dict[str, dict] = {}  # ref -> recorded refund body
        self._effects: dict[str, int] = {}
        self.grant_calls = 0
        self.refresh_calls = 0
        self.last_refresh_token: str | None = None  # body refresh_token observed
        self.execute_calls: list[str] = []

    # -- scripting (explicit, deterministic) --------------------------------------

    def fail_grants(self, count: int) -> None:
        self._fail_grants = count

    def fail_refreshes(self, count: int) -> None:
        self._fail_refreshes = count

    def script_create(self, connector_ref: str, status_code: str) -> None:
        """Force a bKash error code on ``create`` for one invoice."""
        self._create_status[connector_ref] = status_code

    def script_execute(self, connector_ref: str, status_code: str) -> None:
        self._execute_status[connector_ref] = status_code

    def drop_execute(self, connector_ref: str) -> None:
        """Transport drop on execute — the ambiguity the adapter must NOT retry."""
        self._drop_execute.add(connector_ref)

    def serve_500_execute(self, connector_ref: str) -> None:
        self._serve_500_execute.add(connector_ref)

    def expire_token(self) -> None:
        """Invalidate the current token: the next gated call answers 2117."""
        self._current_token = None

    def cancel_payment(self, connector_ref: str) -> None:
        self._txn_status[connector_ref] = "Cancelled"

    def effect_count(self, connector_ref: str) -> int:
        """Rail-side money effects for this invoice (exactly one per execute)."""
        return self._effects.get(connector_ref, 0)

    @property
    def current_token(self) -> str | None:
        return self._current_token

    # -- IPN emission ----------------------------------------------------------------

    def build_ipn(
        self,
        connector_ref: str,
        *,
        amount: str = "101.00",
        status: str = "Completed",
        trx_id: str | None = None,
        instruction_id: str = "",
    ) -> tuple[dict, bytes]:
        """One HMAC-signed IPN delivery ``(headers, body)``.

        A caller may forge any field (wrong ``amount``, wrong ``status``) while
        keeping the signature valid — exactly the CERT-M1 threat model: the
        handler must hand kernel QUERY truth, never these claims.
        """
        payload = {
            "merchantInvoiceNumber": connector_ref,
            "instruction_id": instruction_id,
            "trxID": trx_id if trx_id is not None else f"TRX{_sha(connector_ref)[:10]}",
            "amount": amount,
            "transactionStatus": status,
            "paymentID": self._created.get(connector_ref, f"PID{_sha(connector_ref)[:10]}"),
        }
        body = canonical_json(payload)
        timestamp = _rfc3339(self._clock.now())
        return sign_callback(self._webhook_key, body, timestamp), body

    # -- the wire -----------------------------------------------------------------------

    async def request(
        self, method: str, url: str, *, headers: dict, content: bytes
    ) -> WireResponse:
        if url.endswith("/token/grant"):
            return self._grant()
        if url.endswith("/token/refresh"):
            return self._refresh(content)
        gate = self._gate(headers, content)
        if gate is not None:
            return gate
        body = parse_json_body(content)
        if url.endswith("/checkout/create"):
            return self._create(body)
        if url.endswith("/checkout/execute"):
            return await self._execute(body)
        if url.endswith("/payment/status"):
            return self._status(body)
        if url.endswith("/payment/refund"):
            return self._refund(body)
        return _json_response(404, {"statusCode": "9999", "statusMessage": "unknown endpoint"})

    # -- token endpoints -------------------------------------------------------------------

    def _mint_token(self) -> str:
        self._token_counter += 1
        token = f"TOK{_sha(f'{self._token_seed}:{self._token_counter}')[:12]}"
        self._current_token = token
        return token

    def _grant(self) -> WireResponse:
        self.grant_calls += 1
        if self._fail_grants > 0:
            self._fail_grants -= 1
            return _json_response(500, {"statusCode": "503", "statusMessage": "grant down"})
        token = self._mint_token()
        return _json_response(
            200,
            {
                "statusCode": "0000",
                "id_token": token,
                "refresh_token": f"RTK{_sha(token)[:12]}",
                "expires_in": "3600",
            },
        )

    def _refresh(self, content: bytes = b"") -> WireResponse:
        self.refresh_calls += 1
        if content:
            self.last_refresh_token = parse_json_body(content).get("refresh_token")
        if self._fail_refreshes > 0:
            self._fail_refreshes -= 1
            return _json_response(500, {"statusCode": "503", "statusMessage": "refresh down"})
        token = self._mint_token()
        return _json_response(
            200,
            {
                "statusCode": "0000",
                "id_token": token,
                "refresh_token": f"RTK{_sha(token)[:12]}",
                "expires_in": "3600",
            },
        )

    # -- gated-call guards -----------------------------------------------------------------

    def _gate(self, headers: dict, content: bytes) -> WireResponse | None:
        token = headers.get("authorization", "")
        if self._current_token is None or token != self._current_token:
            return _json_response(
                200, {"statusCode": "2117", "statusMessage": "invalid or expired token"}
            )
        if self._merchant_public_pem is not None:
            signature = headers.get(_SIGNATURE_HEADER, "")
            if not signature or not rsa_verify_b64(
                self._merchant_public_pem, content, signature
            ):
                return _json_response(
                    200, {"statusCode": "2001", "statusMessage": "request signature invalid"}
                )
        return None

    # -- payment endpoints ------------------------------------------------------------------

    def _create(self, body: dict) -> WireResponse:
        ref = str(body.get("merchantInvoiceNumber", ""))
        scripted = self._create_status.get(ref)
        if scripted is not None:
            return _json_response(200, {"statusCode": scripted})
        if ref in self._created:
            # Rail-side invoice idempotency echo (spec/12 §A error table).
            return _json_response(200, {"statusCode": "2029", "statusMessage": "duplicate"})
        payment_id = f"PID{_sha(ref)[:10]}"
        self._created[ref] = payment_id
        self._ref_by_payment_id[payment_id] = ref
        self._txn_status[ref] = "Initiated"
        return _json_response(
            200,
            {
                "statusCode": "0000",
                "paymentID": payment_id,
                "bkashURL": f"https://sim.bka.sh/redirect/{payment_id}",
                "amount": str(body.get("amount", "")),
                "merchantInvoiceNumber": ref,
            },
        )

    async def _execute(self, body: dict) -> WireResponse:
        payment_id = str(body.get("paymentID", ""))
        ref = self._ref_by_payment_id.get(payment_id, "")
        self.execute_calls.append(ref)
        if ref in self._drop_execute:
            self._drop_execute.discard(ref)
            # The rail PROCESSED the payment but the reply is lost in transit:
            # query_status must find the recorded truth (never re-execute).
            self._complete(ref)
            raise ConnectionError("simulated transport drop on execute")
        if ref in self._serve_500_execute:
            self._serve_500_execute.discard(ref)
            self._complete(ref)
            return _json_response(500, {"statusCode": "503", "statusMessage": "psp 5xx"})
        scripted = self._execute_status.get(ref)
        if scripted is not None:
            return _json_response(200, {"statusCode": scripted})
        if not ref:
            return _json_response(200, {"statusCode": "9999", "statusMessage": "unknown id"})
        self._complete(ref)
        return _json_response(
            200,
            {
                "statusCode": "0000",
                "transactionStatus": "Completed",
                "trxID": f"TRX{_sha(ref)[:10]}",
                "paymentID": payment_id,
                "merchantInvoiceNumber": ref,
            },
        )

    def _complete(self, ref: str) -> None:
        if self._txn_status.get(ref) != "Completed":
            self._txn_status[ref] = "Completed"
            self._effects[ref] = self._effects.get(ref, 0) + 1

    def _status(self, body: dict) -> WireResponse:
        payment_id = str(body.get("paymentID", ""))
        ref = self._ref_by_payment_id.get(payment_id, "")
        txn_status = self._txn_status.get(ref, "Initiated")
        payload: dict = {
            "statusCode": "0000",
            "transactionStatus": txn_status,
            "paymentID": payment_id,
            "merchantInvoiceNumber": ref,
        }
        if txn_status == "Completed":
            payload["trxID"] = f"TRX{_sha(ref)[:10]}"
        return _json_response(200, payload)

    def _refund(self, body: dict) -> WireResponse:
        payment_id = str(body.get("paymentID", ""))
        ref = self._ref_by_payment_id.get(payment_id, "")
        recorded = self._refunds.get(ref)
        if recorded is not None:
            return _json_response(200, recorded)  # rail-side refund idempotency
        if self._txn_status.get(ref) != "Completed":
            return _json_response(
                200, {"statusCode": "2062", "statusMessage": "nothing to refund"}
            )
        payload = {
            "statusCode": "0000",
            "refundTrxID": f"RTX{_sha(ref)[:10]}",
            "originalTrxID": str(body.get("trxID") or f"TRX{_sha(ref)[:10]}"),
            "amount": str(body.get("amount", "")),
            "transactionStatus": "Refunded",
        }
        self._refunds[ref] = payload
        return _json_response(200, payload)


def _json_response(status_code: int, payload: dict) -> WireResponse:
    return WireResponse(status_code=status_code, body=canonical_json(payload))


def build_bkash_spec12_scenarios(
    connector_id: str, *, created_at: datetime, created_by: str = "system"
) -> list[SimulatorScenario]:
    """spec/12 §K engine-mode additions for ``bkash_pgw_v2``.

    ``ipn_before_execute_response`` — the signed IPN is queued 0ms after the
    create responds PENDING, before any execute: an unmatched ref PARKs in the
    pipeline and re-matches to exactly one kernel handoff (spec/10 FSM).
    ``execute_timeout_query_truth`` — the rail processed the payment (truth
    recorded as success) but the synchronous reply is lost; ``query_status``
    is the recorded truth and nothing re-executes (LB4 semantics).
    ``token_expiry_midflight`` is wire-level (bKash 2117 mid-call) and is
    exercised against :class:`BkashSimulatorGateway`, not the scenario engine.
    """

    def scenario(name: str, script: list[dict]) -> SimulatorScenario:
        return make_scenario(
            connector_id,
            name,
            match_rules={"metadata_equals": {"sim_scenario": name}},
            script=script,
            created_at=created_at,
            created_by=created_by,
        )

    return [
        scenario(
            "ipn_before_execute_response",
            [
                {
                    "step": "respond",
                    "status": "pending",
                    "delay_ms": 0,
                    "rail_transaction_id": "SIM-{ref_hash8}",
                },
                {
                    "step": "callback",
                    "after_ms": 0,
                    "status": "success",
                    "rail_transaction_id": "SIM-{ref_hash8}",
                    "sign": True,
                },
            ],
        ),
        scenario(
            "execute_timeout_query_truth",
            [
                {
                    "step": "respond",
                    "status": "success",
                    "delay_ms": 0,
                    "rail_transaction_id": "SIM-{ref_hash8}",
                },
                {"step": "timeout"},
            ],
        ),
    ]

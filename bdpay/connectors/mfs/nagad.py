"""``nagad_pgw_v33`` — Nagad PGW API v3.3+ adapter (spec/12 §B).

Crypto scheme (binding): our RSA private key signs outbound ``sensitiveData``
blocks; Nagad's published public key encrypts them; their responses carry
``sensitiveData`` decrypted with our private key and a ``signature`` verified
with THEIR public key. Any signature mismatch => ``FAILED`` /
``error_code="nagad_signature_invalid"``, fail-closed, raw archived.

Challenge determinism (binding): ``datetime`` (yyyyMMddHHmmss) comes from the
injectable clock; the challenge is
``sha256_canonical({"ref": connector_ref, "step": step})[:20]`` — content
addressed, no RNG in the adapter (spec/12 Open question 7 records the
fallback if Nagad's signed agreement demands true randomness).

Flow: initialize/{merchantId}/{orderId=connector_ref} -> complete/
{paymentReferenceId} (amount decimal-string via the sanctioned boundary,
currency code ``"050"``) -> customer redirect (kernel ``REQUIRES_ACTION``) ->
``verify/payment/{paymentReferenceId}`` is TRUTH; callback params are only a
hint. ``rail_transaction_id`` = Nagad ``issuerPaymentRefNo``. Status map:
Success->SUCCESS, Aborted/Cancelled->REJECTED(payment_cancelled_by_payer),
Failed->REJECTED(nagad_declined), Initiated->PENDING, ambiguous/timeout ->
TIMED_OUT then verify (runner settle path).

R11 risk note: Nagad's regulatory standing is contested — this connector is
one registry row; disable in one line without touching core.
"""

from __future__ import annotations

import base64
from hashlib import sha256

from bdpay.connectors.idempotency import IdempotencyStore, InMemoryIdempotencyStore
from bdpay.connectors.mfs.amounts import paisa_to_decimal_string
from bdpay.connectors.mfs.handles import InMemoryMfsHandleStore, MfsHandleStore
from bdpay.connectors.mfs.wire import (
    ManualLocalState,
    RawArchive,
    WireResponse,
    WireTransport,
    parse_json_body,
    rfc3339,
)
from bdpay.connectors.ports import CredentialResolver, ObjectStore
from bdpay.connectors.registry import ConnectorMode
from bdpay.connectors.sdk import ConnectorResult, ConnectorStatus, Money, PaymentInstruction
from bdpay.connectors.simulator import ScenarioEngine
from bdpay.platform.canonical import canonical_json, sha256_canonical
from bdpay.platform.clock import Clock

__all__ = [
    "NAGAD_API_VERSION",
    "NAGAD_CLIENT_TYPE",
    "NAGAD_STATUS_MAP",
    "NagadPgwConnector",
    "NagadSyncStatusTruth",
    "nagad_challenge",
    "nagad_datetime",
    "nagad_km_headers",
    "rsa_decrypt_b64",
    "rsa_encrypt_b64",
    "rsa_sign_b64",
    "rsa_verify_b64",
]

CONNECTOR_ID = "nagad_pgw_v33"

#: Nagad PGW mandatory request headers (cross-validated 2026-06-13 against the
#: independent implementations arif98741/nagadApi, saagoor/takaden,
#: MahmudulHassan5809/nagadpy and shahriar-shojib — strong consensus).
#: ``X-KM-Api-Version`` and ``X-KM-Client-Type`` are fixed constants.
NAGAD_API_VERSION = "v-0.2.0"
NAGAD_CLIENT_TYPE = "PC_WEB"

#: spec/12 §B status map; (status, error_code).
NAGAD_STATUS_MAP: dict[str, tuple[ConnectorStatus, str | None]] = {
    "Success": (ConnectorStatus.SUCCESS, None),
    "Aborted": (ConnectorStatus.REJECTED, "payment_cancelled_by_payer"),
    "Cancelled": (ConnectorStatus.REJECTED, "payment_cancelled_by_payer"),
    "Failed": (ConnectorStatus.REJECTED, "nagad_declined"),
    "Initiated": (ConnectorStatus.PENDING, None),
}

_DEFAULT_PATHS = {
    "initialize": "/check-out/initialize",
    "complete": "/check-out/complete",
    "verify": "/verify/payment",
    "refund": "/purchase/refund",
}


def nagad_datetime(clock: Clock) -> str:
    """``yyyyMMddHHmmss`` from the injectable clock (UTC) — never wall time."""
    return clock.now().strftime("%Y%m%d%H%M%S")


def nagad_challenge(connector_ref: str, step: str) -> str:
    """Content-addressed freshness challenge — unique per instruction, no RNG."""
    return sha256_canonical({"ref": connector_ref, "step": step})[:20]


def nagad_km_headers(client_ip: str, *, json_body: bool) -> dict[str, str]:
    """The three mandatory Nagad ``X-KM-*`` request headers (+ content-type for
    a JSON body). ``X-KM-IP-V4`` is the END-USER request IP; community impls
    forward the client IP. Until the gateway threads the per-transaction client
    IP through to the connector, the connector supplies the configured
    ``client_ip`` (set per deployment) — see CONNECTOR-CONTRACT-VERIFICATION."""
    headers = {
        "X-KM-Api-Version": NAGAD_API_VERSION,
        "X-KM-IP-V4": client_ip,
        "X-KM-Client-Type": NAGAD_CLIENT_TYPE,
    }
    if json_body:
        headers["content-type"] = "application/json"
    return headers


# -- RSA primitives (cryptography lib; PKCS#1 v1.5 per the Nagad merchant kit) --


def _load_private(pem: str):
    from cryptography.hazmat.primitives import serialization

    return serialization.load_pem_private_key(pem.encode("utf-8"), password=None)


def _load_public(pem: str):
    from cryptography.hazmat.primitives import serialization

    return serialization.load_pem_public_key(pem.encode("utf-8"))


def rsa_encrypt_b64(public_pem: str, plaintext: bytes) -> str:
    from cryptography.hazmat.primitives.asymmetric import padding

    return base64.b64encode(_load_public(public_pem).encrypt(plaintext, padding.PKCS1v15())).decode(
        "ascii"
    )


def rsa_decrypt_b64(private_pem: str, cipher_b64: str) -> bytes:
    from cryptography.hazmat.primitives.asymmetric import padding

    return _load_private(private_pem).decrypt(base64.b64decode(cipher_b64), padding.PKCS1v15())


def rsa_sign_b64(private_pem: str, data: bytes) -> str:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    return base64.b64encode(
        _load_private(private_pem).sign(data, padding.PKCS1v15(), hashes.SHA256())
    ).decode("ascii")


def rsa_verify_b64(public_pem: str, data: bytes, signature_b64: str) -> bool:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    try:
        _load_public(public_pem).verify(
            base64.b64decode(signature_b64), data, padding.PKCS1v15(), hashes.SHA256()
        )
        return True
    except (InvalidSignature, ValueError):
        return False


class NagadPgwConnector:
    """Frozen ``sdk.PaymentConnector`` implementation for ``nagad_pgw_v33``."""

    connector_id = CONNECTOR_ID
    supported_methods: tuple[str, ...] = ("NAGAD",)

    def __init__(
        self,
        *,
        mode: ConnectorMode | str,
        clock: Clock,
        engine: ScenarioEngine | None = None,
        transport: WireTransport | None = None,
        credentials: CredentialResolver | None = None,
        config: dict | None = None,
        object_store: ObjectStore | None = None,
        idempotency: IdempotencyStore | None = None,
        handle_store: MfsHandleStore | None = None,
    ) -> None:
        self._mode = mode if isinstance(mode, ConnectorMode) else ConnectorMode(str(mode))
        self._clock = clock
        self._engine = engine
        self._transport = transport
        self._credentials = credentials
        self._config = dict(config or {})
        self._paths = {**_DEFAULT_PATHS, **dict(self._config.get("paths", {}))}
        self._archive = RawArchive(CONNECTOR_ID, object_store)
        self._idem = idempotency if idempotency is not None else InMemoryIdempotencyStore()
        self._manual = ManualLocalState(CONNECTOR_ID, clock=clock, archive=self._archive)
        self._handles: MfsHandleStore = (
            handle_store if handle_store is not None else InMemoryMfsHandleStore()
        )
        self._sim_truth: dict[str, ConnectorResult] = {}
        if self._mode is ConnectorMode.SIMULATOR and engine is None:
            raise ValueError("SIMULATOR mode requires a ScenarioEngine")

    # -- helpers -----------------------------------------------------------------

    @property
    def manual_local(self) -> ManualLocalState:
        return self._manual

    @property
    def object_store(self) -> ObjectStore:
        return self._archive.object_store

    def truth_for(self, connector_ref: str) -> ConnectorResult | None:
        return self._sim_truth.get(connector_ref)

    @property
    def handle_store(self) -> MfsHandleStore:
        return self._handles

    @property
    def idempotency_store(self) -> IdempotencyStore:
        return self._idem

    def payment_ref_for(self, connector_ref: str) -> str | None:
        return self._handles.get(CONNECTOR_ID, connector_ref)

    def _remember_payment_ref(self, connector_ref: str, payment_reference_id: str) -> None:
        self._handles.put(
            CONNECTOR_ID, connector_ref, payment_reference_id, clock=self._clock
        )

    def sync_status_truth(self) -> NagadSyncStatusTruth | None:
        """Live query-confirmed truth port for IPN (CERT-M1). ``None`` outside wire modes."""
        if self._mode not in (ConnectorMode.SANDBOX, ConnectorMode.PRODUCTION):
            return None
        if self._credentials is None:
            return None
        base_url = str(self._config.get("base_url", "")).rstrip("/")
        if not base_url:
            return None
        return NagadSyncStatusTruth(
            base_url=base_url,
            payment_ref_for=self.payment_ref_for,
            clock=self._clock,
            client_ip=str(self._config.get("client_ip", "0.0.0.0")),
            verify_path=self._paths["verify"],
        )

    def _cred(self, name: str) -> str:
        refs = self._config.get("credential_refs", {})
        ref = refs.get(name, f"openbao:secret/connectors/nagad/{self._mode.value.lower()}/{name}")
        return self._credentials.resolve(ref)

    def _url(self, path_key: str, suffix: str = "") -> str:
        base = str(self._config.get("base_url", "")).rstrip("/")
        return f"{base}{self._paths[path_key]}{suffix}"

    def _km_headers(self, *, json_body: bool) -> dict[str, str]:
        """Mandatory Nagad ``X-KM-*`` headers; ``X-KM-IP-V4`` from config."""
        return nagad_km_headers(
            str(self._config.get("client_ip", "0.0.0.0")), json_body=json_body
        )

    def _result(
        self,
        instruction_id: str,
        connector_ref: str,
        status: ConnectorStatus,
        *,
        raw: dict,
        rail_transaction_id: str | None = None,
        error_code: str | None = None,
    ) -> ConnectorResult:
        return ConnectorResult(
            instruction_id=instruction_id,
            connector_ref=connector_ref,
            status=status,
            rail_transaction_id=rail_transaction_id,
            responded_at=rfc3339(self._clock.now()),
            error_code=error_code,
            raw_response_hash=self._archive.archive(raw),
        )

    def _refusal(self, instruction_id: str, connector_ref: str, code: str) -> ConnectorResult:
        raw = {
            "connector_id": CONNECTOR_ID,
            "connector_ref": connector_ref,
            "refused": True,
            "error_code": code,
        }
        return self._result(
            instruction_id, connector_ref, ConnectorStatus.FAILED, raw=raw, error_code=code
        )

    def _signed_payload(self, sensitive: dict) -> bytes:
        """Outbound body: encrypted ``sensitiveData`` + our RSA signature."""
        plaintext = canonical_json(sensitive)
        return canonical_json(
            {
                "sensitiveData": rsa_encrypt_b64(self._cred("nagad_public"), plaintext),
                "signature": rsa_sign_b64(self._cred("rsa_private"), plaintext),
            }
        )

    def _open_response(self, response: WireResponse) -> tuple[dict | None, dict]:
        """Decrypt + verify a Nagad response. ``(None, raw)`` = signature invalid."""
        parsed = parse_json_body(response.body)
        raw = {"http_status": response.status_code, "body": parsed}
        cipher = parsed.get("sensitiveData")
        signature = parsed.get("signature")
        if not cipher or not signature:
            return None, raw
        try:
            plaintext = rsa_decrypt_b64(self._cred("rsa_private"), str(cipher))
        except ValueError:
            return None, raw
        if not rsa_verify_b64(self._cred("nagad_verify_public"), plaintext, str(signature)):
            return None, raw
        opened = parse_json_body(plaintext)
        raw = {"http_status": response.status_code, "body": parsed, "opened": opened}
        return opened, raw

    # -- PaymentConnector ----------------------------------------------------------

    async def submit(self, instruction: PaymentInstruction) -> ConnectorResult:
        ref = instruction.connector_ref
        if self._mode is ConnectorMode.DISABLED:
            return self._refusal(instruction.instruction_id, ref, "connector_disabled")
        if self._mode is ConnectorMode.MANUAL_LOCAL:
            return self._manual.accept_submit(instruction.instruction_id, ref)
        if self._mode is ConnectorMode.SIMULATOR:
            result = await self._engine.submit(instruction)
            self._sim_truth[ref] = result
            return result
        recorded = self._idem.get(CONNECTOR_ID, ref, "SUBMIT")
        if recorded is not None:
            return recorded
        merchant_id = self._cred("merchant_id")
        # Step 1: initialize — receives the Nagad challenge + paymentReferenceId.
        init_body = self._signed_payload(
            {
                "merchantId": merchant_id,
                "orderId": ref,
                "datetime": nagad_datetime(self._clock),
                "challenge": nagad_challenge(ref, "initialize"),
            }
        )
        response = await self._transport.request(
            "POST",
            self._url("initialize", f"/{merchant_id}/{ref}"),
            headers=self._km_headers(json_body=True),
            content=init_body,
        )
        opened, raw = self._open_response(response)
        if opened is None:
            return self._result(
                instruction.instruction_id,
                ref,
                ConnectorStatus.FAILED,
                raw=raw,
                error_code="nagad_signature_invalid",
            )
        payment_reference_id = str(opened.get("paymentReferenceId", ""))
        if not payment_reference_id:
            return self._result(
                instruction.instruction_id,
                ref,
                ConnectorStatus.FAILED,
                raw=raw,
                error_code="nagad_initialize_failed",
            )
        # Step 2: complete — signed sensitiveData with the decimal amount.
        complete_body = self._signed_payload(
            {
                "merchantId": merchant_id,
                "orderId": ref,
                "currencyCode": "050",
                "amount": paisa_to_decimal_string(instruction.amount.amount_minor),
                "challenge": str(opened.get("challenge", nagad_challenge(ref, "complete"))),
            }
        )
        response = await self._transport.request(
            "POST",
            self._url("complete", f"/{payment_reference_id}"),
            headers=self._km_headers(json_body=True),
            content=complete_body,
        )
        opened, raw = self._open_response(response)
        if opened is None:
            return self._result(
                instruction.instruction_id,
                ref,
                ConnectorStatus.FAILED,
                raw=raw,
                error_code="nagad_signature_invalid",
            )
        self._remember_payment_ref(ref, payment_reference_id)
        # Customer redirect (callBackUrl) -> kernel REQUIRES_ACTION; PENDING here.
        result = self._result(instruction.instruction_id, ref, ConnectorStatus.PENDING, raw=raw)
        self._sim_truth[ref] = result
        return self._idem.put(CONNECTOR_ID, ref, "SUBMIT", result)

    async def query_status(
        self, connector_ref: str, rail_transaction_id: str | None
    ) -> ConnectorResult:
        if self._mode is ConnectorMode.SIMULATOR:
            return await self._engine.query_status(connector_ref, rail_transaction_id)
        if self._mode is ConnectorMode.MANUAL_LOCAL:
            recorded = self._manual.recorded(connector_ref)
            if recorded is not None:
                return recorded
            raw = {"connector_id": CONNECTOR_ID, "connector_ref": connector_ref, "manual": True}
            return self._result("", connector_ref, ConnectorStatus.PENDING, raw=raw)
        if self._mode is ConnectorMode.DISABLED:
            return self._refusal("", connector_ref, "connector_disabled")
        payment_reference_id = self.payment_ref_for(connector_ref)
        if payment_reference_id is None:
            raw = {
                "connector_id": CONNECTOR_ID,
                "connector_ref": connector_ref,
                "status": "not_received",
            }
            return self._result(
                "", connector_ref, ConnectorStatus.FAILED, raw=raw, error_code="not_received"
            )
        response = await self._transport.request(
            "GET",
            self._url("verify", f"/{payment_reference_id}"),
            headers=self._km_headers(json_body=False),
            content=b"",
        )
        parsed = parse_json_body(response.body)
        raw = {"http_status": response.status_code, "body": parsed}
        status, error_code = NAGAD_STATUS_MAP.get(
            str(parsed.get("status", "Initiated")), (ConnectorStatus.PENDING, None)
        )
        result = self._result(
            "",
            connector_ref,
            status,
            raw=raw,
            rail_transaction_id=(
                str(parsed["issuerPaymentRefNo"]) if parsed.get("issuerPaymentRefNo") else None
            ),
            error_code=error_code,
        )
        if status in (ConnectorStatus.SUCCESS, ConnectorStatus.REJECTED):
            self._sim_truth[connector_ref] = result
        return result

    async def reverse(
        self,
        connector_ref: str,
        rail_transaction_id: str | None,
        reverse_amount: Money,
        reason: str,
    ) -> ConnectorResult:
        if self._mode is ConnectorMode.SIMULATOR:
            return await self._engine.reverse(
                connector_ref, rail_transaction_id, reverse_amount, reason
            )
        if self._mode is not ConnectorMode.SANDBOX and self._mode is not ConnectorMode.PRODUCTION:
            return self._refusal("", connector_ref, "connector_disabled")
        recorded = self._idem.get(CONNECTOR_ID, connector_ref, "REVERSE")
        if recorded is not None:
            return recorded  # idempotent re-query discipline
        payment_reference_id = self.payment_ref_for(connector_ref)
        if payment_reference_id is None:
            return self._refusal("", connector_ref, "not_received")
        body = self._signed_payload(
            {
                "merchantId": self._cred("merchant_id"),
                "orderId": connector_ref,
                "paymentReferenceId": payment_reference_id,
                "amount": paisa_to_decimal_string(reverse_amount.amount_minor),
                "reason": reason,
                "challenge": nagad_challenge(connector_ref, "refund"),
                "datetime": nagad_datetime(self._clock),
            }
        )
        response = await self._transport.request(
            "POST",
            self._url("refund", f"/{payment_reference_id}"),
            headers=self._km_headers(json_body=True),
            content=body,
        )
        opened, raw = self._open_response(response)
        if opened is None:
            return self._result(
                "",
                connector_ref,
                ConnectorStatus.FAILED,
                raw=raw,
                error_code="nagad_signature_invalid",
            )
        if str(opened.get("status", "")) in {"Success", "Refunded"}:
            result = self._result(
                "",
                connector_ref,
                ConnectorStatus.REVERSED,
                raw=raw,
                rail_transaction_id=(
                    str(opened.get("issuerPaymentRefNo") or rail_transaction_id or "") or None
                ),
            )
            return self._idem.put(CONNECTOR_ID, connector_ref, "REVERSE", result)
        return self._result(
            "", connector_ref, ConnectorStatus.FAILED, raw=raw, error_code="nagad_refund_failed"
        )

    async def health_check(self) -> bool:
        if self._mode is ConnectorMode.SIMULATOR:
            return await self._engine.health_check()
        if self._mode in (ConnectorMode.MANUAL_LOCAL, ConnectorMode.DISABLED):
            return self._mode is ConnectorMode.MANUAL_LOCAL
        try:
            self._cred("merchant_id")
            self._cred("rsa_private")
            return True
        except Exception:
            return False  # unresolvable credentials: unknowable health is not healthy

def _safe_json(response: WireResponse) -> dict:
    try:
        return parse_json_body(response.body)
    except Exception:
        return {"non_json_body_sha256": sha256(response.body).hexdigest()}


class NagadSyncStatusTruth:
    """Synchronous server-side verify/payment truth source for IPN parsing.

    The live counterpart of :meth:`NagadPgwConnector.truth_for`: a forged or
    replayed callback can never move money because the result handed to kernel
    is built from THIS verify answer (CERT-M1). Uses a sync httpx client because
    ``ConnectorWebhookHandler.parse_event`` is synchronous by SDK contract.
    Simulator/manual modes keep using :meth:`NagadPgwConnector.truth_for`.
    """

    def __init__(
        self,
        *,
        base_url: str,
        payment_ref_for,
        clock: Clock,
        client_ip: str = "0.0.0.0",
        verify_path: str = _DEFAULT_PATHS["verify"],
        timeout_s: float = 10.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._payment_ref_for = payment_ref_for
        self._clock = clock
        self._client_ip = client_ip
        self._verify_path = verify_path
        self._timeout_s = timeout_s

    def __call__(self, connector_ref: str) -> ConnectorResult | None:
        import httpx

        payment_reference_id = self._payment_ref_for(connector_ref)
        if payment_reference_id is None:
            return None
        with httpx.Client(timeout=self._timeout_s) as client:
            response = client.get(
                f"{self._base_url}{self._verify_path}/{payment_reference_id}",
                headers=nagad_km_headers(self._client_ip, json_body=False),
            )
        parsed = _safe_json(WireResponse(response.status_code, response.content))
        status, error_code = NAGAD_STATUS_MAP.get(
            str(parsed.get("status", "Initiated")), (ConnectorStatus.PENDING, None)
        )
        return ConnectorResult(
            instruction_id="",
            connector_ref=connector_ref,
            status=status,
            rail_transaction_id=(
                str(parsed["issuerPaymentRefNo"]) if parsed.get("issuerPaymentRefNo") else None
            ),
            responded_at=rfc3339(self._clock.now()),
            error_code=error_code,
            raw_response_hash=sha256_canonical(parsed),
        )

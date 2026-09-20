"""``bkash_pgw_v2`` — bKash Tokenized Checkout PGW adapter (spec/12 §A).

Four modes (spec/10): SIMULATOR delegates to the deterministic ScenarioEngine;
SANDBOX/PRODUCTION run the real wire flow below (fully written — activation
needs only credentials config); MANUAL_LOCAL takes two-eyes manual results;
DISABLED refuses without rail I/O.

Wire flow (SANDBOX/PRODUCTION):

1. OAuth2 grant/refresh through :class:`~bdpay.connectors.mfs.token_state.TokenCache`
   (id_token 1h, refresh_token 28d, refresh at 50min, fail-closed on grant
   failure — ``error_code="mfs_auth_unavailable"``).
2. ``submit()`` -> POST create (mode "0011", ``merchantInvoiceNumber`` =
   ``connector_ref`` — the rail-side idempotency echo). Amounts cross the
   sanctioned paisa->decimal-string boundary exactly once.
3. Customer redirect -> kernel ``REQUIRES_ACTION``; on callback success the
   gateway invokes :meth:`BkashPgwConnector.execute`.
4. Execute is NON-idempotent at the rail (0 submit retries): any ambiguity
   (timeout / 5xx / transport drop) NEVER re-executes — ``query_status`` is the
   recorded truth. bKash 2117/2118 mid-call -> token EXPIRED -> one re-grant +
   a single retry with the same ``connector_ref``.
5. IPN handling is fail-closed AND independently confirmed: an IPN alone never
   settles a payment. ``verify_signature`` is HMAC (fail/absent => False);
   ``parse_event`` re-queries truth through the injected sync truth port and
   hands kernel a result built from the QUERY answer, never the IPN body
   (CERT-M1). Request signing uses the merchant RSA private key (PKCS#1 v1.5 /
   SHA-256 — deterministic) per the merchant agreement.

Raw wire responses are archived content-addressed and hashed via
``sha256_canonical`` (E12); they are never logged. No business DB writes, no
kernel/ledger imports (CERT-03).
"""

from __future__ import annotations

import base64
import hmac as hmac_mod
import json
from datetime import datetime
from hashlib import sha256

from bdpay.connectors.idempotency import IdempotencyStore, InMemoryIdempotencyStore
from bdpay.connectors.mfs.amounts import paisa_to_decimal_string
from bdpay.connectors.mfs.handles import InMemoryMfsHandleStore, MfsHandleStore
from bdpay.connectors.mfs.token_state import (
    MfsAuthUnavailableError,
    TokenCache,
    TokenStateStore,
)
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
from bdpay.connectors.webhooks import normalize_numeric_fields
from bdpay.platform.canonical import canonical_json, sha256_canonical
from bdpay.platform.clock import Clock

__all__ = [
    "BKASH_ERROR_MAP",
    "BKASH_TRANSACTION_STATUS_MAP",
    "BkashIpnWebhookHandler",
    "BkashPgwConnector",
    "BkashSyncStatusTruth",
    "sign_request_rsa",
]

CONNECTOR_ID = "bkash_pgw_v2"

#: Binding principal rows (spec/12 §A); the complete table is registry
#: ``error_map`` config (Open question 1) merged over these defaults.
BKASH_ERROR_MAP: dict[str, tuple[ConnectorStatus, str | None]] = {
    "0000": (ConnectorStatus.SUCCESS, None),
    "2001": (ConnectorStatus.REJECTED, "invalid_app_key"),
    "2023": (ConnectorStatus.REJECTED, "insufficient_balance"),
    "2029": (ConnectorStatus.REJECTED, "duplicate_invoice"),
    "2054": (ConnectorStatus.REJECTED, "invalid_payer_pin_attempts"),
    "2056": (ConnectorStatus.REJECTED, "amount_limit_exceeded"),
    "2062": (ConnectorStatus.REJECTED, "payment_expired"),
    "2068": (ConnectorStatus.REJECTED, "payment_cancelled_by_payer"),
    "2117": (ConnectorStatus.FAILED, "token_invalid"),
    "2118": (ConnectorStatus.FAILED, "token_expired"),
    "503": (ConnectorStatus.FAILED, "bkash_system_error"),
    "9999": (ConnectorStatus.FAILED, "bkash_system_error"),
}

_TOKEN_ERROR_CODES = frozenset({"2117", "2118"})

#: ``transactionStatus`` map (spec/12 §A step 4).
BKASH_TRANSACTION_STATUS_MAP: dict[str, ConnectorStatus] = {
    "Completed": ConnectorStatus.SUCCESS,
    "Initiated": ConnectorStatus.PENDING,
    "Pending": ConnectorStatus.PENDING,
    "Failed": ConnectorStatus.REJECTED,
    "Cancelled": ConnectorStatus.REJECTED,
}

_DEFAULT_PATHS = {
    "grant": "/tokenized/checkout/token/grant",
    "refresh": "/tokenized/checkout/token/refresh",
    "create": "/tokenized/checkout/create",
    "execute": "/tokenized/checkout/execute",
    "status": "/tokenized/checkout/payment/status",
    "refund": "/tokenized/checkout/payment/refund",
}

_SIGNATURE_HEADER = "x-bkash-signature"

_IPN_NUMERIC_FIELDS = ("amount", "amount_minor", "merchantInvoiceNumber", "trxID")


def sign_request_rsa(private_key_pem: str, body: bytes) -> str:
    """Merchant RSA request signature (PKCS#1 v1.5 / SHA-256, deterministic)."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    key = serialization.load_pem_private_key(private_key_pem.encode("utf-8"), password=None)
    signature = key.sign(body, padding.PKCS1v15(), hashes.SHA256())
    return base64.b64encode(signature).decode("ascii")


class BkashPgwConnector:
    """Frozen ``sdk.PaymentConnector`` implementation for ``bkash_pgw_v2``."""

    connector_id = CONNECTOR_ID
    supported_methods: tuple[str, ...] = ("BKASH",)

    def __init__(
        self,
        *,
        mode: ConnectorMode | str,
        clock: Clock,
        engine: ScenarioEngine | None = None,
        transport: WireTransport | None = None,
        credentials: CredentialResolver | None = None,
        token_cache: TokenCache | None = None,
        token_store: TokenStateStore | None = None,
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
        self._token_store = token_store
        if token_cache is not None:
            self._tokens = token_cache
        elif self._mode in (ConnectorMode.SANDBOX, ConnectorMode.PRODUCTION):
            self._tokens = TokenCache(
                CONNECTOR_ID,
                self._mode.value,
                clock=clock,
                grant=self._wire_grant,
                refresh=self._wire_refresh,
                store=token_store,
            )
        else:
            self._tokens = None

    # -- shared helpers ----------------------------------------------------------

    @property
    def manual_local(self) -> ManualLocalState:
        return self._manual

    @property
    def object_store(self) -> ObjectStore:
        return self._archive.object_store

    def truth_for(self, connector_ref: str) -> ConnectorResult | None:
        """Sync recorded-truth lookup for the IPN handler (CERT-M1 port)."""
        return self._sim_truth.get(connector_ref)

    @property
    def handle_store(self) -> MfsHandleStore:
        return self._handles

    @property
    def idempotency_store(self) -> IdempotencyStore:
        return self._idem

    @property
    def token_state_store(self) -> TokenStateStore | None:
        """Backing store for live TokenCache FSM rows (None outside wire modes)."""
        if self._tokens is None:
            return self._token_store
        return self._tokens._store  # noqa: SLF001 — composition/wiring probes

    def payment_id_for(self, connector_ref: str) -> str | None:
        """Durable ``paymentID`` lookup (survives process restart when store is PG)."""
        return self._handles.get(CONNECTOR_ID, connector_ref)

    def _remember_payment_id(self, connector_ref: str, payment_id: str) -> None:
        self._handles.put(CONNECTOR_ID, connector_ref, payment_id, clock=self._clock)

    def _sync_token_supplier(self) -> str:
        if self._tokens is None:
            raise MfsAuthUnavailableError("bkash token cache absent")
        token = self._tokens.current_id_token
        if not token:
            raise MfsAuthUnavailableError(
                "bkash id_token absent in-process; await ensure_token before sync truth"
            )
        return token

    def sync_status_truth(self) -> BkashSyncStatusTruth | None:
        """Live query-confirmed truth port for IPN (CERT-M1). ``None`` outside wire modes."""
        if self._mode not in (ConnectorMode.SANDBOX, ConnectorMode.PRODUCTION):
            return None
        if self._tokens is None or self._credentials is None:
            return None
        base_url = str(self._config.get("base_url", "")).rstrip("/")
        if not base_url:
            return None
        return BkashSyncStatusTruth(
            base_url=base_url,
            credentials=self._credentials,
            token_supplier=self._sync_token_supplier,
            payment_id_for=self.payment_id_for,
            clock=self._clock,
            status_path=self._paths["status"],
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

    def _refusal(
        self, instruction_id: str, connector_ref: str, error_code: str
    ) -> ConnectorResult:
        raw = {
            "connector_id": CONNECTOR_ID,
            "connector_ref": connector_ref,
            "refused": True,
            "error_code": error_code,
        }
        return self._result(
            instruction_id, connector_ref, ConnectorStatus.FAILED, raw=raw, error_code=error_code
        )

    # -- live wire plumbing ----------------------------------------------------------

    def _cred(self, name: str) -> str:
        refs = self._config.get("credential_refs", {})
        ref = refs.get(name, f"openbao:secret/connectors/bkash/{self._mode.value.lower()}/{name}")
        if self._credentials is None:
            raise MfsAuthUnavailableError("no credential resolver configured; fail-closed")
        # CONN-H2 twin: guard the resolved value — a resolver that returns None
        # instead of raising CredentialMissError would propagate a None into RSA/
        # HTTP operations and surface as an AttributeError masked as transport_error.
        value = self._credentials.resolve(ref)
        if value is None:
            raise MfsAuthUnavailableError(
                f"credential resolver returned None for {name!r}; fail-closed"
            )
        return value

    def _url(self, path_key: str) -> str:
        base = str(self._config.get("base_url", "")).rstrip("/")
        return f"{base}{self._paths[path_key]}"

    async def _wire_grant(self):
        from bdpay.connectors.mfs.token_state import GrantOutcome

        body = canonical_json(
            {"app_key": self._cred("app_key"), "app_secret": self._cred("app_secret")}
        )
        headers = {
            "username": self._cred("username"),
            "password": self._cred("password"),
            "content-type": "application/json",
        }
        try:
            response = await self._transport.request(
                "POST", self._url("grant"), headers=headers, content=body
            )
        except Exception:
            return GrantOutcome(ok=False, error_code="grant_transport_error")
        parsed = _safe_json(response)
        self._archive.archive(
            {"endpoint": "token_grant_outcome", "http_status": response.status_code}
        )
        if response.status_code == 200 and parsed.get("id_token"):
            return GrantOutcome(
                ok=True,
                id_token=str(parsed["id_token"]),
                refresh_token=str(parsed.get("refresh_token", "")) or None,
            )
        return GrantOutcome(ok=False, error_code="grant_rejected")

    async def _wire_refresh(self):
        from bdpay.connectors.mfs.token_state import GrantOutcome

        # bKash requires the LIVE refresh_token from the prior grant — confirmed
        # against 5 independent SDKs (shahriar-shojib, ShejanMahamud/bkash-js,
        # sharf-shawon/django-bkash, abdursoft/laravel-bkash, kapilpaul). The
        # FSM only enters refresh while a refresh_token exists (token_state
        # _refresh_cycle), so current_refresh_token is non-None here.
        refresh_token = (
            self._tokens.current_refresh_token if self._tokens is not None else None
        )
        body = canonical_json(
            {
                "app_key": self._cred("app_key"),
                "app_secret": self._cred("app_secret"),
                "refresh_token": refresh_token or "",
            }
        )
        headers = {
            "username": self._cred("username"),
            "password": self._cred("password"),
            "content-type": "application/json",
        }
        try:
            response = await self._transport.request(
                "POST", self._url("refresh"), headers=headers, content=body
            )
        except Exception:
            return GrantOutcome(ok=False, error_code="refresh_transport_error")
        parsed = _safe_json(response)
        if response.status_code == 200 and parsed.get("id_token"):
            return GrantOutcome(
                ok=True,
                id_token=str(parsed["id_token"]),
                refresh_token=str(parsed.get("refresh_token", "")) or None,
            )
        return GrantOutcome(ok=False, error_code="refresh_rejected")

    async def _signed_post(self, path_key: str, payload: dict, token: str) -> WireResponse:
        body = canonical_json(payload)
        headers = {
            "authorization": token,
            "x-app-key": self._cred("app_key"),
            _SIGNATURE_HEADER: sign_request_rsa(self._cred("rsa_private"), body),
            "content-type": "application/json",
        }
        return await self._transport.request(
            "POST", self._url(path_key), headers=headers, content=body
        )

    async def _call_with_token_retry(self, path_key: str, payload: dict) -> tuple[dict, dict]:
        """One signed call; a 2117/2118 reply triggers exactly one re-grant +
        retry with the same payload (same ``connector_ref`` — idempotent)."""
        token = await self._tokens.ensure_token()
        response = await self._signed_post(path_key, payload, token)
        parsed = _safe_json(response)
        raw = {"endpoint": path_key, "http_status": response.status_code, "body": parsed}
        if str(parsed.get("statusCode", "")) in _TOKEN_ERROR_CODES:
            self._tokens.handle_rail_401()
            token = await self._tokens.ensure_token()  # one re-grant
            response = await self._signed_post(path_key, payload, token)
            parsed = _safe_json(response)
            raw = {"endpoint": path_key, "http_status": response.status_code, "body": parsed}
        return parsed, raw

    # -- PaymentConnector: submit ------------------------------------------------------

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
        try:
            payload = {
                "mode": "0011",
                "payerReference": instruction.sender_ref,
                "callbackURL": str(self._config.get("callback_url", "")),
                "amount": paisa_to_decimal_string(instruction.amount.amount_minor),
                "currency": "BDT",
                "intent": "sale",
                "merchantInvoiceNumber": ref,
            }
            parsed, raw = await self._call_with_token_retry("create", payload)
        except MfsAuthUnavailableError:
            return self._refusal(instruction.instruction_id, ref, "mfs_auth_unavailable")
        status_code = str(parsed.get("statusCode", "9999"))
        if status_code == "0000" and parsed.get("paymentID"):
            self._remember_payment_id(ref, str(parsed["paymentID"]))
            result = self._result(
                instruction.instruction_id, ref, ConnectorStatus.PENDING, raw=raw
            )
            self._sim_truth[ref] = result
            return self._idem.put(CONNECTOR_ID, ref, "SUBMIT", result)
        if status_code == "2029":
            # Rail-side idempotency echo: the invoice exists — query is truth.
            return await self.query_status(ref, None)
        mapped_status, error_code = BKASH_ERROR_MAP.get(
            status_code, (ConnectorStatus.FAILED, "bkash_system_error")
        )
        result = self._result(
            instruction.instruction_id, ref, mapped_status, raw=raw, error_code=error_code
        )
        if mapped_status is ConnectorStatus.REJECTED:
            self._idem.put(CONNECTOR_ID, ref, "SUBMIT", result)
        return result

    # -- execute (flow step 3; rail-non-idempotent) --------------------------------------

    async def execute(self, connector_ref: str, *, instruction_id: str = "") -> ConnectorResult:
        """Execute the customer-approved payment. NEVER re-executed on ambiguity."""
        if self._mode is ConnectorMode.SIMULATOR:
            truth = self._sim_truth.get(connector_ref)
            if truth is not None:
                return truth
            return await self.query_status(connector_ref, None)
        if self._mode is not ConnectorMode.SANDBOX and self._mode is not ConnectorMode.PRODUCTION:
            return self._refusal(instruction_id, connector_ref, "connector_disabled")
        recorded = self._idem.get(CONNECTOR_ID, connector_ref, "MANUAL")
        if recorded is not None:
            return recorded
        payment_id = self.payment_id_for(connector_ref)
        if payment_id is None:
            return await self.query_status(connector_ref, None)
        try:
            parsed, raw = await self._call_with_token_retry(
                "execute", {"paymentID": payment_id}
            )
        except MfsAuthUnavailableError:
            return self._refusal(instruction_id, connector_ref, "mfs_auth_unavailable")
        except Exception:
            # Ambiguity (timeout / transport drop / 5xx): never re-execute.
            return await self.query_status(connector_ref, None)
        status_code = str(parsed.get("statusCode", "9999"))
        if status_code == "0000":
            txn_status = str(parsed.get("transactionStatus", "Pending"))
            mapped = BKASH_TRANSACTION_STATUS_MAP.get(txn_status, ConnectorStatus.PENDING)
            result = self._result(
                instruction_id,
                connector_ref,
                mapped,
                raw=raw,
                rail_transaction_id=(str(parsed["trxID"]) if parsed.get("trxID") else None),
            )
            self._sim_truth[connector_ref] = result
            return self._idem.put(CONNECTOR_ID, connector_ref, "MANUAL", result)
        if int(raw.get("http_status", 200)) >= 500:
            # 5xx on execute is ambiguous: the rail may have processed it.
            return await self.query_status(connector_ref, None)
        mapped_status, error_code = BKASH_ERROR_MAP.get(
            status_code, (ConnectorStatus.FAILED, "bkash_system_error")
        )
        result = self._result(
            instruction_id, connector_ref, mapped_status, raw=raw, error_code=error_code
        )
        self._sim_truth[connector_ref] = result
        return result

    # -- PaymentConnector: query_status ----------------------------------------------------

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
            return self._result(
                "", connector_ref, ConnectorStatus.PENDING, raw=raw
            )
        if self._mode is ConnectorMode.DISABLED:
            return self._refusal("", connector_ref, "connector_disabled")
        payment_id = self.payment_id_for(connector_ref)
        if payment_id is None:
            raw = {
                "connector_id": CONNECTOR_ID,
                "connector_ref": connector_ref,
                "status": "not_received",
            }
            return self._result(
                "", connector_ref, ConnectorStatus.FAILED, raw=raw, error_code="not_received"
            )
        try:
            parsed, raw = await self._call_with_token_retry("status", {"paymentID": payment_id})
        except MfsAuthUnavailableError:
            return self._refusal("", connector_ref, "mfs_auth_unavailable")
        txn_status = str(parsed.get("transactionStatus", "Pending"))
        mapped = BKASH_TRANSACTION_STATUS_MAP.get(txn_status, ConnectorStatus.PENDING)
        result = self._result(
            "",
            connector_ref,
            mapped,
            raw=raw,
            rail_transaction_id=(str(parsed["trxID"]) if parsed.get("trxID") else None),
        )
        if mapped in (ConnectorStatus.SUCCESS, ConnectorStatus.REJECTED):
            self._sim_truth[connector_ref] = result
        return result

    # -- PaymentConnector: reverse -----------------------------------------------------------

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
            return recorded  # idempotent on connector_ref+amount: re-query before re-issuing
        payment_id = self.payment_id_for(connector_ref)
        if payment_id is None:
            return self._refusal("", connector_ref, "not_received")
        try:
            parsed, raw = await self._call_with_token_retry(
                "refund",
                {
                    "paymentID": payment_id,
                    "trxID": rail_transaction_id,
                    "amount": paisa_to_decimal_string(reverse_amount.amount_minor),
                    "reason": reason,
                },
            )
        except MfsAuthUnavailableError:
            return self._refusal("", connector_ref, "mfs_auth_unavailable")
        status_code = str(parsed.get("statusCode", "9999"))
        if status_code == "0000":
            result = self._result(
                "",
                connector_ref,
                ConnectorStatus.REVERSED,
                raw=raw,
                rail_transaction_id=(
                    str(parsed.get("refundTrxID") or rail_transaction_id or "") or None
                ),
            )
            return self._idem.put(CONNECTOR_ID, connector_ref, "REVERSE", result)
        mapped_status, error_code = BKASH_ERROR_MAP.get(
            status_code, (ConnectorStatus.FAILED, "bkash_system_error")
        )
        return self._result(
            "", connector_ref, mapped_status, raw=raw, error_code=error_code
        )

    # -- PaymentConnector: health -------------------------------------------------------------

    async def health_check(self) -> bool:
        if self._mode is ConnectorMode.SIMULATOR:
            return await self._engine.health_check()
        if self._mode in (ConnectorMode.MANUAL_LOCAL, ConnectorMode.DISABLED):
            return self._mode is ConnectorMode.MANUAL_LOCAL
        try:
            await self._tokens.ensure_token()
            return True
        except MfsAuthUnavailableError:
            return False


def _safe_json(response: WireResponse) -> dict:
    try:
        return parse_json_body(response.body)
    except Exception:
        return {"non_json_body_sha256": sha256(response.body).hexdigest()}


class BkashSyncStatusTruth:
    """Synchronous server-side payment/status truth source for IPN parsing.

    The live counterpart of :meth:`BkashPgwConnector.truth_for`: a forged or
    replayed IPN can never move money because the result handed to kernel is
    built from THIS query answer (CERT-M1). Uses a sync httpx client because
    ``ConnectorWebhookHandler.parse_event`` is synchronous by SDK contract.
    """

    def __init__(
        self,
        *,
        base_url: str,
        credentials: CredentialResolver,
        token_supplier,
        payment_id_for,
        clock: Clock,
        status_path: str = _DEFAULT_PATHS["status"],
        timeout_s: float = 10.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._credentials = credentials
        self._token_supplier = token_supplier
        self._payment_id_for = payment_id_for
        self._clock = clock
        self._status_path = status_path
        self._timeout_s = timeout_s

    def __call__(self, connector_ref: str) -> ConnectorResult | None:
        import httpx

        payment_id = self._payment_id_for(connector_ref)
        if payment_id is None:
            return None
        body = canonical_json({"paymentID": payment_id})
        with httpx.Client(timeout=self._timeout_s) as client:
            response = client.post(
                f"{self._base_url}{self._status_path}",
                headers={
                    "authorization": self._token_supplier(),
                    "content-type": "application/json",
                },
                content=body,
            )
        parsed = _safe_json(WireResponse(response.status_code, response.content))
        txn_status = str(parsed.get("transactionStatus", "Pending"))
        mapped = BKASH_TRANSACTION_STATUS_MAP.get(txn_status, ConnectorStatus.PENDING)
        return ConnectorResult(
            instruction_id="",
            connector_ref=connector_ref,
            status=mapped,
            rail_transaction_id=(str(parsed["trxID"]) if parsed.get("trxID") else None),
            responded_at=rfc3339(self._clock.now()),
            error_code=None,
            raw_response_hash=sha256_canonical(parsed),
        )


class BkashIpnWebhookHandler:
    """``ConnectorWebhookHandler`` for bKash IPN — fail-closed, query-confirmed.

    Signature scheme: HMAC-SHA256 over ``timestamp + "." + body`` with the
    ``webhook_verify_ref`` key; header names are config (the SIMULATOR-mode
    defaults match the scenario engine's signed callbacks). Verification
    failure, a missing header, an unparsable or stale timestamp — all return
    ``False`` (the pipeline REJECTs; no oracle). ``parse_event`` extracts the
    ``connector_ref`` then consults the truth port; the handed-off result
    carries the QUERY truth — the IPN body is only the trigger (CERT-M1).
    ``raw_response_hash`` is computed over the parsed IPN body so the archived
    raw bytes recompute exactly (CERT-04).
    """

    connector_id = CONNECTOR_ID

    def __init__(
        self,
        key: bytes,
        *,
        clock: Clock,
        truth_for=None,
        max_skew_s: int = 300,
        signature_header: str = "x-sim-signature",
        timestamp_header: str = "x-sim-timestamp",
    ) -> None:
        self._key = key
        self._clock = clock
        self._truth_for = truth_for
        self._max_skew_s = max_skew_s
        self._signature_header = signature_header
        self._timestamp_header = timestamp_header

    def verify_signature(self, headers: dict, body: bytes) -> bool:
        signature = headers.get(self._signature_header)
        timestamp = headers.get(self._timestamp_header)
        if not signature or not timestamp:
            return False
        try:
            stamped = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
        except ValueError:
            return False
        if stamped.tzinfo is None:
            return False
        if abs((self._clock.now() - stamped).total_seconds()) > self._max_skew_s:
            return False
        expected = hmac_mod.new(
            self._key, str(timestamp).encode("utf-8") + b"." + body, sha256
        ).hexdigest()
        return hmac_mod.compare_digest(expected, str(signature))

    def parse_event(self, headers: dict, body: bytes) -> ConnectorResult:
        payload = json.loads(body.decode("utf-8"), parse_float=str)
        normalized = normalize_numeric_fields(payload, _IPN_NUMERIC_FIELDS)
        connector_ref = str(
            normalized.get("connector_ref") or normalized.get("merchantInvoiceNumber") or ""
        )
        if not connector_ref:
            raise ValueError("IPN carries no connector_ref/merchantInvoiceNumber")
        raw_hash = sha256_canonical(payload)
        truth = self._truth_for(connector_ref) if self._truth_for is not None else None
        if truth is not None:
            # Query truth wins — never the IPN's own claims (CERT-M1).
            return ConnectorResult(
                instruction_id=truth.instruction_id,
                connector_ref=connector_ref,
                status=truth.status,
                rail_transaction_id=truth.rail_transaction_id,
                responded_at=rfc3339(self._clock.now()),
                error_code=truth.error_code,
                raw_response_hash=raw_hash,
            )
        # No recorded truth yet: hand kernel a PENDING marker — a PENDING
        # handoff can never settle money; kernel keeps polling.
        return ConnectorResult(
            instruction_id=str(normalized.get("instruction_id", "")),
            connector_ref=connector_ref,
            status=ConnectorStatus.PENDING,
            rail_transaction_id=None,
            responded_at=rfc3339(self._clock.now()),
            error_code=None,
            raw_response_hash=raw_hash,
        )

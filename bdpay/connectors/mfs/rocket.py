"""``rocket_aggregator_v1`` — Rocket via licensed PSO aggregator (spec/12 §C).

Rocket (DBBL) has no public merchant API. Two designed modes:

1. **Aggregator mode (primary, SANDBOX/PRODUCTION):** REST integration with a
   licensed PSO aggregator (SSLCommerz / AamarPay — commercial choice, Open
   question 2) whose session-create / validate / IPN surface covers Rocket.
   IPN verification = aggregator HMAC **plus** a server-side validation-API
   re-query (the same never-trust-the-IPN rule as bKash). Status map:
   ``VALID/VALIDATED -> SUCCESS``, ``FAILED -> REJECTED``,
   ``CANCELLED -> REJECTED(payment_cancelled_by_payer)``,
   ``UNATTEMPTED/PENDING -> PENDING``. ``rail_transaction_id`` = aggregator
   ``bank_tran_id``.
2. **MANUAL_LOCAL contingency:** when the aggregator path is down or not yet
   contracted, ``submit()`` returns PENDING and the outcome is recorded by an
   operator from the aggregator/DBBL portal via the two-eyes manual-result
   flow (spec/10). This is the designed Rocket contingency, not an
   afterthought.
"""

from __future__ import annotations

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

__all__ = ["AGGREGATOR_STATUS_MAP", "RocketAggregatorConnector", "RocketSyncStatusTruth"]

CONNECTOR_ID = "rocket_aggregator_v1"

#: spec/12 §C status map; (status, error_code).
AGGREGATOR_STATUS_MAP: dict[str, tuple[ConnectorStatus, str | None]] = {
    "VALID": (ConnectorStatus.SUCCESS, None),
    "VALIDATED": (ConnectorStatus.SUCCESS, None),
    "FAILED": (ConnectorStatus.REJECTED, "aggregator_declined"),
    "CANCELLED": (ConnectorStatus.REJECTED, "payment_cancelled_by_payer"),
    "UNATTEMPTED": (ConnectorStatus.PENDING, None),
    "PENDING": (ConnectorStatus.PENDING, None),
}

_DEFAULT_PATHS = {
    "create_session": "/gwprocess/v4/api.php",
    "validate": "/validator/api/validationserverAPI.php",
    "refund": "/validator/api/merchantTransIDvalidationAPI.php",
}


class RocketAggregatorConnector:
    """Frozen ``sdk.PaymentConnector`` implementation for ``rocket_aggregator_v1``."""

    connector_id = CONNECTOR_ID
    supported_methods: tuple[str, ...] = ("ROCKET",)

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

    # -- helpers -------------------------------------------------------------------

    @property
    def manual_local(self) -> ManualLocalState:
        return self._manual

    @property
    def object_store(self) -> ObjectStore:
        return self._archive.object_store

    def truth_for(self, connector_ref: str) -> ConnectorResult | None:
        """Recorded truth (server-side validation outcome) for the IPN handler."""
        recorded_manual = self._manual.recorded(connector_ref)
        if recorded_manual is not None:
            return recorded_manual
        return self._sim_truth.get(connector_ref)

    @property
    def handle_store(self) -> MfsHandleStore:
        return self._handles

    @property
    def idempotency_store(self) -> IdempotencyStore:
        return self._idem

    def session_key_for(self, connector_ref: str) -> str | None:
        return self._handles.get(CONNECTOR_ID, connector_ref)

    def _remember_session(self, connector_ref: str, session_key: str) -> None:
        self._handles.put(CONNECTOR_ID, connector_ref, session_key, clock=self._clock)

    def sync_status_truth(self) -> RocketSyncStatusTruth | None:
        """Live query-confirmed truth port for IPN (CERT-M1). ``None`` outside wire modes."""
        if self._mode not in (ConnectorMode.SANDBOX, ConnectorMode.PRODUCTION):
            return None
        if self._credentials is None:
            return None
        base_url = str(self._config.get("base_url", "")).rstrip("/")
        if not base_url:
            return None
        return RocketSyncStatusTruth(
            base_url=base_url,
            credentials=self._credentials,
            session_key_for=self.session_key_for,
            clock=self._clock,
            credential_refs=dict(self._config.get("credential_refs", {})),
            mode=self._mode,
            validate_path=self._paths["validate"],
        )

    def _cred(self, name: str) -> str:
        refs = self._config.get("credential_refs", {})
        ref = refs.get(
            name, f"openbao:secret/connectors/rocket_aggregator/{self._mode.value.lower()}/{name}"
        )
        return self._credentials.resolve(ref)

    def _url(self, path_key: str) -> str:
        base = str(self._config.get("base_url", "")).rstrip("/")
        return f"{base}{self._paths[path_key]}"

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

    # -- PaymentConnector --------------------------------------------------------------

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
        body = canonical_json(
            {
                "store_id": self._cred("store_id"),
                "store_passwd": self._cred("store_passwd"),
                "total_amount": paisa_to_decimal_string(instruction.amount.amount_minor),
                "currency": "BDT",
                "tran_id": ref,
                "product_category": "payment",
                "success_url": str(self._config.get("success_url", "")),
                "fail_url": str(self._config.get("fail_url", "")),
                "cancel_url": str(self._config.get("cancel_url", "")),
                "ipn_url": str(self._config.get("ipn_url", "")),
            }
        )
        response = await self._transport.request(
            "POST",
            self._url("create_session"),
            headers={"content-type": "application/json"},
            content=body,
        )
        parsed = parse_json_body(response.body)
        raw = {"http_status": response.status_code, "body": parsed}
        if str(parsed.get("status", "")).upper() == "SUCCESS" and parsed.get("sessionkey"):
            self._remember_session(ref, str(parsed["sessionkey"]))
            result = self._result(
                instruction.instruction_id, ref, ConnectorStatus.PENDING, raw=raw
            )
            return self._idem.put(CONNECTOR_ID, ref, "SUBMIT", result)
        result = self._result(
            instruction.instruction_id,
            ref,
            ConnectorStatus.REJECTED,
            raw=raw,
            error_code="aggregator_session_rejected",
        )
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
        if self.session_key_for(connector_ref) is None:
            raw = {
                "connector_id": CONNECTOR_ID,
                "connector_ref": connector_ref,
                "status": "not_received",
            }
            return self._result(
                "", connector_ref, ConnectorStatus.FAILED, raw=raw, error_code="not_received"
            )
        query = canonical_json(
            {
                "tran_id": connector_ref,
                "store_id": self._cred("store_id"),
                "store_passwd": self._cred("store_passwd"),
            }
        )
        response = await self._transport.request(
            "POST",
            self._url("validate"),
            headers={"content-type": "application/json"},
            content=query,
        )
        parsed = parse_json_body(response.body)
        raw = {"http_status": response.status_code, "body": parsed}
        status, error_code = AGGREGATOR_STATUS_MAP.get(
            str(parsed.get("status", "PENDING")).upper(), (ConnectorStatus.PENDING, None)
        )
        result = self._result(
            "",
            connector_ref,
            status,
            raw=raw,
            rail_transaction_id=(
                str(parsed["bank_tran_id"]) if parsed.get("bank_tran_id") else None
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
            return recorded
        body = canonical_json(
            {
                "bank_tran_id": rail_transaction_id,
                "tran_id": connector_ref,
                "refund_amount": paisa_to_decimal_string(reverse_amount.amount_minor),
                "refund_remarks": reason,
                "store_id": self._cred("store_id"),
                "store_passwd": self._cred("store_passwd"),
            }
        )
        response = await self._transport.request(
            "POST", self._url("refund"), headers={"content-type": "application/json"}, content=body
        )
        parsed = parse_json_body(response.body)
        raw = {"http_status": response.status_code, "body": parsed}
        if str(parsed.get("status", "")).upper() in {"SUCCESS", "REFUNDED"}:
            result = self._result(
                "",
                connector_ref,
                ConnectorStatus.REVERSED,
                raw=raw,
                rail_transaction_id=(
                    str(parsed.get("bank_tran_id") or rail_transaction_id or "") or None
                ),
            )
            return self._idem.put(CONNECTOR_ID, connector_ref, "REVERSE", result)
        return self._result(
            "",
            connector_ref,
            ConnectorStatus.FAILED,
            raw=raw,
            error_code="aggregator_refund_failed",
        )

    async def health_check(self) -> bool:
        if self._mode is ConnectorMode.SIMULATOR:
            return await self._engine.health_check()
        if self._mode in (ConnectorMode.MANUAL_LOCAL, ConnectorMode.DISABLED):
            return self._mode is ConnectorMode.MANUAL_LOCAL
        try:
            self._cred("store_id")
            return True
        except Exception:
            return False

def _safe_json(response: WireResponse) -> dict:
    try:
        return parse_json_body(response.body)
    except Exception:
        return {"non_json_body_sha256": sha256(response.body).hexdigest()}


class RocketSyncStatusTruth:
    """Synchronous server-side validation-API truth source for IPN parsing.

    The live counterpart of :meth:`RocketAggregatorConnector.truth_for`: a
    forged or replayed IPN can never move money because the result handed to
    kernel is built from THIS validate answer (CERT-M1). Uses a sync httpx
    client because ``ConnectorWebhookHandler.parse_event`` is synchronous by
    SDK contract. Simulator/manual modes keep using
    :meth:`RocketAggregatorConnector.truth_for`.
    """

    def __init__(
        self,
        *,
        base_url: str,
        credentials: CredentialResolver,
        session_key_for,
        clock: Clock,
        credential_refs: dict | None = None,
        mode: ConnectorMode | str = ConnectorMode.SANDBOX,
        validate_path: str = _DEFAULT_PATHS["validate"],
        timeout_s: float = 10.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._credentials = credentials
        self._session_key_for = session_key_for
        self._clock = clock
        self._credential_refs = dict(credential_refs or {})
        self._mode = mode if isinstance(mode, ConnectorMode) else ConnectorMode(str(mode))
        self._validate_path = validate_path
        self._timeout_s = timeout_s

    def _cred(self, name: str) -> str:
        ref = self._credential_refs.get(
            name,
            f"openbao:secret/connectors/rocket_aggregator/{self._mode.value.lower()}/{name}",
        )
        return self._credentials.resolve(ref)

    def __call__(self, connector_ref: str) -> ConnectorResult | None:
        import httpx

        if self._session_key_for(connector_ref) is None:
            return None
        body = canonical_json(
            {
                "tran_id": connector_ref,
                "store_id": self._cred("store_id"),
                "store_passwd": self._cred("store_passwd"),
            }
        )
        with httpx.Client(timeout=self._timeout_s) as client:
            response = client.post(
                f"{self._base_url}{self._validate_path}",
                headers={"content-type": "application/json"},
                content=body,
            )
        parsed = _safe_json(WireResponse(response.status_code, response.content))
        status, error_code = AGGREGATOR_STATUS_MAP.get(
            str(parsed.get("status", "PENDING")).upper(), (ConnectorStatus.PENDING, None)
        )
        return ConnectorResult(
            instruction_id="",
            connector_ref=connector_ref,
            status=status,
            rail_transaction_id=(
                str(parsed["bank_tran_id"]) if parsed.get("bank_tran_id") else None
            ),
            responded_at=rfc3339(self._clock.now()),
            error_code=error_code,
            raw_response_hash=sha256_canonical(parsed),
        )

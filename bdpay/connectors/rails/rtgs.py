"""``rtgs_iso20022_v1`` — BD-RTGS adapter (spec/11 §C).

``sdk.PaymentConnector`` over ISO 20022 MX: pacs.008 build (C.1 element
table), structural fail-closed validation BEFORE every transmit, pacs.002
parse (C.2 taxonomy), camt.054 ingest matched by ``EndToEndId`` (=
``connector_ref``), and the BDT 1 lakh floor guard (>= 10,000,000 paisa —
``rtgs_below_minimum`` refusal).

RTGS finality discipline: once a message is accepted the rail is FINAL —
``reverse()`` NEVER builds wire traffic; it records a manual-ops-queue case
and returns ``FAILED``/``manual_queue_required`` (spec/10 budget
``reversal_policy=manual_ops_queue``; spec/11 FSM §4 MANUAL_QUEUE). A silent
rail (no pacs.002 through the poll budget) likewise ends in the manual queue,
never an auto-reversal.

Modes: SIMULATOR wires the in-process
:class:`~bdpay.connectors.simulators.rtgs_sim.RtgsSimulatorRail`; SANDBOX /
PRODUCTION wire :class:`HttpRtgsTransport` (httpx wire code over the
sponsor-bank H2H, mTLS via client-cert config — fully written, activates on
credentials config).
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256

import httpx

from bdpay.connectors.ids_ext import make_connector_id
from bdpay.connectors.ports import InMemoryEventSink, InMemoryObjectStore, raw_object_key
from bdpay.connectors.rails.rtgs_mx import (
    MxValidationError,
    build_pacs008,
    extract_camt054,
    extract_pacs002,
    validate_pacs008,
)
from bdpay.connectors.sdk import (
    ConnectorResult,
    ConnectorStatus,
    Money,
    PaymentInstruction,
)
from bdpay.platform.canonical import canonical_json, sha256_canonical
from bdpay.platform.clock import Clock

__all__ = [
    "CONNECTOR_ID",
    "HttpRtgsTransport",
    "RTGS_MINIMUM_PAISA",
    "RtgsConnector",
    "RtgsWebhookHandler",
    "SimulatorRtgsTransport",
    "map_pacs002",
]

CONNECTOR_ID = "rtgs_iso20022_v1"
SUPPORTED_METHODS = ("RTGS_CREDIT",)

#: BDT 1 lakh floor (research 02 §3): refuse below with rtgs_below_minimum.
RTGS_MINIMUM_PAISA = 10_000_000

SIGNATURE_HEADER = "x-sim-signature"
TIMESTAMP_HEADER = "x-sim-timestamp"


def map_pacs002(tx_status: str, reason_code: str | None) -> tuple[ConnectorStatus, str | None]:
    """C.2 binding mapping: TxSts -> (ConnectorStatus, error_code)."""
    if tx_status == "ACSP":
        return ConnectorStatus.PENDING, None
    if tx_status == "ACSC":
        return ConnectorStatus.SUCCESS, None
    if tx_status == "ACWC":
        return ConnectorStatus.SUCCESS, "rtgs_accepted_with_change"
    if tx_status == "RJCT":
        return ConnectorStatus.REJECTED, f"rtgs_rjct_{reason_code or 'NORSN'}"
    if tx_status == "PDNG":
        return ConnectorStatus.PENDING, None
    return ConnectorStatus.FAILED, f"rtgs_unmapped_status_{tx_status}"


class SimulatorRtgsTransport:
    """Transport port over the in-process simulator rail (SIMULATOR mode)."""

    def __init__(self, rail) -> None:
        self._rail = rail

    async def send(self, pacs008_xml: bytes) -> bytes:
        return await self._rail.send(pacs008_xml)

    async def poll(self, end_to_end_id: str) -> bytes | None:
        return await self._rail.poll(end_to_end_id)


class HttpRtgsTransport:
    """Sponsor-bank RTGS H2H transport (SANDBOX/PRODUCTION wire code).

    ``client`` is an ``httpx.AsyncClient`` pre-configured with ``base_url``
    and mTLS (client cert per registry ``mtls_client_cert_ref``); injecting
    the client keeps certificate handling in the wiring layer and makes the
    transport testable against ``httpx.MockTransport``.
    """

    def __init__(self, client, *, submit_path: str = "/v1/pacs008",
                 status_path: str = "/v1/status") -> None:
        self._client = client
        self._submit_path = submit_path
        self._status_path = status_path

    async def send(self, pacs008_xml: bytes) -> bytes:
        response = await self._client.post(
            self._submit_path,
            content=pacs008_xml,
            headers={"content-type": "application/xml"},
        )
        response.raise_for_status()
        return response.content

    async def poll(self, end_to_end_id: str) -> bytes | None:
        response = await self._client.get(f"{self._status_path}/{end_to_end_id}")
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.content or None


@dataclass
class _RtgsMessageRow:
    """One ``rtgs_messages`` flow row (in-memory; Postgres at wiring pass)."""

    rtgs_message_id: str
    msg_type: str
    direction: str
    mx_msg_id: str
    end_to_end_id: str
    flow_state: str
    amount_minor: int | None
    occurred_at: datetime


class RtgsConnector:
    """``sdk.PaymentConnector`` for BD-RTGS (all modes, one code path)."""

    connector_id = CONNECTOR_ID

    def __init__(
        self,
        *,
        transport,
        clock: Clock,
        object_store=None,
        events=None,
        debtor_name: str = "BD-PAY SETTLEMENT TSA",
        debtor_account: str = "TSA-BDPAY-01",
        debtor_agent_bic: str = "110000007",
        sim_hint: bool = False,
    ) -> None:
        self.supported_methods = SUPPORTED_METHODS
        self._transport = transport
        self._clock = clock
        self._objects = object_store if object_store is not None else InMemoryObjectStore()
        self._events = events if events is not None else InMemoryEventSink()
        self._debtor_name = debtor_name
        self._debtor_account = debtor_account
        self._debtor_agent_bic = debtor_agent_bic
        self._sim_hint = sim_hint
        self._recorded: dict[str, ConnectorResult] = {}
        self._instruction_by_ref: dict[str, str] = {}
        self.messages: list[_RtgsMessageRow] = []
        self.manual_queue: list[dict] = []

    # -- helpers ------------------------------------------------------------------------

    def _now_iso(self) -> str:
        return self._clock.now().strftime("%Y-%m-%dT%H:%M:%SZ")

    def _archive(self, raw: dict) -> str:
        raw_hash = sha256_canonical(raw)
        self._objects.put(raw_object_key(CONNECTOR_ID, raw_hash), canonical_json(raw))
        return raw_hash

    def _result(
        self,
        instruction_id: str,
        connector_ref: str,
        status: ConnectorStatus,
        *,
        rail_transaction_id: str | None,
        error_code: str | None,
        raw: dict,
    ) -> ConnectorResult:
        return ConnectorResult(
            instruction_id=instruction_id,
            connector_ref=connector_ref,
            status=status,
            rail_transaction_id=rail_transaction_id,
            responded_at=self._now_iso(),
            error_code=error_code,
            raw_response_hash=self._archive(raw),
        )

    def _synthetic(self, instruction_id: str, connector_ref: str,
                   status: ConnectorStatus, error_code: str) -> ConnectorResult:
        raw = {"synthetic": True, "connector_id": CONNECTOR_ID,
               "connector_ref": connector_ref, "status": status.value,
               "error_code": error_code}
        return self._result(instruction_id, connector_ref, status,
                            rail_transaction_id=None, error_code=error_code, raw=raw)

    def _msg_id_for(self, connector_ref: str) -> str:
        # Deterministic, <= 35 chars, no random ids (C.1 binding row).
        return make_connector_id("rtgsm", {"ref": connector_ref, "type": "pacs.008"})[6:]

    def _record_row(self, msg_type: str, direction: str, mx_msg_id: str,
                    end_to_end_id: str, flow_state: str,
                    amount_minor: int | None) -> None:
        self.messages.append(
            _RtgsMessageRow(
                rtgs_message_id=make_connector_id(
                    "rtgsm", {"msg_type": msg_type, "msg_id": mx_msg_id}
                ),
                msg_type=msg_type,
                direction=direction,
                mx_msg_id=mx_msg_id,
                end_to_end_id=end_to_end_id,
                flow_state=flow_state,
                amount_minor=amount_minor,
                occurred_at=self._clock.now(),
            )
        )

    def _handle_status_xml(self, instruction_id: str, connector_ref: str,
                           xml_bytes: bytes) -> ConnectorResult:
        raw = {"connector_id": CONNECTOR_ID, "wire_hex": xml_bytes.hex()}
        try:
            if b"FIToFIPmtStsRpt" in xml_bytes:
                parsed = extract_pacs002(xml_bytes)
                status, error_code = map_pacs002(
                    parsed["tx_status"], parsed.get("reason_code")
                )
            else:
                parsed = extract_camt054(xml_bytes)
                status, error_code = ConnectorStatus.SUCCESS, None
        except MxValidationError as exc:
            return self._result(
                instruction_id, connector_ref, ConnectorStatus.FAILED,
                rail_transaction_id=None, error_code="mx_schema_invalid",
                raw={**raw, "refusal": str(exc)},
            )
        if parsed["end_to_end_id"] != connector_ref:
            return self._result(
                instruction_id, connector_ref, ConnectorStatus.FAILED,
                rail_transaction_id=None, error_code="end_to_end_mismatch", raw=raw,
            )
        result = self._result(
            instruction_id, connector_ref, status,
            rail_transaction_id=self._msg_id_for(connector_ref),
            error_code=error_code, raw=raw,
        )
        if status in (ConnectorStatus.SUCCESS, ConnectorStatus.REJECTED):
            self._recorded[connector_ref] = result
        return result

    # -- PaymentConnector -----------------------------------------------------------------

    async def submit(self, instruction: PaymentInstruction) -> ConnectorResult:
        ref = instruction.connector_ref
        self._instruction_by_ref[ref] = instruction.instruction_id
        recorded = self._recorded.get(ref)
        if recorded is not None:
            return recorded  # idempotent on connector_ref; no second transmit
        if instruction.method not in self.supported_methods:
            return self._synthetic(instruction.instruction_id, ref,
                                   ConnectorStatus.REJECTED, "unsupported_method")
        amount_minor = instruction.amount.amount_minor
        if (isinstance(amount_minor, bool) or not isinstance(amount_minor, int)
                or amount_minor < RTGS_MINIMUM_PAISA):
            return self._synthetic(instruction.instruction_id, ref,
                                   ConnectorStatus.REJECTED, "rtgs_below_minimum")
        msg_id = self._msg_id_for(ref)
        sim_scenario = (
            instruction.metadata.get("sim_scenario") if self._sim_hint else None
        )
        xml_bytes = build_pacs008(
            msg_id=msg_id,
            end_to_end_id=ref,
            amount_minor=amount_minor,
            debtor_name=self._debtor_name,
            debtor_account=self._debtor_account,
            debtor_agent_bic=self._debtor_agent_bic,
            creditor_agent_bic=instruction.metadata.get("creditor_agent", "225000009"),
            creditor_name=instruction.metadata.get("creditor_name", "BENEFICIARY"),
            creditor_account=instruction.beneficiary_ref,
            remittance_info=instruction.instruction_id,
            created_at=self._clock.now(),
            sim_scenario=sim_scenario,
        )
        try:
            validate_pacs008(xml_bytes)  # fail-closed: never transmit invalid MX
        except MxValidationError as exc:
            self._record_row("pacs.008", "OUTBOUND", msg_id, ref, "BUILT", amount_minor)
            raw = {"connector_id": CONNECTOR_ID, "refusal": str(exc), "msg_id": msg_id}
            return self._result(instruction.instruction_id, ref, ConnectorStatus.FAILED,
                                rail_transaction_id=None,
                                error_code="mx_schema_invalid", raw=raw)
        self._record_row("pacs.008", "OUTBOUND", msg_id, ref, "SENT", amount_minor)
        try:
            response = await self._transport.send(xml_bytes)  # hard timeout = runner's
        except httpx.HTTPStatusError as exc:
            # CONN-H1: a 5xx can arrive AFTER the sponsor accepted the pacs.008.
            # RTGS is FINAL, so a server-side error MUST NOT be recorded as FAILED
            # (which would license a resubmit and diverge the ledger from a wire
            # that may already have settled). Treat 5xx as unresolved ->
            # PENDING/rtgs_status_unknown so query_status reconciles the true
            # outcome (mirrors query_status's finality handling). A 4xx is a
            # genuine pre-acceptance rejection and is left to propagate.
            if exc.response.status_code >= 500:
                raw = {
                    "connector_id": CONNECTOR_ID,
                    "connector_ref": ref,
                    "transport_status": exc.response.status_code,
                    "msg_id": msg_id,
                }
                return self._result(
                    instruction.instruction_id, ref, ConnectorStatus.PENDING,
                    rail_transaction_id=None, error_code="rtgs_status_unknown", raw=raw,
                )
            raise
        except (
            httpx.ReadTimeout,
            httpx.ReadError,
            httpx.WriteTimeout,
            httpx.WriteError,
            httpx.RemoteProtocolError,
        ) as exc:
            # CONN-H1 (AR-refined): ONLY post-send ambiguity maps to PENDING. These
            # errors occur after the request bytes were (at least partially) put on
            # the wire, so the sponsor may have accepted the pacs.008 — RTGS is final,
            # so never FAILED; PENDING/rtgs_status_unknown lets query_status reconcile.
            # Pre-connect failures (ConnectError/ConnectTimeout/PoolTimeout/ProxyError)
            # are DEFINITE non-submissions and must propagate as hard failures —
            # mapping them to PENDING would be fail-open on submission truth.
            raw = {
                "connector_id": CONNECTOR_ID,
                "connector_ref": ref,
                "transport_error": type(exc).__name__,
                "msg_id": msg_id,
            }
            return self._result(
                instruction.instruction_id, ref, ConnectorStatus.PENDING,
                rail_transaction_id=None, error_code="rtgs_status_unknown", raw=raw,
            )
        result = self._handle_status_xml(instruction.instruction_id, ref, response)
        self._events.emit("rtgs_message.sent",
                          {"rtgs_message_id": msg_id, "end_to_end_id": ref})
        return result

    async def query_status(
        self, connector_ref: str, rail_transaction_id: str | None
    ) -> ConnectorResult:
        instruction_id = self._instruction_by_ref.get(connector_ref, "")
        recorded = self._recorded.get(connector_ref)
        if recorded is not None:
            return recorded
        response = await self._transport.poll(connector_ref)
        if response is None:
            # The rail has nothing to say. RTGS money may have moved with
            # finality — NEVER report not_received (which would license a
            # resubmit); the unresolved state escalates to the manual queue.
            return self._synthetic(instruction_id, connector_ref,
                                   ConnectorStatus.PENDING, "rtgs_status_unknown")
        return self._handle_status_xml(instruction_id, connector_ref, response)

    async def reverse(
        self,
        connector_ref: str,
        rail_transaction_id: str | None,
        reverse_amount: Money,
        reason: str,
    ) -> ConnectorResult:
        """RTGS is final once accepted: no auto-reverse — manual ops queue."""
        instruction_id = self._instruction_by_ref.get(connector_ref, "")
        case = {
            "connector_ref": connector_ref,
            "rail_transaction_id": rail_transaction_id,
            "reverse_amount_minor": reverse_amount.amount_minor,
            "reason": reason,
            "queued_at": self._now_iso(),
        }
        self.manual_queue.append(case)
        self._events.emit(
            "rtgs_message.manual_queue",
            {"rtgs_message_id": rail_transaction_id or "",
             "end_to_end_id": connector_ref},
        )
        return self._synthetic(instruction_id, connector_ref,
                               ConnectorStatus.FAILED, "manual_queue_required")

    async def health_check(self) -> bool:
        try:
            await self._transport.poll("HEALTH-PROBE")
        except Exception:
            return False
        return True


class RtgsWebhookHandler:
    """Inbound pacs.002 / camt.054 over the bank channel (spec/11 C.3).

    Channel authentication is an HMAC over ``timestamp + '.' + body`` with
    the key behind the connector's ``webhook_verify_ref`` (simulator key in
    SIMULATOR mode, bank-channel key otherwise); anything unverifiable is
    REJECTED fail-closed. ``parse_event`` is XSD-hardened: DTD/entity
    declarations and structural violations are refusals.
    """

    connector_id = CONNECTOR_ID

    def __init__(self, adapter: RtgsConnector, *, key: bytes, clock: Clock,
                 max_skew_s: int = 300) -> None:
        self._adapter = adapter
        self._key = key
        self._clock = clock
        self._max_skew_s = max_skew_s

    def verify_signature(self, headers: dict, body: bytes) -> bool:
        signature = headers.get(SIGNATURE_HEADER)
        timestamp = headers.get(TIMESTAMP_HEADER)
        if not signature or not timestamp:
            return False
        try:
            stamped = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except ValueError:
            return False
        if stamped.tzinfo is None:
            return False
        if abs((self._clock.now() - stamped).total_seconds()) > self._max_skew_s:
            return False
        expected = hmac.new(
            self._key, timestamp.encode("utf-8") + b"." + body, sha256
        ).hexdigest()
        return hmac.compare_digest(expected, signature)

    def parse_event(self, headers: dict, body: bytes) -> ConnectorResult:
        if b"FIToFIPmtStsRpt" in body:
            parsed = extract_pacs002(body)
            status, error_code = map_pacs002(parsed["tx_status"], parsed.get("reason_code"))
        else:
            parsed = extract_camt054(body)
            status, error_code = ConnectorStatus.SUCCESS, None
        connector_ref = parsed["end_to_end_id"]
        result = ConnectorResult(
            instruction_id=self._adapter._instruction_by_ref.get(connector_ref, ""),
            connector_ref=connector_ref,
            status=status,
            rail_transaction_id=None,
            responded_at=None,
            error_code=error_code,
            raw_response_hash=sha256_canonical(
                {"connector_id": CONNECTOR_ID, "wire_hex": body.hex()}
            ),
        )
        if status in (ConnectorStatus.SUCCESS, ConnectorStatus.REJECTED):
            self._adapter._recorded[connector_ref] = result
        return result

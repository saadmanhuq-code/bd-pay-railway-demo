"""``card_acquirer_v1`` — card acquiring via acquiring-bank host (spec/11 §D).

Payment-facilitator model: ``submit()`` = authorization forwarded through the
VAULT-PROXY ONLY (``sender_ref`` is a vault token ``tok_…`` — a PAN-shaped
value is refused before anything leaves the process; the connector never
holds a clear PAN, CERT-03/04/R6). 3DS2: ARes ``tranStatus`` Y/A proceeds
frictionless; ``C`` returns ``PENDING`` with the challenge URL exposed via
``next_action()`` (kernel maps to REQUIRES_ACTION/redirect_to_url); N/R/U is
``REJECTED``/``threeds_failed``. The CRes arrives as an HMAC-verified webhook
(fail-closed). ``reverse()`` pre-capture = void; post-capture = refund
(partial refunds accumulate; over-refund refused). The CardAuth lifecycle FSM
(§FSM 5) is refusal-first: transitions outside the table raise.

The acquirer dialect is data: ``ACQUIRER_RESPONSE_MAP_V1`` (dictionary
``rail=CARD, name=acquirer_response_map, version=v1``) mirrors the A.4
semantics — an acquirer change is a dictionary release, not a code change.

Modes: SIMULATOR wires the in-process scripted issuer
(:class:`~bdpay.connectors.simulators.card_sim.CardSimulatorAcquirer`);
SANDBOX / PRODUCTION wire :class:`HttpAcquirerTransport` +
:class:`HttpVaultProxy` (httpx wire code, mTLS/HMAC per acquirer contract —
fully written, activates on credentials config).
"""

from __future__ import annotations

import hmac
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from hashlib import sha256

from bdpay.connectors.ports import InMemoryEventSink, InMemoryObjectStore, raw_object_key
from bdpay.connectors.rails.ports import VaultProxy
from bdpay.connectors.sdk import (
    ConnectorResult,
    ConnectorStatus,
    Money,
    PaymentInstruction,
)
from bdpay.platform.canonical import canonical_json, sha256_canonical
from bdpay.platform.clock import Clock

__all__ = [
    "ACQUIRER_RESPONSE_MAP_V1",
    "CONNECTOR_ID",
    "CardAcquirerConnector",
    "CardAuthRecord",
    "CardWebhookHandler",
    "HttpAcquirerTransport",
    "HttpVaultProxy",
    "LifecycleDenied",
    "is_vault_token",
]

CONNECTOR_ID = "card_acquirer_v1"
SUPPORTED_METHODS = ("CARD",)

SIGNATURE_HEADER = "x-sim-signature"
TIMESTAMP_HEADER = "x-sim-timestamp"

#: Challenge abandonment TTL (FSM §5: 30 minutes, injectable clock).
THREEDS_TTL = timedelta(minutes=30)
#: Authorization TTL default (spec/02 auth-expiry-sweep: 7 days).
AUTH_TTL = timedelta(days=7)

#: Acquirer response-code map v1 — mirrors spec/11 A.4 semantics (binding).
ACQUIRER_RESPONSE_MAP_V1: dict = {
    "rail": "CARD",
    "name": "acquirer_response_map",
    "version": "v1",
    "codes": {
        "00": ["success", None],
        "01": ["rejected", "refer_to_issuer"],
        "03": ["rejected", "invalid_merchant"],
        "04": ["rejected", "card_blocked"],
        "05": ["rejected", "do_not_honor"],
        "12": ["rejected", "invalid_transaction"],
        "13": ["rejected", "invalid_amount"],
        "14": ["rejected", "invalid_account"],
        "30": ["failed", "format_error"],
        "41": ["rejected", "lost_or_stolen"],
        "43": ["rejected", "lost_or_stolen"],
        "51": ["rejected", "insufficient_funds"],
        "54": ["rejected", "expired_card"],
        "57": ["rejected", "txn_not_permitted_to_holder"],
        "61": ["rejected", "exceeds_amount_limit"],
        "62": ["rejected", "restricted_card"],
        "65": ["rejected", "exceeds_frequency_limit"],
        "91": ["timed_out", "issuer_inoperative"],
        "96": ["failed", "system_malfunction"],
    },
}


def map_acquirer_code(code: str) -> tuple[ConnectorStatus, str | None]:
    entry = ACQUIRER_RESPONSE_MAP_V1["codes"].get(code)
    if entry is None:
        return ConnectorStatus.FAILED, f"card_unmapped_code_{code}"
    return ConnectorStatus(entry[0]), entry[1]


#: A contiguous 13-19 digit run is PAN-shaped (vault tokens are hex-suffixed:
#: scattered digits are fine, an embedded card-number run is not).
_PAN_RUN = re.compile(r"(?<!\d)\d{13,19}(?!\d)")


def is_vault_token(value: str) -> bool:
    """True only for vault-token-shaped refs; PAN-shaped values are refused."""
    if not isinstance(value, str) or not value.startswith("tok_"):
        return False
    return _PAN_RUN.search(value) is None  # 13-19 digit run inside a ref is PAN-shaped


# -- lifecycle FSM (spec/11 FSM §5; refusal-first) -------------------------------------------

_CARD_TABLE: dict[tuple[str, str], str] = {
    ("AUTH_REQUESTED", "approve"): "AUTH_APPROVED",
    ("AUTH_REQUESTED", "decline"): "AUTH_DECLINED",
    ("AUTH_REQUESTED", "challenge"): "THREEDS_PENDING",
    ("THREEDS_PENDING", "challenge_ok"): "AUTH_APPROVED",
    ("THREEDS_PENDING", "challenge_fail"): "AUTH_DECLINED",
    ("AUTH_APPROVED", "capture"): "CAPTURED",
    ("AUTH_APPROVED", "void"): "VOIDED",
    ("AUTH_APPROVED", "auth_expired"): "AUTH_EXPIRED",
    ("CAPTURED", "refund_partial"): "PARTIALLY_REFUNDED",
    ("CAPTURED", "refund_full"): "REFUNDED",
    ("PARTIALLY_REFUNDED", "refund_partial"): "PARTIALLY_REFUNDED",
    ("PARTIALLY_REFUNDED", "refund_full"): "REFUNDED",
}


class LifecycleDenied(RuntimeError):
    """Refusal-first: the card-auth transition is not in the table."""


@dataclass
class CardAuthRecord:
    """One ``card_auth_records`` row (in-memory; Postgres at wiring pass)."""

    connector_ref: str
    instruction_id: str
    pan_token: str
    state: str = "AUTH_REQUESTED"
    authorized_minor: int | None = None
    captured_minor: int = 0
    refunded_minor: int = 0
    acquirer_auth_code: str | None = None
    acquirer_rrn: str | None = None
    threeds_trans_id: str | None = None
    challenge_url: str | None = None
    challenge_started_at: datetime | None = None
    approved_at: datetime | None = None
    history: list[tuple[str, str, str]] = field(default_factory=list)

    def advance(self, trigger: str) -> str:
        to_state = _CARD_TABLE.get((self.state, trigger))
        if to_state is None:
            raise LifecycleDenied(
                f"card auth transition {self.state} --[{trigger}]--> is DENIED"
            )
        self.history.append((self.state, trigger, to_state))
        self.state = to_state
        return to_state


# -- live transports (SANDBOX/PRODUCTION wire code) -------------------------------------------


class HttpVaultProxy:
    """``POST vault-proxy:/detokenize-and-forward`` (mTLS, inside the CDE).

    ``client`` is a pre-configured ``httpx.AsyncClient`` (base_url = the
    vault-proxy, client certs from wiring); testable via MockTransport.
    """

    def __init__(self, client, *, path: str = "/detokenize-and-forward") -> None:
        self._client = client
        self._path = path

    async def detokenize_and_forward(
        self, pan_token: str, operation: str, payload: dict
    ) -> dict:
        response = await self._client.post(
            self._path,
            json={"pan_token": pan_token, "operation": operation, "payload": payload},
        )
        response.raise_for_status()
        return response.json()


class HttpAcquirerTransport:
    """Acquiring-bank host API for the non-PAN operations (capture/void/refund).

    Requests are HMAC-signed per the acquirer contract: header
    ``x-acq-signature`` = HMAC-SHA256(key, canonical_json(body)).
    """

    def __init__(self, client, *, hmac_key: bytes,
                 path_by_op: dict[str, str] | None = None) -> None:
        self._client = client
        self._key = hmac_key
        self._paths = dict(path_by_op or {
            "capture": "/v1/captures",
            "void": "/v1/voids",
            "refund": "/v1/refunds",
            "status": "/v1/status",
        })

    async def request(self, operation: str, payload: dict) -> dict:
        body = canonical_json(payload)
        signature = hmac.new(self._key, body, sha256).hexdigest()
        response = await self._client.post(
            self._paths[operation],
            content=body,
            headers={"content-type": "application/json",
                     "x-acq-signature": signature},
        )
        response.raise_for_status()
        return response.json()


class SimulatorCardTransport:
    """Both ports over the in-process scripted issuer (SIMULATOR mode)."""

    def __init__(self, acquirer) -> None:
        self._acquirer = acquirer

    async def detokenize_and_forward(self, pan_token: str, operation: str,
                                     payload: dict) -> dict:
        return await self._acquirer.authorize(dict(payload, pan_token_hash=sha256(
            pan_token.encode("utf-8")).hexdigest()[:16]))

    async def request(self, operation: str, payload: dict) -> dict:
        return await self._acquirer.request(operation, payload)


# -- the adapter ---------------------------------------------------------------------------


class CardAcquirerConnector:
    """``sdk.PaymentConnector`` for card acquiring (all modes, one code path)."""

    connector_id = CONNECTOR_ID

    def __init__(
        self,
        *,
        vault_proxy: VaultProxy,
        acquirer,
        clock: Clock,
        object_store=None,
        events=None,
        sim_hint: bool = False,
    ) -> None:
        self.supported_methods = SUPPORTED_METHODS
        self._vault = vault_proxy
        self._acquirer = acquirer
        self._clock = clock
        self._objects = object_store if object_store is not None else InMemoryObjectStore()
        self._events = events if events is not None else InMemoryEventSink()
        self._sim_hint = sim_hint
        self._records: dict[str, CardAuthRecord] = {}
        self._submit_results: dict[str, ConnectorResult] = {}
        self._reversal_results: dict[str, ConnectorResult] = {}

    # -- helpers --------------------------------------------------------------------------

    def _now_iso(self) -> str:
        return self._clock.now().strftime("%Y-%m-%dT%H:%M:%SZ")

    def _archive(self, raw: dict) -> str:
        raw_hash = sha256_canonical(raw)
        self._objects.put(raw_object_key(CONNECTOR_ID, raw_hash), canonical_json(raw))
        return raw_hash

    def _result(self, instruction_id: str, connector_ref: str, status: ConnectorStatus,
                *, rail_transaction_id: str | None, error_code: str | None,
                raw: dict) -> ConnectorResult:
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

    def record(self, connector_ref: str) -> CardAuthRecord:
        record = self._records.get(connector_ref)
        if record is None:
            raise KeyError(f"no card auth record for {connector_ref}")
        return record

    def next_action(self, connector_ref: str) -> dict | None:
        """3DS challenge handoff payload for kernel REQUIRES_ACTION mapping."""
        record = self._records.get(connector_ref)
        if record is None or record.state != "THREEDS_PENDING":
            return None
        return {"type": "redirect_to_url", "redirect_to_url": record.challenge_url,
                "threeds_trans_id": record.threeds_trans_id}

    # -- PaymentConnector --------------------------------------------------------------------

    async def submit(self, instruction: PaymentInstruction) -> ConnectorResult:
        ref = instruction.connector_ref
        recorded = self._submit_results.get(ref)
        if recorded is not None:
            return recorded  # idempotent on connector_ref
        if instruction.method not in self.supported_methods:
            return self._synthetic(instruction.instruction_id, ref,
                                   ConnectorStatus.REJECTED, "unsupported_method")
        if not is_vault_token(instruction.sender_ref):
            # PAN-shaped or non-token sender_ref never leaves the process.
            return self._synthetic(instruction.instruction_id, ref,
                                   ConnectorStatus.REJECTED, "pan_blocked")
        amount_minor = instruction.amount.amount_minor
        if (isinstance(amount_minor, bool) or not isinstance(amount_minor, int)
                or amount_minor <= 0):
            return self._synthetic(instruction.instruction_id, ref,
                                   ConnectorStatus.REJECTED, "invalid_amount")
        record = CardAuthRecord(
            connector_ref=ref,
            instruction_id=instruction.instruction_id,
            pan_token=instruction.sender_ref,
        )
        self._records[ref] = record
        payload = {
            "connector_ref": ref,
            "amount_minor": amount_minor,
            "currency": instruction.amount.currency,
            "merchant_ref": instruction.beneficiary_ref,
        }
        if self._sim_hint and instruction.metadata.get("sim_scenario"):
            payload["sim_scenario"] = instruction.metadata["sim_scenario"]
        response = await self._vault.detokenize_and_forward(
            instruction.sender_ref, "authorize", payload
        )
        raw = {"connector_id": CONNECTOR_ID, "response": response}
        result = self._apply_auth_response(instruction, record, response, raw)
        if result.status in (ConnectorStatus.SUCCESS, ConnectorStatus.PENDING,
                             ConnectorStatus.REJECTED):
            self._submit_results[ref] = result
        return result

    def _apply_auth_response(self, instruction: PaymentInstruction,
                             record: CardAuthRecord, response: dict,
                             raw: dict) -> ConnectorResult:
        outcome = response.get("result")
        tran_status = (response.get("threeds") or {}).get("tran_status")
        if outcome == "challenge":
            if tran_status != "C":
                record.advance("decline")
                return self._result(instruction.instruction_id, record.connector_ref,
                                    ConnectorStatus.REJECTED,
                                    rail_transaction_id=response.get("acquirer_rrn"),
                                    error_code="threeds_failed", raw=raw)
            record.advance("challenge")
            record.threeds_trans_id = (response.get("threeds") or {}).get("threeds_trans_id")
            record.challenge_url = (response.get("threeds") or {}).get("acs_url")
            record.challenge_started_at = self._clock.now()
            self._events.emit("card_auth.threeds_challenge",
                              {"connector_ref": record.connector_ref})
            return self._result(instruction.instruction_id, record.connector_ref,
                                ConnectorStatus.PENDING,
                                rail_transaction_id=response.get("acquirer_rrn"),
                                error_code=None, raw=raw)
        if tran_status in ("N", "R", "U"):
            record.advance("decline")
            return self._result(instruction.instruction_id, record.connector_ref,
                                ConnectorStatus.REJECTED,
                                rail_transaction_id=response.get("acquirer_rrn"),
                                error_code="threeds_failed", raw=raw)
        status, error_code = map_acquirer_code(str(response.get("response_code", "")))
        if status is ConnectorStatus.SUCCESS:
            record.advance("approve")
            record.authorized_minor = instruction.amount.amount_minor
            record.acquirer_auth_code = response.get("auth_code")
            record.acquirer_rrn = response.get("acquirer_rrn")
            record.approved_at = self._clock.now()
            if instruction.metadata.get("auto_capture") == "true":
                record.advance("capture")
                record.captured_minor = record.authorized_minor
        elif status is ConnectorStatus.REJECTED:
            record.advance("decline")
        return self._result(instruction.instruction_id, record.connector_ref, status,
                            rail_transaction_id=response.get("acquirer_rrn"),
                            error_code=error_code, raw=raw)

    async def capture(self, connector_ref: str, amount: Money) -> ConnectorResult:
        """Capture an approved auth (guard: amount <= authorized; FSM §5)."""
        record = self._records.get(connector_ref)
        if record is None:
            return self._synthetic("", connector_ref, ConnectorStatus.FAILED,
                                   "not_received")
        if record.state != "AUTH_APPROVED":
            return self._synthetic(record.instruction_id, connector_ref,
                                   ConnectorStatus.REJECTED, "capture_state_denied")
        if amount.amount_minor > (record.authorized_minor or 0):
            return self._synthetic(record.instruction_id, connector_ref,
                                   ConnectorStatus.REJECTED, "capture_exceeds_authorized")
        response = await self._acquirer.request(
            "capture",
            {"connector_ref": connector_ref, "amount_minor": amount.amount_minor,
             "acquirer_rrn": record.acquirer_rrn},
        )
        raw = {"connector_id": CONNECTOR_ID, "response": response}
        status, error_code = map_acquirer_code(str(response.get("response_code", "")))
        if status is ConnectorStatus.SUCCESS:
            record.advance("capture")
            record.captured_minor = amount.amount_minor
        return self._result(record.instruction_id, connector_ref, status,
                            rail_transaction_id=record.acquirer_rrn,
                            error_code=error_code, raw=raw)

    async def query_status(
        self, connector_ref: str, rail_transaction_id: str | None
    ) -> ConnectorResult:
        record = self._records.get(connector_ref)
        if record is None:
            return self._synthetic("", connector_ref, ConnectorStatus.FAILED,
                                   "not_received")
        state_to_status = {
            "AUTH_REQUESTED": (ConnectorStatus.PENDING, None),
            "THREEDS_PENDING": (ConnectorStatus.PENDING, None),
            "AUTH_APPROVED": (ConnectorStatus.SUCCESS, None),
            "CAPTURED": (ConnectorStatus.SUCCESS, None),
            "PARTIALLY_REFUNDED": (ConnectorStatus.SUCCESS, None),
            "REFUNDED": (ConnectorStatus.REVERSED, None),
            "VOIDED": (ConnectorStatus.REVERSED, None),
            "AUTH_DECLINED": (ConnectorStatus.REJECTED, "do_not_honor"),
            "AUTH_EXPIRED": (ConnectorStatus.REVERSED, "auth_expired"),
        }
        status, error_code = state_to_status[record.state]
        raw = {"connector_id": CONNECTOR_ID, "connector_ref": connector_ref,
               "state": record.state, "query": True}
        return self._result(record.instruction_id, connector_ref, status,
                            rail_transaction_id=record.acquirer_rrn,
                            error_code=error_code, raw=raw)

    async def reverse(
        self,
        connector_ref: str,
        rail_transaction_id: str | None,
        reverse_amount: Money,
        reason: str,
    ) -> ConnectorResult:
        record = self._records.get(connector_ref)
        if record is None:
            return self._synthetic("", connector_ref, ConnectorStatus.FAILED,
                                   "not_received")
        recorded = self._reversal_results.get(connector_ref)
        if recorded is not None and record.state in ("VOIDED", "REFUNDED"):
            return recorded  # idempotent replay of a terminal reversal
        if record.state == "AUTH_APPROVED":
            response = await self._acquirer.request(
                "void", {"connector_ref": connector_ref,
                         "acquirer_rrn": record.acquirer_rrn, "reason": reason},
            )
            raw = {"connector_id": CONNECTOR_ID, "response": response}
            status, error_code = map_acquirer_code(str(response.get("response_code", "")))
            if status is ConnectorStatus.SUCCESS:
                record.advance("void")
                result = self._result(record.instruction_id, connector_ref,
                                      ConnectorStatus.REVERSED,
                                      rail_transaction_id=record.acquirer_rrn,
                                      error_code=None, raw=raw)
                self._reversal_results[connector_ref] = result
                return result
            return self._result(record.instruction_id, connector_ref, status,
                                rail_transaction_id=record.acquirer_rrn,
                                error_code=error_code, raw=raw)
        if record.state in ("CAPTURED", "PARTIALLY_REFUNDED"):
            remaining = record.captured_minor - record.refunded_minor
            if reverse_amount.amount_minor > remaining:
                return self._synthetic(record.instruction_id, connector_ref,
                                       ConnectorStatus.REJECTED, "refund_exceeds_captured")
            response = await self._acquirer.request(
                "refund", {"connector_ref": connector_ref,
                           "amount_minor": reverse_amount.amount_minor,
                           "acquirer_rrn": record.acquirer_rrn, "reason": reason},
            )
            raw = {"connector_id": CONNECTOR_ID, "response": response}
            status, error_code = map_acquirer_code(str(response.get("response_code", "")))
            if status is ConnectorStatus.SUCCESS:
                record.refunded_minor += reverse_amount.amount_minor
                trigger = ("refund_full" if record.refunded_minor == record.captured_minor
                           else "refund_partial")
                record.advance(trigger)
                result = self._result(record.instruction_id, connector_ref,
                                      ConnectorStatus.REVERSED,
                                      rail_transaction_id=record.acquirer_rrn,
                                      error_code=None, raw=raw)
                if record.state == "REFUNDED":
                    self._reversal_results[connector_ref] = result
                return result
            return self._result(record.instruction_id, connector_ref, status,
                                rail_transaction_id=record.acquirer_rrn,
                                error_code=error_code, raw=raw)
        return self._synthetic(record.instruction_id, connector_ref,
                               ConnectorStatus.REJECTED, "reverse_state_denied")

    async def health_check(self) -> bool:
        try:
            await self._acquirer.request("status", {"probe": "health"})
        except Exception:
            return False
        return True

    # -- 3DS lifecycle sweeps (injectable clock) ------------------------------------------------

    def apply_cres(self, connector_ref: str, tran_status: str) -> ConnectorResult:
        """Challenge completion (CRes via webhook): Y -> approved, else declined."""
        record = self.record(connector_ref)
        raw = {"connector_id": CONNECTOR_ID, "connector_ref": connector_ref,
               "event": "cres", "tran_status": tran_status}
        if record.state != "THREEDS_PENDING":
            return self._result(record.instruction_id, connector_ref,
                                ConnectorStatus.FAILED,
                                rail_transaction_id=record.acquirer_rrn,
                                error_code="cres_state_denied", raw=raw)
        if tran_status == "Y":
            record.advance("challenge_ok")
            record.approved_at = self._clock.now()
            if record.authorized_minor is None:
                record.authorized_minor = 0
            result = self._result(record.instruction_id, connector_ref,
                                  ConnectorStatus.SUCCESS,
                                  rail_transaction_id=record.acquirer_rrn,
                                  error_code=None, raw=raw)
            self._submit_results[connector_ref] = result
            return result
        record.advance("challenge_fail")
        result = self._result(record.instruction_id, connector_ref,
                              ConnectorStatus.REJECTED,
                              rail_transaction_id=record.acquirer_rrn,
                              error_code="threeds_failed", raw=raw)
        self._submit_results[connector_ref] = result
        return result

    def expire_challenges(self) -> list[str]:
        """30-minute 3DS abandonment sweep -> AUTH_DECLINED/threeds_failed."""
        expired: list[str] = []
        now = self._clock.now()
        for record in self._records.values():
            if (record.state == "THREEDS_PENDING"
                    and record.challenge_started_at is not None
                    and now - record.challenge_started_at >= THREEDS_TTL):
                record.advance("challenge_fail")
                expired.append(record.connector_ref)
        return expired

    def expire_auths(self) -> list[str]:
        """7-day uncaptured-auth sweep -> AUTH_EXPIRED (reversed taxonomy)."""
        expired: list[str] = []
        now = self._clock.now()
        for record in self._records.values():
            if (record.state == "AUTH_APPROVED"
                    and record.approved_at is not None
                    and now - record.approved_at >= AUTH_TTL):
                record.advance("auth_expired")
                expired.append(record.connector_ref)
        return expired


class CardWebhookHandler:
    """Acquirer webhooks (CRes and async advices) — HMAC fail-closed."""

    connector_id = CONNECTOR_ID

    def __init__(self, adapter: CardAcquirerConnector, *, key: bytes, clock: Clock,
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
        payload = json.loads(body.decode("utf-8"))
        connector_ref = str(payload["connector_ref"])
        if payload.get("event") == "cres":
            return self._adapter.apply_cres(connector_ref, str(payload["tran_status"]))
        raise ValueError(f"unknown card webhook event {payload.get('event')!r}")

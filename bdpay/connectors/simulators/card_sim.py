"""Deterministic card acquiring-host simulator (spec/11 §F card scenarios).

The simulator IS the acquirer endpoint behind the vault-proxy: ``authorize``
receives the payload the proxy forwarded — carrying a ``pan_token_hash``,
NEVER a PAN (a 13-19 digit run anywhere in the payload is refused with a
format-error response, fail-closed) — and answers with the acquirer response
dialect that :data:`~bdpay.connectors.rails.card.ACQUIRER_RESPONSE_MAP_V1`
maps. ``request`` serves the non-PAN operations (capture/void/refund/status).

Scenario selection: ``sim_scenario`` in the forwarded payload (injected by
the adapter ONLY in SIMULATOR mode); absent a hint the issuer approves
(response_code 00).

Scenarios (spec/11 §F card table):

- ``success`` / default: frictionless approval (00, auth code, RRN).
- ``decline``: 51 insufficient funds, definitive.
- ``timeout``: the host never replies (the runner's hard timeout fires).
- ``threeds_challenge_flow``: ARes tranStatus=C with challenge URL; a SIGNED
  CRes callback (tranStatus=Y) is queued for async delivery through the
  spec/10 webhook pipeline — challenge completion approves the auth.
- ``threeds_failed``: ARes tranStatus=N, definitive decline.
- ``void_after_auth`` / ``partial_refund``: approve the auth; lifecycle
  coverage is driven through ``request`` (void/refund answered 00).

Duplicate ``connector_ref`` authorizations return the recorded response with
no second issuer effect (idempotency on the rail side).
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256

from bdpay.connectors.simulator import sign_callback
from bdpay.platform.canonical import canonical_json
from bdpay.platform.clock import Clock

__all__ = ["CardSimulatorAcquirer", "SimVaultProxy"]

_PAN_SHAPED = re.compile(r"(?<!\d)\d{13,19}(?!\d)")

_THREEDS_SCENARIOS = frozenset({"threeds_challenge_flow"})
_SILENT_SCENARIOS = frozenset({"timeout"})


class SimVaultProxy:
    """SIMULATOR-mode vault-proxy port (spec/14 surface, in-process).

    Mirrors the CDE boundary contract of ``HttpVaultProxy``: only vault-token
    shaped references cross; the proxy "detokenizes" by replacing the token
    with its hash before forwarding — the simulator host, like the real
    acquirer-facing proxy, never hands a PAN back to the connector process.
    A PAN-shaped reference is refused fail-closed.
    """

    def __init__(self, acquirer: CardSimulatorAcquirer) -> None:
        self._acquirer = acquirer

    async def detokenize_and_forward(
        self, pan_token: str, operation: str, payload: dict
    ) -> dict:
        if not pan_token.startswith("tok_") or _PAN_SHAPED.search(pan_token):
            raise ValueError("vault-proxy refuses non-token references (fail-closed)")
        forwarded = dict(payload)
        forwarded["pan_token_hash"] = sha256(
            pan_token.encode("utf-8")
        ).hexdigest()[:16]
        if operation == "authorize":
            return await self._acquirer.authorize(forwarded)
        return await self._acquirer.request(operation, forwarded)


@dataclass
class _DueCallback:
    due_at: datetime
    sequence: int
    body: bytes


@dataclass
class _IssuerRecord:
    """Issuer-side truth for one connector_ref."""

    connector_ref: str
    acquirer_rrn: str
    scenario: str
    amount_minor: int
    approved: bool = False
    voided: bool = False
    captured_minor: int = 0
    refunded_minor: int = 0


class CardSimulatorAcquirer:
    """Deterministic acquiring-host endpoint for ``card_acquirer_v1``."""

    def __init__(self, connector_id: str, *, clock: Clock,
                 signing_key: bytes = b"card-sim-channel-key") -> None:
        self.connector_id = connector_id
        self._clock = clock
        self._signing_key = signing_key
        self._records: dict[str, _IssuerRecord] = {}
        self._responses: dict[str, dict] = {}
        self._effects: dict[str, int] = {}
        self._queue: list[_DueCallback] = []
        self._sequence = 0
        self._callback_sink = None

    # -- wiring -----------------------------------------------------------------

    def bind_callback_sink(self, sink) -> None:
        """``sink(connector_id, headers, body)`` — usually ``pipeline.ingest``."""
        self._callback_sink = sink

    def effect_count(self, connector_ref: str) -> int:
        return self._effects.get(connector_ref, 0)

    # -- helpers ----------------------------------------------------------------

    @staticmethod
    async def _never() -> None:
        await asyncio.Event().wait()

    @staticmethod
    def _refuse_pan_shaped(payload: dict) -> bool:
        """True when any string in the payload carries a 13-19 digit run."""
        for value in payload.values():
            if isinstance(value, str) and _PAN_SHAPED.search(value):
                return True
        return False

    @staticmethod
    def _rrn_for(connector_ref: str) -> str:
        return "CRD" + sha256(connector_ref.encode("utf-8")).hexdigest()[:9].upper()

    def _record_effect(self, connector_ref: str) -> None:
        self._effects[connector_ref] = self._effects.get(connector_ref, 0) + 1

    # -- the authorize port (reached ONLY through the vault-proxy) ----------------

    async def authorize(self, payload: dict) -> dict:
        connector_ref = str(payload.get("connector_ref", ""))
        recorded = self._responses.get(connector_ref)
        if recorded is not None:
            return dict(recorded)  # duplicate authorization: no second effect
        if "pan_token_hash" not in payload or self._refuse_pan_shaped(payload):
            # The CDE boundary holds: anything PAN-shaped is refused outright.
            return {"result": "error", "response_code": "30",
                    "reason": "pan_shaped_or_untokenized_request_refused"}
        scenario = str(payload.get("sim_scenario") or "success")
        if scenario in _SILENT_SCENARIOS:
            await self._never()
        rrn = self._rrn_for(connector_ref)
        amount_minor = int(payload.get("amount_minor", 0))
        record = _IssuerRecord(connector_ref=connector_ref, acquirer_rrn=rrn,
                               scenario=scenario, amount_minor=amount_minor)
        self._records[connector_ref] = record
        if scenario == "decline":
            response = {"result": "auth", "response_code": "51", "acquirer_rrn": rrn}
        elif scenario == "threeds_failed":
            response = {
                "result": "auth", "response_code": "05", "acquirer_rrn": rrn,
                "threeds": {"tran_status": "N"},
            }
        elif scenario in _THREEDS_SCENARIOS:
            response = {
                "result": "challenge",
                "acquirer_rrn": rrn,
                "threeds": {
                    "tran_status": "C",
                    "threeds_trans_id": f"3DS-{rrn}",
                    "acs_url": f"https://acs.sim.local/challenge/{rrn}",
                },
            }
            self._enqueue_cres(connector_ref, due_at=self._clock.now() + timedelta(seconds=15))
        else:  # success (mandatory fallback) — covers void/refund lifecycles
            record.approved = True
            response = {
                "result": "auth", "response_code": "00",
                "auth_code": f"A{rrn[3:8]}", "acquirer_rrn": rrn,
                "threeds": {"tran_status": "Y"},
            }
        self._record_effect(connector_ref)
        self._responses[connector_ref] = response
        return dict(response)

    # -- the non-PAN operations port ----------------------------------------------

    async def request(self, operation: str, payload: dict) -> dict:
        if operation == "status":
            return {"response_code": "00", "host": "card-sim", "signed_on": True}
        connector_ref = str(payload.get("connector_ref", ""))
        record = self._records.get(connector_ref)
        if record is None:
            return {"response_code": "25"}  # unable to locate record
        if operation == "capture":
            record.captured_minor = int(payload.get("amount_minor", 0))
            return {"response_code": "00", "acquirer_rrn": record.acquirer_rrn}
        if operation == "void":
            if record.voided:
                return {"response_code": "00", "acquirer_rrn": record.acquirer_rrn}
            record.voided = True
            return {"response_code": "00", "acquirer_rrn": record.acquirer_rrn}
        if operation == "refund":
            amount = int(payload.get("amount_minor", 0))
            if record.refunded_minor + amount > record.captured_minor:
                return {"response_code": "13"}  # invalid amount: over-refund
            record.refunded_minor += amount
            return {"response_code": "00", "acquirer_rrn": record.acquirer_rrn}
        return {"response_code": "12"}  # invalid transaction (unknown operation)

    # -- async CRes delivery ---------------------------------------------------------

    def _enqueue_cres(self, connector_ref: str, *, due_at: datetime) -> None:
        body = canonical_json({
            "event": "cres",
            "connector_ref": connector_ref,
            "tran_status": "Y",
        })
        self._sequence += 1
        self._queue.append(_DueCallback(due_at=due_at, sequence=self._sequence, body=body))

    async def deliver_due_callbacks(self) -> int:
        """Deliver queued CRes callbacks with ``due_at <= clock.now()``."""
        if self._callback_sink is None:
            raise RuntimeError("no callback sink bound; call bind_callback_sink first")
        now = self._clock.now()
        due = sorted(
            (cb for cb in self._queue if cb.due_at <= now),
            key=lambda cb: (cb.due_at, cb.sequence),
        )
        for callback in due:
            self._queue.remove(callback)
            timestamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
            headers = sign_callback(self._signing_key, callback.body, timestamp)
            await self._callback_sink(self.connector_id, headers, callback.body)
        return len(due)

    def pending_callbacks(self) -> int:
        return len(self._queue)

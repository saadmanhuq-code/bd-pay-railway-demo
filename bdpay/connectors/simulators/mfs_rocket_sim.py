"""Deterministic Rocket-via-aggregator host simulator (spec/12 §C, §K).

A fake licensed-PSO-aggregator HOST (SSLCommerz/AamarPay surface shape) behind
the ``WireTransport`` port: session create -> validate -> refund, so the REAL
``RocketAggregatorConnector`` SANDBOX/PRODUCTION wire code runs unmodified.
Store credentials are checked on every call (an aggregator never serves an
unauthenticated merchant); session keys / bank_tran_ids are content-addressed
from ``tran_id``; customer outcomes are scripted via ``settle()`` — VALID /
FAILED / CANCELLED / UNATTEMPTED — never random.

``DownAggregatorTransport`` raises on every call: the transport-failure leg of
the spec/12 §K ``aggregator_down_manual_local`` scenario (the connector's
designed contingency is MANUAL_LOCAL mode with two-eyes manual results, which
tests drive end-to-end through ``ManualLocalState``).
"""

from __future__ import annotations

import hashlib

from bdpay.connectors.mfs.wire import WireResponse, parse_json_body
from bdpay.platform.canonical import canonical_json
from bdpay.platform.clock import Clock

__all__ = ["DownAggregatorTransport", "RocketAggregatorSimulatorGateway"]


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class DownAggregatorTransport:
    """Transport whose every exchange fails — the aggregator-outage leg."""

    def __init__(self) -> None:
        self.attempts = 0

    async def request(
        self, method: str, url: str, *, headers: dict, content: bytes
    ) -> WireResponse:
        self.attempts += 1
        raise ConnectionError("simulated aggregator outage")


class RocketAggregatorSimulatorGateway:
    """Deterministic aggregator endpoint behind the ``WireTransport`` port."""

    def __init__(
        self,
        *,
        clock: Clock,
        store_id: str = "store-sim",
        store_passwd: str = "store-secret",
    ) -> None:
        self._clock = clock
        self._store_id = store_id
        self._store_passwd = store_passwd
        self._sessions: dict[str, str] = {}  # tran_id -> sessionkey
        self._status: dict[str, str] = {}  # tran_id -> aggregator status
        self._refunded: set[str] = set()
        self._reject_sessions: set[str] = set()
        self.validate_calls: list[str] = []

    # -- scripting -----------------------------------------------------------------

    def settle(self, tran_id: str, status: str) -> None:
        """Script the customer outcome: VALID | VALIDATED | FAILED | CANCELLED."""
        self._status[tran_id] = status

    def reject_session(self, tran_id: str) -> None:
        """Force session creation to fail for one transaction id."""
        self._reject_sessions.add(tran_id)

    # -- the wire -----------------------------------------------------------------------

    async def request(
        self, method: str, url: str, *, headers: dict, content: bytes
    ) -> WireResponse:
        body = parse_json_body(content)
        if not self._authenticated(body):
            return _json_response(
                200, {"status": "FAILED", "failedreason": "store credential invalid"}
            )
        if url.endswith("/gwprocess/v4/api.php"):
            return self._create_session(body)
        if url.endswith("/validator/api/validationserverAPI.php"):
            return self._validate(body)
        if url.endswith("/validator/api/merchantTransIDvalidationAPI.php"):
            return self._refund(body)
        return _json_response(404, {"status": "FAILED", "failedreason": "unknown endpoint"})

    def _authenticated(self, body: dict) -> bool:
        return (
            str(body.get("store_id", "")) == self._store_id
            and str(body.get("store_passwd", "")) == self._store_passwd
        )

    def _create_session(self, body: dict) -> WireResponse:
        tran_id = str(body.get("tran_id", ""))
        if not tran_id or tran_id in self._reject_sessions:
            return _json_response(
                200, {"status": "FAILED", "failedreason": "session refused"}
            )
        session_key = self._sessions.setdefault(tran_id, f"SES{_sha(tran_id)[:12]}")
        self._status.setdefault(tran_id, "UNATTEMPTED")
        return _json_response(
            200,
            {
                "status": "SUCCESS",
                "sessionkey": session_key,
                "GatewayPageURL": f"https://sim.aggregator/gw/{session_key}",
            },
        )

    def _validate(self, body: dict) -> WireResponse:
        tran_id = str(body.get("tran_id", ""))
        self.validate_calls.append(tran_id)
        if tran_id not in self._sessions:
            return _json_response(200, {"status": "INVALID_TRANSACTION"})
        status = self._status.get(tran_id, "UNATTEMPTED")
        payload: dict = {"status": status, "tran_id": tran_id}
        if status in {"VALID", "VALIDATED"}:
            payload["bank_tran_id"] = f"BTX{_sha(tran_id)[:10]}"
            payload["card_issuer"] = "ROCKET-DBBL"
        return _json_response(200, payload)

    def _refund(self, body: dict) -> WireResponse:
        tran_id = str(body.get("tran_id", ""))
        if self._status.get(tran_id) not in {"VALID", "VALIDATED"}:
            return _json_response(
                200, {"status": "FAILED", "errorReason": "nothing to refund"}
            )
        if tran_id in self._refunded:
            return _json_response(
                200,
                {
                    "status": "REFUNDED",
                    "tran_id": tran_id,
                    "bank_tran_id": f"BTX{_sha(tran_id)[:10]}",
                },
            )
        self._refunded.add(tran_id)
        return _json_response(
            200,
            {
                "status": "SUCCESS",
                "tran_id": tran_id,
                "bank_tran_id": f"BTX{_sha(tran_id)[:10]}",
                "refund_ref_id": f"RFD{_sha(tran_id)[:10]}",
            },
        )


def _json_response(status_code: int, payload: dict) -> WireResponse:
    return WireResponse(status_code=status_code, body=canonical_json(payload))

"""Deterministic BD-RTGS rail-host simulator (spec/11 §F rtgs scenarios).

Accepts real pacs.008 XML (validated with the SAME structural validator the
adapter uses — a message the adapter would refuse is refused here too),
answers with real pacs.002 XML, and emits camt.054 credit notifications
through the callback queue. Scenario selection: a second ``RmtInf/Ustrd``
element ``SIM:<name>`` (injected by the adapter ONLY in SIMULATOR mode);
default is ``success`` (immediate ACSC).

Scenarios: ``success`` (ACSC), ``decline``/``rjct_am04`` (RJCT/AM04),
``acsp_then_camt054_final`` (ACSP now, camt.054 final later — EndToEndId
matching), ``pending_then_success`` (alias of the ACSP/camt.054 flow, the
async-pending baseline), ``timeout``/``no_pacs002_manual_queue`` (no reply
ever; polls return nothing — the adapter ends in MANUAL_QUEUE, never
auto-reverses). Duplicate EndToEndId submissions return the recorded pacs.002
bytes with no second effect (idempotency on the rail side).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta

from bdpay.connectors.rails.rtgs_mx import (
    build_camt054,
    build_pacs002,
    extract_pacs008,
    validate_pacs008,
)
from bdpay.connectors.simulator import sign_callback
from bdpay.platform.clock import Clock

__all__ = ["RtgsSimulatorRail"]

_ASYNC_SCENARIOS = frozenset({"acsp_then_camt054_final", "pending_then_success"})
_SILENT_SCENARIOS = frozenset({"timeout", "no_pacs002_manual_queue"})
_REJECT_SCENARIOS = frozenset({"decline", "rjct_am04"})


@dataclass
class _DueCallback:
    due_at: datetime
    sequence: int
    body: bytes


class RtgsSimulatorRail:
    """Deterministic ISO 20022 H2H endpoint for ``rtgs_iso20022_v1``."""

    def __init__(self, connector_id: str, *, clock: Clock,
                 signing_key: bytes = b"rtgs-sim-channel-key") -> None:
        self.connector_id = connector_id
        self._clock = clock
        self._signing_key = signing_key
        self._recorded: dict[str, bytes] = {}
        self._status: dict[str, str] = {}
        self._effects: dict[str, int] = {}
        self._queue: list[_DueCallback] = []
        self._sequence = 0
        self._callback_sink = None

    def bind_callback_sink(self, sink) -> None:
        self._callback_sink = sink

    def effect_count(self, end_to_end_id: str) -> int:
        return self._effects.get(end_to_end_id, 0)

    @staticmethod
    async def _never() -> None:
        await asyncio.Event().wait()

    async def send(self, pacs008_xml: bytes) -> bytes:
        """One pacs.008 -> pacs.002 exchange. Silent scenarios never reply."""
        validate_pacs008(pacs008_xml)  # fail-closed: malformed is refused
        message = extract_pacs008(pacs008_xml)
        e2e = message["end_to_end_id"]
        recorded = self._recorded.get(e2e)
        if recorded is not None:
            return recorded  # duplicate submission dedupe: no second effect
        scenario = message.get("sim_scenario") or "success"
        now = self._clock.now()
        if scenario in _SILENT_SCENARIOS:
            self._status[e2e] = "SILENT"
            await self._never()
        if scenario in _REJECT_SCENARIOS:
            response = build_pacs002(
                original_msg_id=message["msg_id"], end_to_end_id=e2e,
                tx_status="RJCT", reason_code="AM04", created_at=now,
            )
            self._status[e2e] = "RJCT"
        elif scenario in _ASYNC_SCENARIOS:
            response = build_pacs002(
                original_msg_id=message["msg_id"], end_to_end_id=e2e,
                tx_status="ACSP", reason_code=None, created_at=now,
            )
            self._status[e2e] = "ACSP"
            self._enqueue_camt054(e2e, message["amount"], due_at=now + timedelta(seconds=30))
        else:  # success fallback
            response = build_pacs002(
                original_msg_id=message["msg_id"], end_to_end_id=e2e,
                tx_status="ACSC", reason_code=None, created_at=now,
            )
            self._status[e2e] = "ACSC"
        self._effects[e2e] = self._effects.get(e2e, 0) + 1
        self._recorded[e2e] = response
        return response

    async def poll(self, end_to_end_id: str) -> bytes | None:
        """Status poll: recorded pacs.002 or None (rail has nothing to say)."""
        status = self._status.get(end_to_end_id)
        if status is None or status == "SILENT":
            return None
        return self._recorded.get(end_to_end_id)

    def _enqueue_camt054(self, end_to_end_id: str, amount: str, *, due_at: datetime) -> None:
        self._sequence += 1
        self._queue.append(
            _DueCallback(
                due_at=due_at,
                sequence=self._sequence,
                body=build_camt054(
                    end_to_end_id=end_to_end_id, amount=amount, created_at=due_at
                ),
            )
        )

    async def deliver_due_callbacks(self) -> int:
        if self._callback_sink is None:
            raise RuntimeError("no callback sink bound; call bind_callback_sink first")
        now = self._clock.now()
        due = sorted(
            (cb for cb in self._queue if cb.due_at <= now),
            key=lambda cb: (cb.due_at, cb.sequence),
        )
        for callback in due:
            self._queue.remove(callback)
            self._status[self._e2e_of(callback.body)] = "ACSC"
            timestamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
            headers = sign_callback(self._signing_key, callback.body, timestamp)
            await self._callback_sink(self.connector_id, headers, callback.body)
        return len(due)

    @staticmethod
    def _e2e_of(camt_body: bytes) -> str:
        from bdpay.connectors.rails.rtgs_mx import extract_camt054

        return extract_camt054(camt_body)["end_to_end_id"]

    def pending_callbacks(self) -> int:
        return len(self._queue)

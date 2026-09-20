"""Deterministic NPSB rail-host simulator speaking packed ISO 8583 (spec/11 §F).

The simulator IS a switch endpoint: it unpacks real wire bytes with the same
dictionary codec the adapter uses, decides an outcome deterministically, and
packs a real MAC-bearing response. Scenario selection comes from the
BB-private DE63 hint (``SIM:<name>``, injected by the adapter ONLY in
SIMULATOR mode); absent a hint the rail approves (DE39=00).

Scenario semantics (spec/11 §F rail table + the timeout-reversal flow):

- ``success`` / default: 0210 DE39=00, one rail-side effect.
- ``decline``: 0210 DE39=51 (insufficient funds), definitive.
- ``timeout``: rail records the instruction UNRESOLVED and never replies;
  status polls answer DE39=91 (non-definitive) so the poll-then-reverse path
  ends in an acknowledged 0420/0430 reversal.
- ``de39_91_then_query_success``: rail PROCESSED the instruction (truth =
  success, effect recorded) but answers DE39=91; ``query_status`` finds the
  recorded truth — money is settled by the poll, never reversed.
- ``late_0210_after_reversal``: no reply; reversal is acked; then a late
  SUCCESS 0210 is queued for async delivery — the consumer must record the
  late response without double-credit (reversal stands).

Session behavior: 0800 sign-on/sign-off/cutover/echo are answered DE39=00;
``answer_echoes=False`` makes echoes (and everything financial) hang, driving
the echo-storm sign-off scenario. ``ack_reversals=False`` makes 0420/0421
hang so reversals land in the adapter's store-and-forward queue (CERT-R5).
Retransmission of an already-answered STAN returns the recorded response
bytes with NO second effect (ISO retransmission dedupe).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta

from bdpay.connectors.rails.iso8583 import (
    NPSB_V28_DICTIONARY,
    Iso8583Error,
    mac_preimage,
    pack_message,
    unpack_message,
)
from bdpay.connectors.rails.ports import MacService
from bdpay.platform.clock import Clock

__all__ = ["NpsbSimulatorRail", "SIM_HINT_PREFIX"]

SIM_HINT_PREFIX = "SIM:"

_DECLINE_DE39 = "51"
_SUCCESS_DE39 = "00"
_UNRESOLVED_DE39 = "91"
_NOT_FOUND_DE39 = "25"


@dataclass
class _RailRecord:
    """Rail-side truth for one financial STAN."""

    stan: str
    rrn: str
    request_fields: dict[int, str]
    scenario: str
    de39: str | None  # None while unresolved
    reversed_flag: bool = False


@dataclass
class _DueCallback:
    due_at: datetime
    sequence: int
    body: bytes


class NpsbSimulatorRail:
    """Deterministic ISO 8583 switch endpoint for ``npsb_iso8583_v28``."""

    def __init__(
        self,
        connector_id: str,
        *,
        clock: Clock,
        mac: MacService,
        dictionary: dict | None = None,
    ) -> None:
        self.connector_id = connector_id
        self._clock = clock
        self._mac = mac
        self._dictionary = dictionary if dictionary is not None else NPSB_V28_DICTIONARY
        self.answer_echoes = True
        self.ack_reversals = True
        self._signed_on = False
        self._records: dict[str, _RailRecord] = {}
        self._responses: dict[tuple[str, str], bytes] = {}
        self._effects: dict[str, int] = {}
        self._queue: list[_DueCallback] = []
        self._sequence = 0
        self._callback_sink = None

    # -- wiring -----------------------------------------------------------------

    def bind_callback_sink(self, sink) -> None:
        """``sink(connector_id, headers, body)`` — usually ``pipeline.ingest``."""
        self._callback_sink = sink

    def effect_count_for_stan(self, stan: str) -> int:
        return self._effects.get(stan, 0)

    @property
    def signed_on(self) -> bool:
        return self._signed_on

    # -- helpers ----------------------------------------------------------------

    @staticmethod
    async def _never() -> None:
        await asyncio.Event().wait()

    def _respond(self, mti: str, fields: dict[int, str]) -> bytes:
        fields = dict(fields)
        fields[64] = self._mac.generate_mac(mac_preimage(mti, fields))
        return pack_message(mti, fields, self._dictionary)

    def _echo_fields(self, request: dict[int, str], de39: str) -> dict[int, str]:
        fields: dict[int, str] = {39: de39}
        for de in (3, 4, 7, 11, 12, 13, 32, 37, 41, 49):
            if de in request:
                fields[de] = request[de]
        return fields

    @staticmethod
    def _scenario_for(request: dict[int, str]) -> str:
        hint = request.get(63, "")
        if hint.startswith(SIM_HINT_PREFIX):
            return hint[len(SIM_HINT_PREFIX) :]
        return "success"

    def _record_effect(self, stan: str) -> None:
        self._effects[stan] = self._effects.get(stan, 0) + 1

    # -- the wire ----------------------------------------------------------------

    async def exchange(self, payload: bytes) -> bytes:
        """One request/response exchange. May hang forever (timeout model)."""
        mti, fields = unpack_message(payload, self._dictionary)
        if 64 not in fields or not self._mac.verify_mac(
            mac_preimage(mti, fields), fields[64]
        ):
            # Unauthenticated financial traffic is refused (security class).
            return self._respond("0810" if mti == "0800" else "0210",
                                 self._echo_fields(fields, "63"))
        if mti == "0800":
            return await self._network_management(fields)
        if not self._signed_on:
            return self._respond("0210", self._echo_fields(fields, "92"))
        if mti == "0200":
            return await self._financial(fields)
        if mti in ("0420", "0421"):
            return await self._reversal(mti, fields)
        raise Iso8583Error(f"simulator cannot serve MTI {mti}", code="iso_mti_not_allowed")

    async def _network_management(self, fields: dict[int, str]) -> bytes:
        nmi = fields.get(70, "")
        if nmi == "001":
            self._signed_on = True
        elif nmi == "002":
            self._signed_on = False
        elif nmi == "301":
            if not self.answer_echoes:
                await self._never()
        elif nmi != "201":
            return self._respond("0810", {**self._echo_fields(fields, "30"), 70: nmi})
        return self._respond("0810", {**self._echo_fields(fields, _SUCCESS_DE39), 70: nmi})

    async def _financial(self, fields: dict[int, str]) -> bytes:
        stan = fields[11]
        rrn = fields.get(37, "")
        replay_key = (stan, rrn)
        recorded = self._responses.get(replay_key)
        if recorded is not None:
            return recorded  # retransmission dedupe: no second effect
        if fields.get(3, "").startswith("38"):
            return self._status_inquiry(fields)
        scenario = self._scenario_for(fields)
        record = _RailRecord(
            stan=stan, rrn=rrn, request_fields=dict(fields), scenario=scenario, de39=None
        )
        self._records[stan] = record
        if scenario == "decline":
            record.de39 = _DECLINE_DE39
            self._record_effect(stan)
            response = self._respond(
                "0210", self._echo_fields(fields, _DECLINE_DE39)
            )
        elif scenario in ("timeout", "late_0210_after_reversal"):
            await self._never()
            raise AssertionError("unreachable")
        elif scenario == "de39_91_then_query_success":
            record.de39 = _SUCCESS_DE39  # rail truth: processed
            self._record_effect(stan)
            response = self._respond(
                "0210", self._echo_fields(fields, _UNRESOLVED_DE39)
            )
        else:  # success (mandatory fallback)
            record.de39 = _SUCCESS_DE39
            self._record_effect(stan)
            success = self._echo_fields(fields, _SUCCESS_DE39)
            success[38] = f"A{stan[1:]}"
            response = self._respond("0210", success)
        self._responses[replay_key] = response
        return response

    def _status_inquiry(self, fields: dict[int, str]) -> bytes:
        original_stan = fields.get(90, "")[4:10]
        record = self._records.get(original_stan)
        if record is None or record.reversed_flag:
            de39 = _NOT_FOUND_DE39
        elif record.de39 is None:
            de39 = _UNRESOLVED_DE39
        else:
            de39 = record.de39
        return self._respond("0210", self._echo_fields(fields, de39))

    async def _reversal(self, mti: str, fields: dict[int, str]) -> bytes:
        if not self.ack_reversals:
            await self._never()
        original_stan = fields.get(90, "")[4:10]
        record = self._records.get(original_stan)
        if record is not None and not record.reversed_flag:
            record.reversed_flag = True
            if record.scenario == "late_0210_after_reversal":
                self._enqueue_late_0210(record)
        return self._respond("0430", self._echo_fields(fields, _SUCCESS_DE39))

    # -- late async messages -------------------------------------------------------

    def _enqueue_late_0210(self, record: _RailRecord) -> None:
        late = self._echo_fields(record.request_fields, _SUCCESS_DE39)
        late[38] = f"L{record.stan[1:]}"
        body = self._respond("0210", late)
        self._sequence += 1
        self._queue.append(
            _DueCallback(
                due_at=self._clock.now() + timedelta(seconds=20),
                sequence=self._sequence,
                body=body,
            )
        )

    async def deliver_due_callbacks(self) -> int:
        """Deliver queued late messages with ``due_at <= clock.now()``."""
        if self._callback_sink is None:
            raise RuntimeError("no callback sink bound; call bind_callback_sink first")
        now = self._clock.now()
        due = sorted(
            (cb for cb in self._queue if cb.due_at <= now),
            key=lambda cb: (cb.due_at, cb.sequence),
        )
        for callback in due:
            self._queue.remove(callback)
            await self._callback_sink(self.connector_id, {}, callback.body)
        return len(due)

    def pending_callbacks(self) -> int:
        return len(self._queue)

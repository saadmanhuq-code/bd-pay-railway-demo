"""``npsb_iso8583_v28`` — NPSB IBFT/Bangla-QR adapter (spec/11 §A).

Implements the frozen ``sdk.PaymentConnector`` + ``ConnectorWebhookHandler``
against the BB v2.8 ISO 8583 dialect: dictionary-driven codec, STAN/RRN
allocation (A.2 normative algorithm), session FSM (sign-on / 60s echo /
cutover / sign-off, refusal-first), DE39 -> canonical taxonomy (A.4 complete
table), and the store-and-forward reversal discipline of FSM §2 — a queued
reversal NEVER expires; rows leave the queue only on an acknowledged 0430.

The adapter is transport-agnostic: SIMULATOR mode wires the in-process
:class:`~bdpay.connectors.simulators.npsb_sim.NpsbSimulatorRail`; SANDBOX and
PRODUCTION wire :class:`TcpNpsbTransport` (persistent framed TCP, mTLS-ready)
— the wire code is fully written and activates on credentials/host config.
MAC generation/verification is the injected :class:`MacService` boundary
(spec/12 owns the HSM); MAC unavailability refuses traffic fail-closed.
Raw wire bytes are archived content-addressed (E12 canonical hash); they are
never logged.
"""

from __future__ import annotations

import asyncio
import ssl
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from bdpay.connectors.ids_ext import make_connector_id
from bdpay.connectors.ports import InMemoryObjectStore, ObjectStore, raw_object_key
from bdpay.connectors.rails.iso8583 import (
    NPSB_V28_DICTIONARY,
    Iso8583Error,
    dictionary_hash,
    mac_preimage,
    pack_de48,
    pack_message,
    unpack_message,
)
from bdpay.connectors.rails.ports import DHAKA_TZ, MacService
from bdpay.connectors.sdk import (
    ConnectorResult,
    ConnectorStatus,
    Money,
    PaymentInstruction,
)
from bdpay.platform.canonical import canonical_json, sha256_canonical
from bdpay.platform.clock import Clock
from bdpay.qr.codec import MerchantAccountInfo
from bdpay.qr.errors import QrEncodeError
from bdpay.qr.npsb import npsb_qr_dictionary_rows

__all__ = [
    "CONNECTOR_ID",
    "DE39_TO_RESULT",
    "InMemorySafStore",
    "InMemoryStanStore",
    "NpsbConnector",
    "NpsbSession",
    "NpsbTransport",
    "NpsbWebhookHandler",
    "PostgresSafStore",
    "PostgresStanStore",
    "SafItem",
    "StanAllocation",
    "StanExhaustedError",
    "TcpNpsbTransport",
    "build_de90",
]

CONNECTOR_ID = "npsb_iso8583_v28"
SUPPORTED_METHODS = ("NPSB_IBFT", "BANGLA_QR")
ConnectionFactory = Callable[[], Any]

#: Processing codes (spec/11 A.3): IBFT credit; status inquiry family.
PROC_IBFT_CREDIT = "400000"
PROC_QR_MERCHANT = "260000"
PROC_STATUS_INQUIRY = "380000"

#: DE39 -> (ConnectorStatus, error_code) — spec/11 A.4, binding and complete.
DE39_TO_RESULT: dict[str, tuple[ConnectorStatus, str | None]] = {
    "00": (ConnectorStatus.SUCCESS, None),
    "01": (ConnectorStatus.REJECTED, "refer_to_issuer"),
    "02": (ConnectorStatus.REJECTED, "refer_to_issuer"),
    "03": (ConnectorStatus.REJECTED, "invalid_merchant"),
    "04": (ConnectorStatus.REJECTED, "card_blocked"),
    "07": (ConnectorStatus.REJECTED, "card_blocked"),
    "05": (ConnectorStatus.REJECTED, "do_not_honor"),
    "08": (ConnectorStatus.SUCCESS, None),
    "12": (ConnectorStatus.REJECTED, "invalid_transaction"),
    "13": (ConnectorStatus.REJECTED, "invalid_amount"),
    "14": (ConnectorStatus.REJECTED, "invalid_account"),
    "15": (ConnectorStatus.REJECTED, "no_such_issuer"),
    "25": (ConnectorStatus.REJECTED, "record_not_found"),
    "30": (ConnectorStatus.FAILED, "format_error"),
    "41": (ConnectorStatus.REJECTED, "lost_or_stolen"),
    "43": (ConnectorStatus.REJECTED, "lost_or_stolen"),
    "51": (ConnectorStatus.REJECTED, "insufficient_funds"),
    "54": (ConnectorStatus.REJECTED, "expired_card"),
    "55": (ConnectorStatus.REJECTED, "incorrect_pin"),
    "57": (ConnectorStatus.REJECTED, "txn_not_permitted_to_holder"),
    "58": (ConnectorStatus.REJECTED, "txn_not_permitted_to_terminal"),
    "61": (ConnectorStatus.REJECTED, "exceeds_amount_limit"),
    "62": (ConnectorStatus.REJECTED, "restricted_card"),
    "63": (ConnectorStatus.FAILED, "security_violation"),
    "65": (ConnectorStatus.REJECTED, "exceeds_frequency_limit"),
    "68": (ConnectorStatus.TIMED_OUT, "issuer_response_late"),
    "75": (ConnectorStatus.REJECTED, "pin_tries_exceeded"),
    "90": (ConnectorStatus.FAILED, "cutoff_in_progress"),
    "91": (ConnectorStatus.TIMED_OUT, "issuer_inoperative"),
    "92": (ConnectorStatus.FAILED, "routing_unavailable"),
    "94": (ConnectorStatus.REJECTED, "duplicate_transmission"),
    "96": (ConnectorStatus.FAILED, "system_malfunction"),
}


def map_de39(code: str) -> tuple[ConnectorStatus, str | None]:
    mapped = DE39_TO_RESULT.get(code)
    if mapped is None:
        # Taxonomy violation alarm class (CERT-05 catches in certification).
        return ConnectorStatus.FAILED, f"iso_unmapped_de39_{code}"
    return mapped


class StanExhaustedError(RuntimeError):
    """Cyclic-wrap collision inside one (connector, date, session) scope."""


@dataclass(frozen=True)
class StanAllocation:
    """One ``stan_allocations`` row (spec/11 Data Model)."""

    allocation_id: str
    connector_id: str
    business_date: date
    session_ref: str
    stan: str
    rrn: str
    instruction_id: str | None
    connector_ref: str | None
    allocated_at: datetime


class InMemoryStanStore:
    """Deterministic STAN counter + allocation log (UNIQUE collision refused)."""

    def __init__(self) -> None:
        self._counters: dict[tuple[str, str, str], int] = {}
        self._unique: set[tuple[str, str, str, str]] = set()
        self._allocations: list[StanAllocation] = []
        self._by_ref: dict[str, StanAllocation] = {}
        self._by_stan: dict[str, StanAllocation] = {}

    def allocate(
        self,
        connector_id: str,
        business_date: date,
        session_ref: str,
        *,
        instruction_id: str | None,
        connector_ref: str | None,
        clock: Clock,
    ) -> StanAllocation:
        scope = (connector_id, business_date.isoformat(), session_ref)
        n = self._counters.get(scope, 1)
        stan = f"{n:06d}"
        self._counters[scope] = 1 if n == 999_999 else n + 1
        unique_key = (*scope, stan)
        if unique_key in self._unique:
            raise StanExhaustedError(
                "STAN cyclic wrap collided with a live allocation in scope "
                f"{scope} (tripwire — page ops)"
            )
        self._unique.add(unique_key)
        now = clock.now()
        dhaka = now.astimezone(DHAKA_TZ)
        rrn = f"{dhaka.year % 10}{dhaka.timetuple().tm_yday:03d}{dhaka.hour:02d}{stan}"
        allocation = StanAllocation(
            allocation_id=make_connector_id(
                "stan",
                {
                    "connector_id": connector_id,
                    "business_date": business_date.isoformat(),
                    "session_ref": session_ref,
                    "stan": stan,
                },
            ),
            connector_id=connector_id,
            business_date=business_date,
            session_ref=session_ref,
            stan=stan,
            rrn=rrn,
            instruction_id=instruction_id,
            connector_ref=connector_ref,
            allocated_at=now,
        )
        self._allocations.append(allocation)
        if connector_ref is not None and connector_ref not in self._by_ref:
            self._by_ref[connector_ref] = allocation
        self._by_stan[stan] = allocation
        return allocation

    def find_by_ref(self, connector_ref: str) -> StanAllocation | None:
        return self._by_ref.get(connector_ref)

    def find_by_stan(self, stan: str) -> StanAllocation | None:
        return self._by_stan.get(stan)

    def stans_for_ref(self, connector_ref: str) -> list[str]:
        return [a.stan for a in self._allocations if a.connector_ref == connector_ref]


class PostgresStanStore:
    """Postgres-backed STAN counter + allocation log for NPSB."""

    def __init__(self, connection_factory: ConnectionFactory) -> None:
        self._connect = connection_factory

    @staticmethod
    def _to_allocation(row: Any) -> StanAllocation:
        return StanAllocation(
            allocation_id=row["allocation_id"],
            connector_id=row["connector_id"],
            business_date=row["business_date"],
            session_ref=row["session_ref"],
            stan=row["stan"],
            rrn=row["rrn"],
            instruction_id=row["instruction_id"],
            connector_ref=row["connector_ref"],
            allocated_at=row["allocated_at"],
        )

    def allocate(
        self,
        connector_id: str,
        business_date: date,
        session_ref: str,
        *,
        instruction_id: str | None,
        connector_ref: str | None,
        clock: Clock,
    ) -> StanAllocation:
        from psycopg.rows import dict_row

        now = clock.now()
        dhaka = now.astimezone(DHAKA_TZ)
        with self._connect() as conn:
            with conn.transaction():
                with conn.cursor(row_factory=dict_row) as cur:
                    cur.execute(
                        """
                        INSERT INTO connector_npsb_stan_counters
                            (connector_id, business_date, session_ref, next_stan, updated_at)
                        VALUES (%s, %s, %s, 1, %s)
                        ON CONFLICT (connector_id, business_date, session_ref) DO NOTHING
                        """,
                        (connector_id, business_date, session_ref, now),
                    )
                    row = cur.execute(
                        """
                        SELECT next_stan
                        FROM connector_npsb_stan_counters
                        WHERE connector_id = %s
                          AND business_date = %s
                          AND session_ref = %s
                        FOR UPDATE
                        """,
                        (connector_id, business_date, session_ref),
                    ).fetchone()
                    assert row is not None
                    n = int(row["next_stan"])
                    scope = (connector_id, business_date.isoformat(), session_ref)
                    for _ in range(999_999):
                        stan = f"{n:06d}"
                        next_n = 1 if n == 999_999 else n + 1
                        cur.execute(
                            """
                            UPDATE connector_npsb_stan_counters
                            SET next_stan = %s, updated_at = %s
                            WHERE connector_id = %s
                              AND business_date = %s
                              AND session_ref = %s
                            """,
                            (next_n, now, connector_id, business_date, session_ref),
                        )
                        rrn = (
                            f"{dhaka.year % 10}{dhaka.timetuple().tm_yday:03d}"
                            f"{dhaka.hour:02d}{stan}"
                        )
                        allocation = StanAllocation(
                            allocation_id=make_connector_id(
                                "stan",
                                {
                                    "connector_id": connector_id,
                                    "business_date": business_date.isoformat(),
                                    "session_ref": session_ref,
                                    "stan": stan,
                                },
                            ),
                            connector_id=connector_id,
                            business_date=business_date,
                            session_ref=session_ref,
                            stan=stan,
                            rrn=rrn,
                            instruction_id=instruction_id,
                            connector_ref=connector_ref,
                            allocated_at=now,
                        )
                        inserted = cur.execute(
                            """
                            INSERT INTO connector_npsb_stan_allocations
                                (allocation_id, connector_id, business_date, session_ref,
                                 stan, rrn, instruction_id, connector_ref, allocated_at)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                            ON CONFLICT DO NOTHING
                            RETURNING allocation_id
                            """,
                            (
                                allocation.allocation_id,
                                allocation.connector_id,
                                allocation.business_date,
                                allocation.session_ref,
                                allocation.stan,
                                allocation.rrn,
                                allocation.instruction_id,
                                allocation.connector_ref,
                                allocation.allocated_at,
                            ),
                        ).fetchone()
                        if inserted is not None:
                            return allocation
                        n = next_n
        raise StanExhaustedError(
            "STAN cyclic wrap collided with every allocation in scope "
            f"{scope} (tripwire — page ops)"
        )

    def find_by_ref(self, connector_ref: str) -> StanAllocation | None:
        from psycopg.rows import dict_row

        with self._connect() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                row = cur.execute(
                    """
                    SELECT *
                    FROM connector_npsb_stan_allocations
                    WHERE connector_ref = %s
                    ORDER BY allocated_at, stan
                    LIMIT 1
                    """,
                    (connector_ref,),
                ).fetchone()
        return self._to_allocation(row) if row is not None else None

    def find_by_stan(self, stan: str) -> StanAllocation | None:
        from psycopg.rows import dict_row

        with self._connect() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                row = cur.execute(
                    """
                    SELECT *
                    FROM connector_npsb_stan_allocations
                    WHERE stan = %s
                    ORDER BY allocated_at DESC
                    LIMIT 1
                    """,
                    (stan,),
                ).fetchone()
        return self._to_allocation(row) if row is not None else None

    def stans_for_ref(self, connector_ref: str) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT stan
                FROM connector_npsb_stan_allocations
                WHERE connector_ref = %s
                ORDER BY allocated_at, stan
                """,
                (connector_ref,),
            ).fetchall()
        return [row[0] for row in rows]


def build_de90(mti: str, stan: str, de7: str, acquiring_inst_id: str) -> str:
    """Original-data-elements (n42): MTI + STAN + transmission dt + acq + fwd."""
    acq = acquiring_inst_id.zfill(11)[:11]
    forwarding = "0" * 11
    value = f"{mti}{stan}{de7}{acq}{forwarding}"
    return value.ljust(42, "0")[:42]


# -- session FSM ------------------------------------------------------------------


_SESSION_TABLE: dict[tuple[str, str], str] = {
    ("SIGNED_OFF", "sign_on"): "SIGN_ON_SENT",
    ("SIGN_ON_SENT", "sign_on_ack"): "SIGNED_ON",
    ("SIGN_ON_SENT", "sign_on_failed"): "SIGNED_OFF",
    ("SIGNED_ON", "echo_ok"): "SIGNED_ON",
    ("SIGNED_ON", "echo_missed"): "ECHO_MISSED",
    ("ECHO_MISSED", "echo_ok"): "SIGNED_ON",
    ("ECHO_MISSED", "echo_missed"): "ECHO_MISSED",
    ("ECHO_MISSED", "third_miss"): "SIGNED_OFF",
    ("SIGNED_ON", "cutover_window"): "CUTOVER_IN_PROGRESS",
    ("CUTOVER_IN_PROGRESS", "cutover_ack"): "SIGNED_ON",
    ("SIGNED_ON", "sign_off"): "SIGNED_OFF",
}

#: Session states that admit financial traffic.
_DISPATCHABLE = frozenset({"SIGNED_ON", "ECHO_MISSED"})


class SessionTransitionDenied(RuntimeError):
    """Refusal-first: the session transition is not in the spec/11 table."""


@dataclass
class NpsbSession:
    """``rail_session_states`` row + FSM (spec/11 State Machines §1)."""

    connector_id: str
    state: str = "SIGNED_OFF"
    business_date: date | None = None
    session_ref: str | None = None
    missed_echo_count: int = 0
    sign_on_count: int = 0
    last_echo_at: datetime | None = None
    signed_on_at: datetime | None = None
    history: list[tuple[str, str, str]] = field(default_factory=list)

    def advance(self, trigger: str) -> str:
        to_state = _SESSION_TABLE.get((self.state, trigger))
        if to_state is None:
            raise SessionTransitionDenied(
                f"NPSB session transition {self.state} --[{trigger}]--> is DENIED"
            )
        self.history.append((self.state, trigger, to_state))
        self.state = to_state
        return to_state

    @property
    def dispatchable(self) -> bool:
        return self.state in _DISPATCHABLE


# -- transports ---------------------------------------------------------------------


class NpsbTransport:
    """Wire port: one request/response exchange of packed ISO 8583 bytes.

    A Protocol-shaped seam (duck-typed; kept as a plain base so the live
    transport can subclass and share framing helpers).
    """

    async def exchange(self, payload: bytes) -> bytes:
        raise ConnectionError("transport not wired")  # fail-closed default


class TcpNpsbTransport(NpsbTransport):
    """Persistent framed TCP transport (SANDBOX/PRODUCTION wire code).

    Framing: 2-byte big-endian length prefix + ASCII ISO 8583 payload (the
    common H2H switch framing). mTLS: pass a pre-built ``ssl.SSLContext``
    (client cert/key per registry ``mtls_client_cert_ref``) or ``None`` for
    pre-prod links that terminate TLS at the VPN concentrator. Responses are
    correlated to requests by (response-MTI, STAN); unsolicited inbound
    messages (late 0210s, 0430s, network management) go to ``inbound_sink``
    for loopback injection into the spec/10 webhook ingestion pipeline.
    """

    def __init__(
        self,
        host: str,
        port: int,
        *,
        dictionary: dict | None = None,
        ssl_context: ssl.SSLContext | None = None,
        inbound_sink=None,
        open_connection=None,
    ) -> None:
        self._host = host
        self._port = port
        self._dictionary = dictionary if dictionary is not None else NPSB_V28_DICTIONARY
        self._ssl = ssl_context
        self._inbound_sink = inbound_sink
        self._open_connection = (
            open_connection if open_connection is not None else asyncio.open_connection
        )
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._pending: dict[tuple[str, str], asyncio.Future] = {}
        self._reader_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    @staticmethod
    def _frame(payload: bytes) -> bytes:
        if len(payload) > 0xFFFF:
            raise ValueError("ISO 8583 payload exceeds 2-byte frame limit")
        return len(payload).to_bytes(2, "big") + payload

    @staticmethod
    def _response_key(mti: str, stan: str) -> tuple[str, str]:
        return (mti, stan)

    @staticmethod
    def _expected_response_mti(request_mti: str) -> str:
        pairs = {"0100": "0110", "0200": "0210", "0420": "0430", "0421": "0430",
                 "0800": "0810"}
        return pairs.get(request_mti, "0210")

    async def connect(self) -> None:
        async with self._lock:
            if self._writer is not None:
                return
            self._reader, self._writer = await self._open_connection(
                self._host, self._port, ssl=self._ssl
            )
            self._reader_task = asyncio.ensure_future(self._read_loop())

    async def close(self) -> None:
        async with self._lock:
            if self._reader_task is not None:
                self._reader_task.cancel()
                self._reader_task = None
            if self._writer is not None:
                self._writer.close()
                self._writer = None
                self._reader = None
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(ConnectionError("NPSB link closed"))
            self._pending.clear()

    async def _read_loop(self) -> None:
        try:
            while self._reader is not None:
                header = await self._reader.readexactly(2)
                length = int.from_bytes(header, "big")
                payload = await self._reader.readexactly(length)
                self._route_inbound(payload)
        except (asyncio.IncompleteReadError, ConnectionError, asyncio.CancelledError):
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(ConnectionError("NPSB link dropped"))
            self._pending.clear()

    def _route_inbound(self, payload: bytes) -> None:
        try:
            mti, fields = unpack_message(payload, self._dictionary)
        except Iso8583Error:
            return  # undecodable inbound: dropped here, raw stays on the rail side
        key = self._response_key(mti, fields.get(11, ""))
        future = self._pending.pop(key, None)
        if future is not None and not future.done():
            future.set_result(payload)
            return
        if self._inbound_sink is not None:
            self._inbound_sink(payload)

    async def exchange(self, payload: bytes) -> bytes:
        await self.connect()
        mti, fields = unpack_message(payload, self._dictionary)
        key = self._response_key(self._expected_response_mti(mti), fields.get(11, ""))
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._pending[key] = future
        assert self._writer is not None
        self._writer.write(self._frame(payload))
        await self._writer.drain()
        try:
            return await future
        finally:
            self._pending.pop(key, None)


# -- store-and-forward ---------------------------------------------------------------


@dataclass
class SafItem:
    """One ``store_and_forward_items`` row — never deleted before ack."""

    saf_id: str
    connector_id: str
    connector_ref: str
    original_stan: str
    reversal_fields: dict[int, str]
    reversal_mti: str = "0420"
    attempt_count: int = 0
    acked: bool = False
    queued_at: datetime | None = None
    acked_at: datetime | None = None
    acked_rail_transaction_id: str | None = None
    ack_raw_response_hash: str | None = None


class InMemorySafStore:
    """Deterministic in-memory SAF queue for unit tests."""

    def __init__(self) -> None:
        self._items: list[SafItem] = []

    def insert(self, item: SafItem) -> None:
        if not any(existing.saf_id == item.saf_id for existing in self._items):
            self._items.append(item)

    def save(self, item: SafItem) -> None:
        for index, existing in enumerate(self._items):
            if existing.saf_id == item.saf_id:
                self._items[index] = item
                return
        self._items.append(item)

    def list_all(self) -> list[SafItem]:
        return self._items

    def replace_all(self, items: list[SafItem]) -> None:
        self._items = items

    def pending(self) -> list[SafItem]:
        return [item for item in self._items if not item.acked]

    def pending_by_ref(self, connector_ref: str) -> SafItem | None:
        for item in self._items:
            if item.connector_ref == connector_ref and not item.acked:
                return item
        return None

    def acked_by_ref(self, connector_ref: str) -> SafItem | None:
        for item in self._items:
            if item.connector_ref == connector_ref and item.acked:
                return item
        return None


class PostgresSafStore:
    """Postgres-backed NPSB store-and-forward reversal queue."""

    def __init__(self, connection_factory: ConnectionFactory) -> None:
        self._connect = connection_factory

    @staticmethod
    def _jsonb(fields: dict[int, str]) -> Any:
        from psycopg.types.json import Jsonb

        payload = {str(k): v for k, v in fields.items()}
        return Jsonb(payload, dumps=lambda o: canonical_json(o).decode("utf-8"))

    @staticmethod
    def _to_item(row: Any) -> SafItem:
        fields = row["reversal_fields"]
        return SafItem(
            saf_id=row["saf_id"],
            connector_id=row["connector_id"],
            connector_ref=row["connector_ref"],
            original_stan=row["original_stan"],
            reversal_fields={int(k): str(v) for k, v in dict(fields).items()},
            reversal_mti=row["reversal_mti"],
            attempt_count=row["attempt_count"],
            acked=row["acked"],
            queued_at=row["queued_at"],
            acked_at=row["acked_at"],
            acked_rail_transaction_id=row["acked_rail_transaction_id"],
            ack_raw_response_hash=row["ack_raw_response_hash"],
        )

    def insert(self, item: SafItem) -> None:
        self.save(item)

    def save(self, item: SafItem) -> None:
        updated_at = item.acked_at or item.queued_at
        if updated_at is None:
            raise ValueError("NPSB SAF item persistence requires queued_at or acked_at")
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO connector_npsb_saf_items
                    (saf_id, connector_id, connector_ref, original_stan,
                     reversal_fields, reversal_mti, attempt_count, acked, queued_at,
                     acked_at, acked_rail_transaction_id, ack_raw_response_hash,
                     updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (saf_id) DO UPDATE SET
                    reversal_fields = EXCLUDED.reversal_fields,
                    reversal_mti = EXCLUDED.reversal_mti,
                    attempt_count = EXCLUDED.attempt_count,
                    acked = EXCLUDED.acked,
                    queued_at = EXCLUDED.queued_at,
                    acked_at = EXCLUDED.acked_at,
                    acked_rail_transaction_id = EXCLUDED.acked_rail_transaction_id,
                    ack_raw_response_hash = EXCLUDED.ack_raw_response_hash,
                    updated_at = EXCLUDED.updated_at
                """,
                (
                    item.saf_id,
                    item.connector_id,
                    item.connector_ref,
                    item.original_stan,
                    self._jsonb(item.reversal_fields),
                    item.reversal_mti,
                    item.attempt_count,
                    item.acked,
                    item.queued_at,
                    item.acked_at,
                    item.acked_rail_transaction_id,
                    item.ack_raw_response_hash,
                    updated_at,
                ),
            )
            conn.commit()

    def list_all(self) -> list[SafItem]:
        from psycopg.rows import dict_row

        with self._connect() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                rows = cur.execute(
                    """
                    SELECT *
                    FROM connector_npsb_saf_items
                    ORDER BY queued_at NULLS LAST, saf_id
                    """
                ).fetchall()
        return [self._to_item(row) for row in rows]

    def replace_all(self, items: list[SafItem]) -> None:
        for item in items:
            self.save(item)

    def pending(self) -> list[SafItem]:
        from psycopg.rows import dict_row

        with self._connect() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                rows = cur.execute(
                    """
                    SELECT *
                    FROM connector_npsb_saf_items
                    WHERE acked = FALSE
                    ORDER BY queued_at NULLS LAST, saf_id
                    """
                ).fetchall()
        return [self._to_item(row) for row in rows]

    def pending_by_ref(self, connector_ref: str) -> SafItem | None:
        return self._by_ref(connector_ref, acked=False)

    def acked_by_ref(self, connector_ref: str) -> SafItem | None:
        return self._by_ref(connector_ref, acked=True)

    def _by_ref(self, connector_ref: str, *, acked: bool) -> SafItem | None:
        from psycopg.rows import dict_row

        with self._connect() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                row = cur.execute(
                    """
                    SELECT *
                    FROM connector_npsb_saf_items
                    WHERE connector_ref = %s AND acked = %s
                    ORDER BY queued_at NULLS LAST, saf_id
                    LIMIT 1
                    """,
                    (connector_ref, acked),
                ).fetchone()
        return self._to_item(row) if row is not None else None


# -- the adapter -----------------------------------------------------------------------


class NpsbConnector:
    """``sdk.PaymentConnector`` for the NPSB rail (all modes, one code path)."""

    connector_id = CONNECTOR_ID

    def __init__(
        self,
        *,
        transport: NpsbTransport,
        clock: Clock,
        mac: MacService,
        object_store: ObjectStore | None = None,
        stan_store: Any | None = None,
        saf_store: Any | None = None,
        dictionary: dict | None = None,
        acquiring_inst_id: str = "11223344",
        terminal_id: str = "BDPAY001",
        sim_hint_de63: bool = False,
        nm_timeout_s: float = 30.0,
        wait_for=None,
    ) -> None:
        self.supported_methods = SUPPORTED_METHODS
        self._transport = transport
        self._clock = clock
        self._mac = mac
        self._objects = object_store if object_store is not None else InMemoryObjectStore()
        self._stans = stan_store if stan_store is not None else InMemoryStanStore()
        self._saf = saf_store if saf_store is not None else InMemorySafStore()
        self._dictionary = dictionary if dictionary is not None else NPSB_V28_DICTIONARY
        self._dictionary_hash = dictionary_hash(self._dictionary)
        self._acquiring_inst_id = acquiring_inst_id
        self._terminal_id = terminal_id
        self._sim_hint_de63 = sim_hint_de63
        self._nm_timeout_s = nm_timeout_s
        self._wait_for = wait_for if wait_for is not None else asyncio.wait_for
        self.session = NpsbSession(connector_id=CONNECTOR_ID)
        self.late_responses: list[dict] = []
        self._reversed_refs: dict[str, ConnectorResult] = {}
        self._instruction_by_ref: dict[str, str] = {}
        self._sent_de7: dict[str, str] = {}

    # -- shared helpers --------------------------------------------------------------

    @property
    def dictionary_hash(self) -> str:
        return self._dictionary_hash

    @property
    def stan_store(self) -> Any:
        return self._stans

    @property
    def saf_queue(self) -> list[SafItem]:
        return self._saf.list_all()

    @saf_queue.setter
    def saf_queue(self, items: list[SafItem]) -> None:
        self._saf.replace_all(items)

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

    def _synthetic(
        self,
        instruction_id: str,
        connector_ref: str,
        status: ConnectorStatus,
        error_code: str,
    ) -> ConnectorResult:
        raw = {
            "synthetic": True,
            "connector_id": CONNECTOR_ID,
            "connector_ref": connector_ref,
            "status": status.value,
            "error_code": error_code,
        }
        return self._result(
            instruction_id,
            connector_ref,
            status,
            rail_transaction_id=None,
            error_code=error_code,
            raw=raw,
        )

    def _wire_raw(self, response: bytes, mti: str, fields: dict[int, str]) -> dict:
        return {
            "connector_id": CONNECTOR_ID,
            "wire_hex": response.hex(),
            "mti": mti,
            "stan": fields.get(11),
            "rrn": fields.get(37),
            "response_code": fields.get(39),
            "dictionary_hash": self._dictionary_hash,
        }

    def _macd(self, mti: str, fields: dict[int, str]) -> dict[int, str]:
        fields = dict(fields)
        fields[64] = self._mac.generate_mac(mac_preimage(mti, fields))
        return fields

    def _verify_response_mac(self, mti: str, fields: dict[int, str]) -> bool:
        mac = fields.get(64)
        if mac is None:
            return False
        return self._mac.verify_mac(mac_preimage(mti, fields), mac)

    def _de7(self) -> str:
        now = self._clock.now()
        return f"{now.month:02d}{now.day:02d}{now.hour:02d}{now.minute:02d}{now.second:02d}"

    def _local_de12_de13(self) -> tuple[str, str]:
        local = self._clock.now().astimezone(DHAKA_TZ)
        return (
            f"{local.hour:02d}{local.minute:02d}{local.second:02d}",
            f"{local.month:02d}{local.day:02d}",
        )

    def _business_date(self) -> date:
        return self._clock.now().astimezone(DHAKA_TZ).date()

    def _allocate(
        self, instruction_id: str | None, connector_ref: str | None
    ) -> StanAllocation:
        if self.session.business_date is None or self.session.session_ref is None:
            raise SessionTransitionDenied("no live NPSB session scope (sign on first)")
        return self._stans.allocate(
            CONNECTOR_ID,
            self.session.business_date,
            self.session.session_ref,
            instruction_id=instruction_id,
            connector_ref=connector_ref,
            clock=self._clock,
        )

    def _qr_de_fields(self, instruction: PaymentInstruction) -> dict[int, str]:
        """DE overlay for a BANGLA_QR financial message (spec/13 §D rows).

        ``bdpay.qr.npsb.npsb_qr_dictionary_rows`` is consumed verbatim as the
        authoritative row table: plain ``deN`` rows map onto the DE dict and
        ``de48.SS`` rows pack into the DE48 private-data sub-fields (DE48.10
        qr_payload_hash, DE48.11 merchant template echo). Metadata keys are
        the ones the qr pay path stamps on the intent (``qr_payload_hash``)
        plus optional routing-context echoes (``qr_acquirer_id``,
        ``qr_merchant_id``, ``qr_psp_sub_id``); the merchant PAN-equivalent
        falls back to the instruction's tokenized ``beneficiary_ref``.
        Empty sub-field values are omitted — no zero-length TLVs on the wire.
        """
        md = instruction.metadata
        template = MerchantAccountInfo(
            root_id=26,
            guid=md.get("qr_guid", ""),
            acquirer_id=md.get("qr_acquirer_id", ""),
            merchant_id=md.get("qr_merchant_id", "") or instruction.beneficiary_ref,
            psp_sub_id=md.get("qr_psp_sub_id") or None,
        )
        rows = npsb_qr_dictionary_rows(
            amount_minor=instruction.amount.amount_minor,
            payload_hash=md["qr_payload_hash"],
            template=template,
            sender_ref=instruction.sender_ref,
            processing_code=PROC_QR_MERCHANT,
        )
        fields: dict[int, str] = {}
        sub48: dict[str, str] = {}
        for key, value in rows.items():
            if key == "mti":
                continue  # the financial MTI (0200) is fixed by submit()
            if key.startswith("de48."):
                if value:
                    sub48[key.removeprefix("de48.")] = value
                continue
            fields[int(key.removeprefix("de"))] = value
        if sub48:
            fields[48] = pack_de48(sub48, self._dictionary)
        return fields

    # -- session lifecycle (spec/11 FSM §1) ---------------------------------------------

    async def _nm_exchange(self, nmi: str) -> dict[int, str] | None:
        """One 0800/0810 exchange; None on the 30s timeout (echo-miss class)."""
        if self.session.business_date is None or self.session.session_ref is None:
            self.session.business_date = self._business_date()
            self.session.session_ref = (
                f"{self.session.business_date.isoformat()}-{self.session.sign_on_count:03d}"
            )
        allocation = self._allocate(None, None)
        fields = self._macd(
            "0800",
            {7: self._de7(), 11: allocation.stan, 37: allocation.rrn, 70: nmi},
        )
        payload = pack_message("0800", fields, self._dictionary)
        try:
            response = await self._wait_for(
                self._transport.exchange(payload), timeout=self._nm_timeout_s
            )
        except (TimeoutError, ConnectionError):
            return None
        try:
            mti, resp_fields = unpack_message(response, self._dictionary)
        except Iso8583Error:
            return None
        if mti != "0810" or resp_fields.get(11) != allocation.stan:
            return None
        if not self._verify_response_mac(mti, resp_fields):
            return None
        return resp_fields

    async def sign_on(self) -> bool:
        """0800 NMI 001; on ack: SIGNED_ON, new session scope, SAF drain."""
        self.session.sign_on_count += 1
        self.session.business_date = self._business_date()
        self.session.session_ref = (
            f"{self.session.business_date.isoformat()}-{self.session.sign_on_count:03d}"
        )
        self.session.advance("sign_on")
        response = await self._nm_exchange("001")
        if response is None or response.get(39) != "00":
            self.session.advance("sign_on_failed")
            return False
        self.session.advance("sign_on_ack")
        self.session.missed_echo_count = 0
        self.session.signed_on_at = self._clock.now()
        await self.drain_store_and_forward()
        return True

    async def echo_cycle(self) -> bool:
        """One 60s echo (NMI 301). Third consecutive miss signs the session off."""
        if self.session.state not in ("SIGNED_ON", "ECHO_MISSED"):
            raise SessionTransitionDenied(f"echo from {self.session.state} is DENIED")
        response = await self._nm_exchange("301")
        if response is not None and response.get(39) == "00":
            self.session.advance("echo_ok")
            self.session.missed_echo_count = 0
            self.session.last_echo_at = self._clock.now()
            return True
        self.session.missed_echo_count += 1
        if self.session.state == "SIGNED_ON":
            self.session.advance("echo_missed")
        elif self.session.missed_echo_count >= 3:
            self.session.advance("third_miss")
            return False
        else:
            self.session.advance("echo_missed")
        return False

    async def cutover(self) -> bool:
        """0800 NMI 201 at the BB cutover time: new business date, STAN reset."""
        self.session.advance("cutover_window")
        response = await self._nm_exchange("201")
        if response is None or response.get(39) != "00":
            # Cutover not acknowledged: session integrity is unknown — refuse
            # traffic until a fresh sign-on (fail-closed; not in happy table).
            self.session.state = "SIGNED_OFF"
            self.session.history.append(("CUTOVER_IN_PROGRESS", "cutover_failed", "SIGNED_OFF"))
            return False
        self.session.advance("cutover_ack")
        self.session.business_date = self._business_date()
        self.session.session_ref = (
            f"{self.session.business_date.isoformat()}-{self.session.sign_on_count:03d}"
        )
        return True

    async def sign_off(self) -> bool:
        self.session.advance("sign_off")
        await self._nm_exchange("002")
        return True

    # -- PaymentConnector ------------------------------------------------------------

    async def submit(self, instruction: PaymentInstruction) -> ConnectorResult:
        ref = instruction.connector_ref
        self._instruction_by_ref[ref] = instruction.instruction_id
        if instruction.method not in self.supported_methods:
            return self._synthetic(
                instruction.instruction_id, ref, ConnectorStatus.REJECTED, "unsupported_method"
            )
        amount_minor = instruction.amount.amount_minor
        if (
            isinstance(amount_minor, bool)
            or not isinstance(amount_minor, int)
            or amount_minor <= 0
            or amount_minor > 999_999_999_999
        ):
            return self._synthetic(
                instruction.instruction_id, ref, ConnectorStatus.REJECTED, "invalid_amount"
            )
        qr_fields: dict[int, str] = {}
        if instruction.method == "BANGLA_QR":
            # Refusal-first (spec/00): a Bangla-QR financial message without
            # its DE48 forensics sub-fields is incomplete — never sent.
            if not instruction.metadata.get("qr_payload_hash"):
                return self._synthetic(
                    instruction.instruction_id,
                    ref,
                    ConnectorStatus.REJECTED,
                    "qr_metadata_missing",
                )
            try:
                qr_fields = self._qr_de_fields(instruction)
            except (QrEncodeError, Iso8583Error):
                return self._synthetic(
                    instruction.instruction_id,
                    ref,
                    ConnectorStatus.REJECTED,
                    "qr_metadata_invalid",
                )
        existing = self._stans.find_by_ref(ref)
        if existing is not None:
            # Idempotency on connector_ref (A.3 / CERT-01): re-query the
            # original message's outcome instead of re-sending.
            return await self.query_status(ref, existing.rrn)
        if not self.session.dispatchable:
            return self._synthetic(
                instruction.instruction_id, ref, ConnectorStatus.FAILED, "rail_session_down"
            )
        try:
            allocation = self._allocate(instruction.instruction_id, ref)
        except StanExhaustedError:
            return self._synthetic(
                instruction.instruction_id, ref, ConnectorStatus.FAILED, "stan_exhausted"
            )
        de12, de13 = self._local_de12_de13()
        de7 = self._de7()
        self._sent_de7[allocation.stan] = de7
        fields: dict[int, str] = {
            3: PROC_IBFT_CREDIT,
            4: f"{amount_minor:012d}",
            7: de7,
            11: allocation.stan,
            12: de12,
            13: de13,
            32: self._acquiring_inst_id,
            37: allocation.rrn,
            41: self._terminal_id,
            49: "050",
            102: instruction.sender_ref,
            103: instruction.beneficiary_ref,
        }
        # BANGLA_QR: spec/13 §D rows (DE3=260000, DE48.10/.11, ...) overlay
        # the IBFT defaults — ``npsb_qr_dictionary_rows`` is the source.
        fields.update(qr_fields)
        if self._sim_hint_de63:
            hint = instruction.metadata.get("sim_scenario")
            if hint:
                fields[63] = f"SIM:{hint}"
        try:
            fields = self._macd("0200", fields)
        except Exception:
            return self._synthetic(
                instruction.instruction_id, ref, ConnectorStatus.FAILED, "mac_unavailable"
            )
        payload = pack_message("0200", fields, self._dictionary)
        response = await self._transport.exchange(payload)  # hard timeout = runner's
        return self._financial_response(
            instruction.instruction_id, ref, allocation.stan, allocation.rrn, response
        )

    def _financial_response(
        self,
        instruction_id: str,
        connector_ref: str,
        stan: str,
        rrn: str,
        response: bytes,
        *,
        report_rrn: str | None = None,
    ) -> ConnectorResult:
        try:
            mti, fields = unpack_message(response, self._dictionary)
        except Iso8583Error as exc:
            raw = {"connector_id": CONNECTOR_ID, "wire_hex": response.hex(),
                   "decode_error": exc.code}
            return self._result(
                instruction_id,
                connector_ref,
                ConnectorStatus.FAILED,
                rail_transaction_id=None,
                error_code=exc.code,
                raw=raw,
            )
        raw = self._wire_raw(response, mti, fields)
        if not self._verify_response_mac(mti, fields):
            return self._result(
                instruction_id,
                connector_ref,
                ConnectorStatus.FAILED,
                rail_transaction_id=None,
                error_code="security_violation",
                raw=raw,
            )
        if fields.get(11) != stan or fields.get(37, rrn) != rrn:
            return self._result(
                instruction_id,
                connector_ref,
                ConnectorStatus.FAILED,
                rail_transaction_id=None,
                error_code="stan_mismatch",
                raw=raw,
            )
        status, error_code = map_de39(fields.get(39, ""))
        return self._result(
            instruction_id,
            connector_ref,
            status,
            rail_transaction_id=report_rrn if report_rrn is not None else rrn,
            error_code=error_code,
            raw=raw,
        )

    def _recorded_reversal_result(self, connector_ref: str) -> ConnectorResult | None:
        recorded = self._reversed_refs.get(connector_ref)
        if recorded is not None:
            return recorded
        saf = self._saf.acked_by_ref(connector_ref)
        if saf is None:
            return None
        original = self._stans.find_by_ref(connector_ref)
        instruction_id = self._instruction_by_ref.get(connector_ref)
        if instruction_id is None and original is not None:
            instruction_id = original.instruction_id
        responded_at = (
            saf.acked_at.strftime("%Y-%m-%dT%H:%M:%SZ")
            if saf.acked_at is not None
            else None
        )
        return ConnectorResult(
            instruction_id=instruction_id or "",
            connector_ref=connector_ref,
            status=ConnectorStatus.REVERSED,
            rail_transaction_id=saf.acked_rail_transaction_id,
            responded_at=responded_at,
            error_code=None,
            raw_response_hash=saf.ack_raw_response_hash or "",
        )

    async def query_status(
        self, connector_ref: str, rail_transaction_id: str | None
    ) -> ConnectorResult:
        instruction_id = self._instruction_by_ref.get(connector_ref, "")
        reversed_truth = self._recorded_reversal_result(connector_ref)
        if reversed_truth is not None:
            return reversed_truth
        original = self._stans.find_by_ref(connector_ref)
        if original is None:
            return self._synthetic(
                instruction_id, connector_ref, ConnectorStatus.FAILED, "not_received"
            )
        if not self.session.dispatchable:
            return self._synthetic(
                instruction_id, connector_ref, ConnectorStatus.FAILED, "rail_session_down"
            )
        inquiry = self._allocate(None, None)
        de12, de13 = self._local_de12_de13()
        fields: dict[int, str] = {
            3: PROC_STATUS_INQUIRY,
            7: self._de7(),
            11: inquiry.stan,
            12: de12,
            13: de13,
            32: self._acquiring_inst_id,
            37: inquiry.rrn,
            41: self._terminal_id,
            49: "050",
            90: build_de90(
                "0200",
                original.stan,
                self._sent_de7.get(original.stan, "0" * 10),
                self._acquiring_inst_id,
            ),
        }
        try:
            fields = self._macd("0200", fields)
        except Exception:
            return self._synthetic(
                instruction_id, connector_ref, ConnectorStatus.FAILED, "mac_unavailable"
            )
        payload = pack_message("0200", fields, self._dictionary)
        response = await self._transport.exchange(payload)
        # The inquiry has its own STAN/RRN on the wire, but the result speaks
        # about the ORIGINAL transaction: report the original RRN so an
        # idempotent replay equals the first submit (CERT-01 equality).
        return self._financial_response(
            instruction_id, connector_ref, inquiry.stan, inquiry.rrn, response,
            report_rrn=original.rrn,
        )

    async def reverse(
        self,
        connector_ref: str,
        rail_transaction_id: str | None,
        reverse_amount: Money,
        reason: str,
    ) -> ConnectorResult:
        instruction_id = self._instruction_by_ref.get(connector_ref, "")
        recorded = self._recorded_reversal_result(connector_ref)
        if recorded is not None:
            return recorded  # idempotent on connector_ref
        original = self._stans.find_by_ref(connector_ref)
        if original is None:
            return self._synthetic(
                instruction_id, connector_ref, ConnectorStatus.FAILED, "not_received"
            )
        saf = self._find_or_queue_saf(connector_ref, original)
        if not self.session.dispatchable:
            return self._synthetic(
                instruction_id, connector_ref, ConnectorStatus.PENDING, "reversal_queued"
            )
        return await self._send_reversal(instruction_id, connector_ref, saf)

    def _find_or_queue_saf(self, connector_ref: str, original: StanAllocation) -> SafItem:
        existing = self._saf.pending_by_ref(connector_ref)
        if existing is not None:
            return existing
        fields: dict[int, str] = {
            3: PROC_IBFT_CREDIT,
            7: self._de7(),
            32: self._acquiring_inst_id,
            41: self._terminal_id,
            49: "050",
            90: build_de90(
                "0200",
                original.stan,
                self._sent_de7.get(original.stan, "0" * 10),
                self._acquiring_inst_id,
            ),
        }
        item = SafItem(
            saf_id=make_connector_id(
                "saf",
                {
                    "connector_id": CONNECTOR_ID,
                    "connector_ref": connector_ref,
                    "original_stan": original.stan,
                },
            ),
            connector_id=CONNECTOR_ID,
            connector_ref=connector_ref,
            original_stan=original.stan,
            reversal_fields=fields,
            queued_at=self._clock.now(),
        )
        self._saf.insert(item)
        return item

    async def _send_reversal(
        self, instruction_id: str, connector_ref: str, saf: SafItem
    ) -> ConnectorResult:
        mti = saf.reversal_mti
        allocation = self._allocate(None, None)
        saf.attempt_count += 1
        saf.reversal_mti = "0421"
        self._saf.save(saf)
        de12, de13 = self._local_de12_de13()
        fields = dict(saf.reversal_fields)
        fields.update({7: self._de7(), 11: allocation.stan, 12: de12, 13: de13,
                       37: allocation.rrn})
        try:
            fields = self._macd(mti, fields)
        except Exception:
            self._saf.save(saf)
            return self._synthetic(
                instruction_id, connector_ref, ConnectorStatus.FAILED, "mac_unavailable"
            )
        payload = pack_message(mti, fields, self._dictionary)
        try:
            response = await self._wait_for(
                self._transport.exchange(payload), timeout=self._nm_timeout_s
            )
        except (TimeoutError, ConnectionError):
            self._saf.save(saf)
            return self._synthetic(
                instruction_id, connector_ref, ConnectorStatus.PENDING, "reversal_queued"
            )
        try:
            resp_mti, resp_fields = unpack_message(response, self._dictionary)
        except Iso8583Error:
            self._saf.save(saf)
            return self._synthetic(
                instruction_id, connector_ref, ConnectorStatus.PENDING, "reversal_queued"
            )
        raw = self._wire_raw(response, resp_mti, resp_fields)
        if (
            resp_mti == "0430"
            and resp_fields.get(11) == allocation.stan
            and self._verify_response_mac(resp_mti, resp_fields)
            and resp_fields.get(39) == "00"
        ):
            result = self._result(
                instruction_id,
                connector_ref,
                ConnectorStatus.REVERSED,
                rail_transaction_id=allocation.rrn,
                error_code=None,
                raw=raw,
            )
            saf.acked = True
            saf.acked_at = self._clock.now()
            saf.acked_rail_transaction_id = result.rail_transaction_id
            saf.ack_raw_response_hash = result.raw_response_hash
            self._saf.save(saf)
            self._reversed_refs[connector_ref] = result
            return result
        self._saf.save(saf)
        return self._result(
            instruction_id,
            connector_ref,
            ConnectorStatus.PENDING,
            rail_transaction_id=allocation.rrn,
            error_code="reversal_queued",
            raw=raw,
        )

    async def drain_store_and_forward(self) -> list[ConnectorResult]:
        """FIFO 0421 drain on SIGNED_ON; rows leave only on an acked 0430."""
        results: list[ConnectorResult] = []
        for item in list(self._saf.pending()):
            if not self.session.dispatchable:
                continue
            item.reversal_mti = "0421" if item.attempt_count else item.reversal_mti
            instruction_id = self._instruction_by_ref.get(item.connector_ref, "")
            results.append(
                await self._send_reversal(instruction_id, item.connector_ref, item)
            )
        return results

    def pending_saf(self) -> list[SafItem]:
        return self._saf.pending()

    async def health_check(self) -> bool:
        return self.session.dispatchable

    # -- inbound (late async messages) ---------------------------------------------------

    def record_late_response(self, connector_ref: str, raw_hash: str) -> None:
        self.late_responses.append(
            {
                "connector_ref": connector_ref,
                "late_response_after_reversal": connector_ref in self._reversed_refs,
                "raw_response_hash": raw_hash,
            }
        )


class NpsbWebhookHandler:
    """Inbound async ISO messages via the spec/10 loopback (spec/11 A.5).

    ``verify_signature`` = MAC verification through the injected MacService
    (HSM surface in PRODUCTION); fail/HSM-unavailable -> False -> REJECTED,
    fail-closed. ``parse_event`` = codec decode + DE39 mapping. A late
    response for an already-reversed ref is recorded on the adapter — the
    reversal stands; the recon layer consumes the flag (spec/04).
    """

    connector_id = CONNECTOR_ID

    def __init__(self, adapter: NpsbConnector, *, mac: MacService,
                 dictionary: dict | None = None) -> None:
        self._adapter = adapter
        self._mac = mac
        self._dictionary = dictionary if dictionary is not None else NPSB_V28_DICTIONARY

    def verify_signature(self, headers: dict, body: bytes) -> bool:
        try:
            mti, fields = unpack_message(body, self._dictionary)
        except Iso8583Error:
            return False
        mac = fields.get(64)
        if mac is None:
            return False
        return self._mac.verify_mac(mac_preimage(mti, fields), mac)

    def parse_event(self, headers: dict, body: bytes) -> ConnectorResult:
        mti, fields = unpack_message(body, self._dictionary)
        status, error_code = map_de39(fields.get(39, ""))
        stan = fields.get(11, "")
        allocation = self._adapter.stan_store.find_by_stan(stan)
        connector_ref = allocation.connector_ref if allocation is not None else ""
        instruction_id = allocation.instruction_id if allocation is not None else ""
        raw = {
            "connector_id": CONNECTOR_ID,
            "wire_hex": body.hex(),
            "mti": mti,
            "stan": stan,
            "response_code": fields.get(39),
        }
        raw_hash = sha256_canonical(raw)
        if connector_ref:
            self._adapter.record_late_response(connector_ref, raw_hash)
        return ConnectorResult(
            instruction_id=instruction_id or "",
            connector_ref=connector_ref or "",
            status=status,
            rail_transaction_id=fields.get(37),
            responded_at=None,
            error_code=error_code,
            raw_response_hash=raw_hash,
        )

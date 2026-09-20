"""TCSA monitor (spec/05) — 60-second coverage snapshots, shortfall blocking.

``TcsaMonitor.run_once`` computes one TcsaSnapshot from the ledger's own
balances (REPEATABLE READ semantics on Postgres; atomic dict reads on the
in-memory store): TCSA mirror balance vs outstanding liabilities
(merchant settlement, plus customer e-money float in PSP mode). A shortfall
flips ``payout_initiation_blocked`` to True, emits the
``tcsa_snapshot.shortfall_detected`` outbox event, and raises the
approval-queue alert; recovery emits ``tcsa_snapshot.shortfall_resolved``.

``TcsaDispatchGate`` is the spec/04 FSM-1 hard gate the settlement engine
calls synchronously before any dispatch: absent snapshot, stale snapshot
(monitor down), or blocked snapshot all fail CLOSED.

Balance-sign note (errata LA-L3/LA-L11): the spec/05 derivation comment sums
CREDIT−DEBIT for every subtype, which would make the ASSET-class TCSA mirror
negative exactly when it holds funds. Balances here are normal-balance signed
sums (ASSET debit-positive; LIABILITY credit-positive), so a fully covered
TCSA yields ``tcsa_balance_minor >= total_outstanding_liability_minor``; the
spec/05 subtype name 'TCSA' resolves to the canonical 'SPONSOR_BANK_TCSA'.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

import psycopg

from bdpay.ledger.errors import TcsaError
from bdpay.ledger.service import MONEY_DOMAIN, LedgerService
from bdpay.ledger.settlement.engine import TcsaGateResult
from bdpay.ledger.types import AuditEventSpec
from bdpay.platform.clock import Clock
from bdpay.platform.config import Settings
from bdpay.platform.ids import make_id
from bdpay.platform.observability import MetricsRegistry
from bdpay.platform.scheduler import ScheduledTask

__all__ = [
    "EXTENDED_SHORTFALL_DAILY_FINE_MINOR",
    "EXTENDED_SHORTFALL_DAYS_THRESHOLD",
    "SLF_RATE_BPS_DEFAULT",
    "TSA_SHORTFALL_MAX_PENALTY_MINOR",
    "InMemoryTcsaStore",
    "PostgresTcsaStore",
    "TcsaDispatchGate",
    "TcsaMonitor",
    "TcsaSnapshotRow",
    "TcsaStore",
    "compute_penalty_exposure_minor",
]

PRODUCER = "tcsa-monitor@1"

#: research 01 §8.2 penalty parameters (paisa).
SLF_RATE_BPS_DEFAULT = 1150  # 11.50% in basis points
TSA_SHORTFALL_MAX_PENALTY_MINOR = 300_000_000  # BDT 30 lakh
EXTENDED_SHORTFALL_DAILY_FINE_MINOR = 1_000_000  # BDT 10,000 per day
EXTENDED_SHORTFALL_DAYS_THRESHOLD = 14

#: spec/04 stale-snapshot guard: monitor runs every 60s; older than 90s is down.
SNAPSHOT_MAX_AGE_SECONDS = 90


@dataclass(frozen=True, slots=True)
class TcsaSnapshotRow:
    """tcsa_snapshots row (spec/05 DDL; unpartitioned per errata LA-L1)."""

    snapshot_id: str
    snapshotted_at: datetime
    platform_mode: str
    tcsa_balance_minor: int
    currency: str
    outstanding_merchant_liability_minor: int
    outstanding_customer_float_minor: int
    total_outstanding_liability_minor: int
    coverage_surplus_minor: int
    coverage_ratio_bps: int | None
    status: str  # OK | SHORTFALL | BLOCKED
    shortfall_minor: int
    shortfall_days_running: int
    penalty_exposure_minor: int
    payout_initiation_blocked: bool
    alert_dispatched: bool
    ledger_chain_index_at_snap: int
    ledger_chain_hash_at_snap: str
    produced_by: str
    schema_version: int = 1


class TcsaStore(Protocol):
    """Persistence boundary for TCSA snapshots (append-only)."""

    def insert_snapshot(self, row: TcsaSnapshotRow) -> None: ...

    def latest_snapshot(self) -> TcsaSnapshotRow | None: ...

    def list_snapshots(self) -> list[TcsaSnapshotRow]: ...


class InMemoryTcsaStore:
    """Deterministic in-memory store (append-only by construction)."""

    def __init__(self) -> None:
        self._rows: list[TcsaSnapshotRow] = []

    def insert_snapshot(self, row: TcsaSnapshotRow) -> None:
        if any(existing.snapshot_id == row.snapshot_id for existing in self._rows):
            raise TcsaError(f"duplicate snapshot_id {row.snapshot_id}")
        self._rows.append(row)

    def latest_snapshot(self) -> TcsaSnapshotRow | None:
        return self._rows[-1] if self._rows else None

    def list_snapshots(self) -> list[TcsaSnapshotRow]:
        return list(self._rows)


_SNAPSHOT_COLUMNS = (
    "snapshot_id, snapshotted_at, platform_mode, tcsa_balance_minor, currency, "
    "outstanding_merchant_liability_minor, outstanding_customer_float_minor, "
    "total_outstanding_liability_minor, coverage_surplus_minor, coverage_ratio_bps, status, "
    "shortfall_minor, shortfall_days_running, penalty_exposure_minor, "
    "payout_initiation_blocked, alert_dispatched, ledger_chain_index_at_snap, "
    "ledger_chain_hash_at_snap, produced_by, schema_version"
)


class PostgresTcsaStore:
    """Sync psycopg3 store matching 0014_tcsa.sql (append-only + RLS)."""

    def __init__(self, conn: psycopg.Connection[tuple[Any, ...]]) -> None:
        if not conn.autocommit:
            raise ValueError("PostgresTcsaStore requires an autocommit connection")
        self._conn = conn

    @classmethod
    def connect(cls, dsn: str, *, role: str | None = None) -> PostgresTcsaStore:
        conn = psycopg.connect(dsn, autocommit=True, options="-c TimeZone=UTC")
        if role is not None:
            conn.execute(psycopg.sql.SQL("SET ROLE {}").format(psycopg.sql.Identifier(role)))
        return cls(conn)

    def close(self) -> None:
        self._conn.close()

    def insert_snapshot(self, row: TcsaSnapshotRow) -> None:
        # total/coverage are GENERATED columns in the DDL — not inserted.
        self._conn.execute(
            "INSERT INTO ledger.tcsa_snapshots (snapshot_id, snapshotted_at, platform_mode,"
            " tcsa_balance_minor, currency, outstanding_merchant_liability_minor,"
            " outstanding_customer_float_minor, coverage_ratio_bps, status, shortfall_minor,"
            " shortfall_days_running, penalty_exposure_minor, payout_initiation_blocked,"
            " alert_dispatched, ledger_chain_index_at_snap, ledger_chain_hash_at_snap,"
            " produced_by, schema_version)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                row.snapshot_id,
                row.snapshotted_at,
                row.platform_mode,
                row.tcsa_balance_minor,
                row.currency,
                row.outstanding_merchant_liability_minor,
                row.outstanding_customer_float_minor,
                row.coverage_ratio_bps,
                row.status,
                row.shortfall_minor,
                row.shortfall_days_running,
                row.penalty_exposure_minor,
                row.payout_initiation_blocked,
                row.alert_dispatched,
                row.ledger_chain_index_at_snap,
                row.ledger_chain_hash_at_snap,
                row.produced_by,
                row.schema_version,
            ),
        )

    def _row(self, row: tuple[Any, ...]) -> TcsaSnapshotRow:
        return TcsaSnapshotRow(*row)

    def latest_snapshot(self) -> TcsaSnapshotRow | None:
        cur = self._conn.execute(
            f"SELECT {_SNAPSHOT_COLUMNS} FROM ledger.tcsa_snapshots"
            " ORDER BY snapshotted_at DESC, snapshot_id DESC LIMIT 1"
        )
        row = cur.fetchone()
        return self._row(row) if row else None

    def list_snapshots(self) -> list[TcsaSnapshotRow]:
        cur = self._conn.execute(
            f"SELECT {_SNAPSHOT_COLUMNS} FROM ledger.tcsa_snapshots"
            " ORDER BY snapshotted_at, snapshot_id"
        )
        return [self._row(row) for row in cur.fetchall()]


def compute_penalty_exposure_minor(
    shortfall_minor: int, days_running: int, *, slf_rate_bps: int = SLF_RATE_BPS_DEFAULT
) -> int:
    """spec/05: min(BDT 30 lakh, SLF-rate share of the shortfall) plus
    BDT 10,000/day for each day beyond day 14. All integer paisa."""
    if shortfall_minor <= 0:
        return 0
    rate_based = (slf_rate_bps * shortfall_minor) // 10_000
    base_penalty = min(TSA_SHORTFALL_MAX_PENALTY_MINOR, rate_based)
    extra_days = max(0, days_running - EXTENDED_SHORTFALL_DAYS_THRESHOLD)
    return base_penalty + extra_days * EXTENDED_SHORTFALL_DAILY_FINE_MINOR


class TcsaMonitor:
    """tcsa-monitor@1 — the 60-second coverage loop (spec/05).

    A snapshot row is written EVERY cycle regardless of status; the snapshot
    is append-only. Shortfall blocks payout initiation immediately (the flag
    is in the snapshot row itself, before any external alert dispatch).
    """

    def __init__(
        self,
        ledger: LedgerService,
        tcsa_store: TcsaStore,
        *,
        clock: Clock,
        settings: Settings,
        alert: Callable[[str, dict], None] | None = None,
        slf_rate_bps: int = SLF_RATE_BPS_DEFAULT,
        producer: str = PRODUCER,
        metrics: MetricsRegistry | None = None,
    ) -> None:
        self._ledger = ledger
        self._tcsa_store = tcsa_store
        self._clock = clock
        self._settings = settings
        self._alert = alert or (lambda _kind, _payload: None)
        self._slf_rate_bps = slf_rate_bps
        self._producer = producer
        self._metrics = metrics

    def run_once(self) -> TcsaSnapshotRow:
        now = self._now()
        store = self._ledger.store
        tcsa_balance = store.sum_balance_by_subtype("SPONSOR_BANK_TCSA")
        merchant_liability = store.sum_balance_by_subtype("MERCHANT_SETTLEMENT")
        customer_float = 0
        if self._settings.platform_mode == "PSP":
            customer_float = store.sum_balance_by_subtype("CUSTOMER_FLOAT")

        total_liability = merchant_liability + customer_float
        shortfall = max(0, total_liability - tcsa_balance)
        status = "OK" if shortfall == 0 else "SHORTFALL"
        coverage_ratio_bps = (
            (tcsa_balance * 10_000) // total_liability if total_liability > 0 else None
        )
        previous = self._tcsa_store.latest_snapshot()
        shortfall_days = self._shortfall_days_running(previous, shortfall > 0, now)
        penalty = compute_penalty_exposure_minor(
            shortfall, shortfall_days, slf_rate_bps=self._slf_rate_bps
        )

        chain_tip_index = store.max_chain_index(MONEY_DOMAIN)
        if chain_tip_index is None:
            self._ledger.initialize_chain(MONEY_DOMAIN)
            chain_tip_index = store.max_chain_index(MONEY_DOMAIN)
            assert chain_tip_index is not None
        chain_tip = store.get_chain_entry(MONEY_DOMAIN, chain_tip_index)
        assert chain_tip is not None

        payload = {
            "snapshotted_at": now,
            "platform_mode": self._settings.platform_mode,
            "tcsa_balance_minor": tcsa_balance,
            "outstanding_merchant_liability_minor": merchant_liability,
            "outstanding_customer_float_minor": customer_float,
            "status": status,
            "shortfall_minor": shortfall,
            "ledger_chain_index_at_snap": chain_tip.chain_index,
        }
        snapshot_id = make_id("tcsa", payload)
        row = TcsaSnapshotRow(
            snapshot_id=snapshot_id,
            snapshotted_at=now,
            platform_mode=self._settings.platform_mode,
            tcsa_balance_minor=tcsa_balance,
            currency="BDT",
            outstanding_merchant_liability_minor=merchant_liability,
            outstanding_customer_float_minor=customer_float,
            total_outstanding_liability_minor=total_liability,
            coverage_surplus_minor=tcsa_balance - total_liability,
            coverage_ratio_bps=coverage_ratio_bps,
            status=status,
            shortfall_minor=shortfall,
            shortfall_days_running=shortfall_days,
            penalty_exposure_minor=penalty,
            payout_initiation_blocked=shortfall > 0,
            alert_dispatched=False,
            ledger_chain_index_at_snap=chain_tip.chain_index,
            ledger_chain_hash_at_snap=chain_tip.chain_hash,
            produced_by=self._producer,
        )

        # Monitoring contract (monitoring/README.md + monitoring/prometheus-rules.yml):
        # one counter tick per completed cycle — a stalled tick stream is how the
        # TcsaSnapshotEventLag alert detects "monitor down / shortfall events would
        # lag" without any wall-clock metric. The coverage gauge mirrors
        # coverage_ratio_bps; zero outstanding liability (ratio None) exports as
        # 10_000 bps — fully covered by definition.
        if self._metrics is not None:
            self._metrics.increment("tcsa_snapshots_total")
            self._metrics.set_gauge(
                "tcsa_coverage_ratio_bps",
                coverage_ratio_bps if coverage_ratio_bps is not None else 10_000,
            )

        was_blocked = previous is not None and previous.payout_initiation_blocked
        if shortfall > 0 and not was_blocked:
            # Block first, then alert (spec/05 ordering).
            self._tcsa_store.insert_snapshot(row)
            self._ledger.emit_event(
                event_type="tcsa_snapshot.shortfall_detected",
                subject_type="TcsaSnapshot",
                subject_id=snapshot_id,
                payload={
                    "shortfall_minor": shortfall,
                    "penalty_exposure_minor": penalty,
                    "shortfall_days_running": shortfall_days,
                    "payout_initiation_blocked": True,
                },
                topic="settlement.events",
                producer=self._producer,
            )
            self._ledger.write_audit_event(
                AuditEventSpec(
                    event_type="TCSA_SHORTFALL_DETECTED",
                    actor_id=self._producer,
                    actor_type="SERVICE",
                    subject_type="TcsaSnapshot",
                    subject_id=snapshot_id,
                    payload={"shortfall_minor": shortfall},
                )
            )
            self._alert(
                "tcsa-shortfall",
                {"snapshot_id": snapshot_id, "shortfall_minor": shortfall},
            )
            return row
        if shortfall == 0 and was_blocked:
            self._tcsa_store.insert_snapshot(row)
            self._ledger.emit_event(
                event_type="tcsa_snapshot.shortfall_resolved",
                subject_type="TcsaSnapshot",
                subject_id=snapshot_id,
                payload={"payout_initiation_blocked": False},
                topic="settlement.events",
                producer=self._producer,
            )
            self._ledger.write_audit_event(
                AuditEventSpec(
                    event_type="TCSA_SHORTFALL_RESOLVED",
                    actor_id=self._producer,
                    actor_type="SERVICE",
                    subject_type="TcsaSnapshot",
                    subject_id=snapshot_id,
                    payload={},
                )
            )
            return row
        self._tcsa_store.insert_snapshot(row)
        return row

    def _shortfall_days_running(
        self, previous: TcsaSnapshotRow | None, currently_in_shortfall: bool, now: datetime
    ) -> int:
        """Consecutive BD calendar days in shortfall, carried across snapshots."""
        if not currently_in_shortfall:
            return 0
        if previous is None or previous.shortfall_minor == 0:
            return 1
        prev_day = previous.snapshotted_at.astimezone(UTC).date()
        if now.astimezone(UTC).date() > prev_day:
            return previous.shortfall_days_running + 1
        return max(previous.shortfall_days_running, 1)

    def as_scheduled_task(self, *, cadence_seconds: float = 60.0) -> ScheduledTask:
        """The spec/05 60-second scheduler registration."""

        async def _tick() -> TcsaSnapshotRow:
            return self.run_once()

        return ScheduledTask(
            name="tcsa-monitor", fn=_tick, cadence_seconds=cadence_seconds, window_gate="ALWAYS"
        )

    def _now(self) -> datetime:
        dt = self._clock.now()
        if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
            raise TcsaError("clock returned a naive datetime (spec/00 §7)")
        return dt.astimezone(UTC)


class TcsaDispatchGate:
    """spec/04 §TCSA dispatch gate — synchronous, hard, fails CLOSED.

    - no snapshot ever            -> closed
    - latest snapshot older 90s   -> closed (monitor down; distinct alert)
    - payout_initiation_blocked   -> closed
    - fresh + unblocked           -> clear
    """

    def __init__(
        self,
        tcsa_store: TcsaStore,
        *,
        clock: Clock,
        max_age_seconds: int = SNAPSHOT_MAX_AGE_SECONDS,
    ) -> None:
        self._tcsa_store = tcsa_store
        self._clock = clock
        self._max_age = max_age_seconds

    def __call__(self) -> TcsaGateResult:
        snapshot = self._tcsa_store.latest_snapshot()
        if snapshot is None:
            return TcsaGateResult(clear=False, snapshot_id=None, shortfall_minor=0)
        now = self._clock.now()
        age = (now - snapshot.snapshotted_at).total_seconds()
        if age > self._max_age:
            return TcsaGateResult(
                clear=False,
                snapshot_id=snapshot.snapshot_id,
                shortfall_minor=snapshot.shortfall_minor,
                stale=True,
            )
        if snapshot.payout_initiation_blocked:
            return TcsaGateResult(
                clear=False,
                snapshot_id=snapshot.snapshot_id,
                shortfall_minor=snapshot.shortfall_minor,
            )
        return TcsaGateResult(clear=True, snapshot_id=snapshot.snapshot_id, shortfall_minor=0)

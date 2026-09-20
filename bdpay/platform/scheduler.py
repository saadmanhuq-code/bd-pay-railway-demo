"""Injectable-clock scheduler + Bangladesh Bank operating calendar.

Ported from ``dse-profit-engine/src/dse_engine/scheduler/scheduler.py``
(ADAPT, per PORTING-MAP.md): the cadence-driven async runner is retained —
per-task due-times, semaphore-bounded concurrency, single-tick exception
isolation, tick counters, injectable clock and sleep, and the TEST-ONLY
``max_iterations`` loop guard. The DSE market-hours gate is REPLACED by a
:class:`BangladeshBankCalendar` window gate (the DSE holiday table was empty —
arch §(g) mandates authoring the BB calendar fresh).

Calendar facts (research/02 §2–3, arch §(e)):

- Weekend is Friday + Saturday (Bangladesh working week is Sunday–Thursday).
- BEFTN runs three sessions per working day (morning 00:00–11:59, afternoon
  12:00–14:59, end-of-day from 15:00) with a **12:30 same-day cutoff** —
  files submitted later, or on weekends/holidays, queue for the next working
  day.
- RTGS submission window defaults to **09:30–15:30** local (arch §(g) row;
  the calendar takes the window as config so the gazetted hours can be set
  without code change — see SPEC_ERRATA-LANE-A-platform-runtime.md row PR-4).
- The public-holiday list is injectable config data; a 2026 seed table ships
  below.

All datetimes are timezone-aware; naive input is rejected (spec/00 §7).
Local-time decisions convert to Asia/Dhaka internally; everything returned is
UTC.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

from bdpay.platform.clock import Clock

__all__ = [
    "ALWAYS_REQUIRED_SCHEDULER_TASKS",
    "BD_PUBLIC_HOLIDAYS_2026",
    "CONDITIONAL_SCHEDULER_TASKS",
    "DHAKA_TZ",
    "REQUIRED_SCHEDULER_TASKS",
    "WINDOW_GATES",
    "BangladeshBankCalendar",
    "BeftnSession",
    "ScheduledTask",
    "Scheduler",
]

DHAKA_TZ = ZoneInfo("Asia/Dhaka")

#: Python ``date.weekday()`` numbers for Friday and Saturday.
_WEEKEND = (4, 5)

# ---------------------------------------------------------------------------
# CONFIG DATA — 2026 Bangladesh public-holiday seed table.
#
# This is configuration, not law: the binding source is the Bangladesh Bank
# gazetted holiday circular for the year, and ops MUST reconcile this table
# against it (and against any executive-order adjustments) before each year
# starts. Lunar-calendar observances (Eid, Ashura, Shab-e-Barat, Eid-e-Milad)
# are PROVISIONAL — actual dates depend on moon sighting and BB's circular.
# The calendar accepts any holiday set by injection; nothing below is
# hard-coded into scheduling logic.
# ---------------------------------------------------------------------------
BD_PUBLIC_HOLIDAYS_2026: frozenset[date] = frozenset(
    {
        date(2026, 2, 2),  # Shab-e-Barat (provisional, lunar)
        date(2026, 2, 21),  # Shaheed Dibosh / Intl Mother Language Day
        date(2026, 3, 19),  # Eid-ul-Fitr holiday (provisional, lunar)
        date(2026, 3, 22),  # Eid-ul-Fitr holiday (provisional, lunar)
        date(2026, 3, 23),  # Eid-ul-Fitr holiday (provisional, lunar)
        date(2026, 3, 26),  # Independence Day
        date(2026, 4, 14),  # Pohela Boishakh (Bengali New Year)
        date(2026, 5, 1),  # May Day
        date(2026, 5, 3),  # Buddha Purnima (provisional, lunar)
        date(2026, 5, 26),  # Eid-ul-Azha holiday (provisional, lunar)
        date(2026, 5, 27),  # Eid-ul-Azha holiday (provisional, lunar)
        date(2026, 5, 28),  # Eid-ul-Azha holiday (provisional, lunar)
        date(2026, 6, 25),  # Ashura (provisional, lunar)
        date(2026, 8, 26),  # Eid-e-Milad-un-Nabi (provisional, lunar)
        date(2026, 10, 20),  # Durga Puja — Vijaya Dashami (provisional, lunar)
        date(2026, 12, 16),  # Victory Day
        date(2026, 12, 25),  # Christmas Day
    }
)

#: Window-gate vocabulary from spec/02 ``scheduler_tasks.window_gate``.
WINDOW_GATES = ("ALWAYS", "BEFTN_SESSION", "RTGS_HOURS", "NPSB_24_7")

#: Spec-mandated scheduler tasks that the production app MUST register
#: unconditionally — regardless of runtime configuration. The boot guard
#: (``ProductionCompositionGuard.assert_boot_invariants``) asserts the union
#: of registered task names across ALL schedulers (kernel, compliance,
#: connector-inbound) supersets this set. Adding a task here without wiring
#: it into a scheduler makes boot FAIL in production — making "handler
#: implemented but never scheduled" impossible to ship.
#:
#: To add a new always-required task:
#:   1. Register it in the appropriate ``build_*_scheduler_tasks`` function.
#:   2. Add its name to this set.
#:   3. The boot guard and ``test_wiring_guard`` test enforce both.
ALWAYS_REQUIRED_SCHEDULER_TASKS: frozenset[str] = frozenset({
    # kernel scheduler (spec/02 + spec/04 settlement resume)
    "ttl-sweep",
    "reversal-dispatch",
    "settlement-confirm-resume",
    # compliance scheduler (spec/06)
    "sanctions_rescreen",
    "ctr_daily_aggregate",
    "ctr_filing",
    "str_filing",
    "str_retry",
    "goaml_submit_sweep",
    "str_ack_confirm",
    # connector-inbound scheduler (spec/02 verified callbacks). Gated on
    # settings.database_url != "memory://" in build_services, but production
    # never runs on memory:// (ProductionCompositionGuard's own offender
    # check refuses that), so it is effectively unconditional in production.
    "connector_inbound_drain",
})

#: Scheduler tasks whose registration legitimately depends on runtime
#: configuration, mapped to a short description of the condition under
#: which each one is required. ``CONNECTOR_MODE=disabled`` is a documented,
#: legitimate deployment posture (see ``bdpay.app._build_disabled_connector_
#: runner``) for a dry-run production boot with no rail connectors active —
#: under that posture ``sanctions_feed_connector`` is None and these two
#: tasks are never registered by ``build_compliance_scheduler_tasks``.  The
#: boot guard resolves the EFFECTIVE required set from the live
#: container/config state (see ``ProductionCompositionGuard``) instead of
#: requiring these unconditionally; when a conditional task is legitimately
#: exempt, the guard logs the exemption rather than silently dropping it or
#: failing boot for it.
CONDITIONAL_SCHEDULER_TASKS: dict[str, str] = {
    "sanctions_feed_poll": (
        "required iff sanctions_feed_connector is configured (spec/12 §G); "
        "exempt when CONNECTOR_MODE=disabled or the connector is unconfigured"
    ),
    "sanctions_staleness": (
        "required iff sanctions_feed_connector is configured (spec/12 §G); "
        "exempt when CONNECTOR_MODE=disabled or the connector is unconfigured"
    ),
    "beftn_return_poll": (
        "required iff the BEFTN settlement connector is configured; "
        "exempt when no BEFTN connector is wired"
    ),
}

#: Backward-compatible full task-name union (always-required + conditional).
#: Callers that only need simple set-membership semantics (docs, tests that
#: enumerate every spec-mandated task name) can keep using this; the boot
#: guard itself resolves the narrower EFFECTIVE required set at runtime —
#: see ``ALWAYS_REQUIRED_SCHEDULER_TASKS`` / ``CONDITIONAL_SCHEDULER_TASKS``.
REQUIRED_SCHEDULER_TASKS: frozenset[str] = ALWAYS_REQUIRED_SCHEDULER_TASKS | frozenset(
    CONDITIONAL_SCHEDULER_TASKS
)


class BeftnSession(StrEnum):
    """The three BEFTN processing sessions on a working day (research/02 §2)."""

    MORNING = "morning"  # 00:00 – 11:59 local
    AFTERNOON = "afternoon"  # 12:00 – 14:59 local
    END_OF_DAY = "end_of_day"  # 15:00 onward local


def _require_aware(value: datetime, what: str = "datetime") -> datetime:
    if not isinstance(value, datetime):
        raise ValueError(f"{what} must be datetime, got {type(value).__name__}")
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError(f"{what} must be timezone-aware (naive rejected, spec 00 §7)")
    return value


class BangladeshBankCalendar:
    """Working-day, BEFTN-session, and RTGS-window calendar for BB rails.

    Holiday list and window times are injectable configuration; defaults are
    the 2026 seed table and the arch-pinned RTGS 09:30–15:30 window.
    """

    def __init__(
        self,
        *,
        holidays: frozenset[date] = BD_PUBLIC_HOLIDAYS_2026,
        rtgs_open: time = time(9, 30),
        rtgs_close: time = time(15, 30),
        beftn_same_day_cutoff: time = time(12, 30),
    ) -> None:
        if rtgs_open >= rtgs_close:
            raise ValueError("rtgs_open must be earlier than rtgs_close")
        self._holidays = frozenset(holidays)
        self._rtgs_open = rtgs_open
        self._rtgs_close = rtgs_close
        self._beftn_cutoff = beftn_same_day_cutoff

    # -- working days --------------------------------------------------------

    def is_working_day(self, day: date) -> bool:
        """True when ``day`` is neither the Fri/Sat weekend nor a holiday."""
        if not isinstance(day, date) or isinstance(day, datetime):
            raise ValueError(f"day must be a date, got {type(day).__name__}")
        return day.weekday() not in _WEEKEND and day not in self._holidays

    def next_working_day(self, day: date) -> date:
        """The first working day strictly after ``day``."""
        candidate = day + timedelta(days=1)
        while not self.is_working_day(candidate):
            candidate += timedelta(days=1)
        return candidate

    def _local(self, at: datetime) -> datetime:
        return _require_aware(at, "at").astimezone(DHAKA_TZ)

    # -- BEFTN ----------------------------------------------------------------

    def beftn_session(self, at: datetime) -> BeftnSession | None:
        """The active BEFTN session at ``at``; None on weekends/holidays."""
        local = self._local(at)
        if not self.is_working_day(local.date()):
            return None
        if local.hour < 12:
            return BeftnSession.MORNING
        if local.hour < 15:
            return BeftnSession.AFTERNOON
        return BeftnSession.END_OF_DAY

    def is_beftn_same_day(self, at: datetime) -> bool:
        """True while a file submitted now still settles same-day (< 12:30)."""
        local = self._local(at)
        return self.is_working_day(local.date()) and local.time() < self._beftn_cutoff

    def next_beftn_cutoff(self, at: datetime) -> datetime:
        """The next same-day cutoff instant (UTC) at or after ``at``."""
        local = self._local(at)
        day = local.date()
        if not self.is_working_day(day) or local.time() >= self._beftn_cutoff:
            day = self.next_working_day(day)
        cutoff_local = datetime.combine(day, self._beftn_cutoff, tzinfo=DHAKA_TZ)
        return cutoff_local.astimezone(UTC)

    # -- RTGS -----------------------------------------------------------------

    def is_rtgs_open(self, at: datetime) -> bool:
        """True inside the RTGS submission window on a working day."""
        local = self._local(at)
        return (
            self.is_working_day(local.date())
            and self._rtgs_open <= local.time() < self._rtgs_close
        )

    def next_rtgs_open(self, at: datetime) -> datetime:
        """The next instant (UTC) at which the RTGS window is open."""
        local = self._local(at)
        if self.is_rtgs_open(at):
            return _require_aware(at).astimezone(UTC)
        day = local.date()
        if not self.is_working_day(day) or local.time() >= self._rtgs_open:
            day = self.next_working_day(day)
        open_local = datetime.combine(day, self._rtgs_open, tzinfo=DHAKA_TZ)
        return open_local.astimezone(UTC)

    # -- gate dispatch ---------------------------------------------------------

    def is_window_open(self, gate: str, at: datetime) -> bool:
        """Evaluate a spec/02 ``window_gate`` value. Unknown gates fail closed."""
        if gate == "ALWAYS" or gate == "NPSB_24_7":
            _require_aware(at, "at")
            return True
        if gate == "RTGS_HOURS":
            return self.is_rtgs_open(at)
        if gate == "BEFTN_SESSION":
            return self.beftn_session(at) is not None
        raise ValueError(f"unknown window gate {gate!r}; allowed: {WINDOW_GATES}")


# ---------------------------------------------------------------------------
# Scheduler (ported runner)
# ---------------------------------------------------------------------------

TaskCallable = Callable[[], Awaitable[object]]
SleepFn = Callable[[float], Awaitable[None]]
TickHook = Callable[[str, datetime, object], None]


@dataclass(frozen=True)
class ScheduledTask:
    """One scheduled task: name, async callable, cadence, window gate."""

    name: str
    fn: TaskCallable
    cadence_seconds: float
    window_gate: str = "ALWAYS"

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("task name must be non-empty")
        if not callable(self.fn):
            raise TypeError(f"task {self.name!r} callable is not callable")
        if self.cadence_seconds <= 0:
            raise ValueError(f"task {self.name!r} cadence_seconds must be positive")
        if self.window_gate not in WINDOW_GATES:
            raise ValueError(
                f"task {self.name!r} window_gate {self.window_gate!r} "
                f"not in {WINDOW_GATES}"
            )


class Scheduler:
    """Cadence-driven async runner gated by the BB calendar.

    A task runs when (a) its cadence is due AND (b) its window gate is open.
    A closed gate skips the task without advancing its due time, so it fires
    as soon as the window opens. One task raising never stops the others
    (single-tick isolation, retained from the DSE source).
    """

    def __init__(
        self,
        tasks: list[ScheduledTask] | tuple[ScheduledTask, ...],
        *,
        clock: Clock,
        calendar: BangladeshBankCalendar | None = None,
        poll_interval_seconds: float = 1.0,
        sleep: SleepFn | None = None,
        max_concurrency: int = 4,
        logger: logging.Logger | None = None,
    ) -> None:
        if not tasks:
            raise ValueError("tasks must be non-empty")
        names = [task.name for task in tasks]
        if len(set(names)) != len(names):
            raise ValueError(f"duplicate task names: {names}")
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be positive")
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be >= 1")

        self._tasks: tuple[ScheduledTask, ...] = tuple(tasks)
        self._clock = clock
        self._calendar = calendar or BangladeshBankCalendar()
        self._poll_interval = float(poll_interval_seconds)
        self._sleep: SleepFn = sleep or asyncio.sleep
        self._max_concurrency = max_concurrency
        self.log = logger or logging.getLogger("bdpay.platform.scheduler")

        # Observability state (retained from the DSE source).
        self.tick_count: dict[str, int] = {task.name: 0 for task in self._tasks}
        self.last_tick_at: dict[str, datetime] = {}
        self.error_count: dict[str, int] = {task.name: 0 for task in self._tasks}
        self._next_due_at: dict[str, datetime] = {}

    # -- public API ------------------------------------------------------------

    async def run(self, *, max_iterations: int | None = None) -> dict[str, int]:
        """Poll-and-dispatch loop. ``max_iterations`` is a TEST-ONLY guard.

        Returns the per-task tick counts accumulated so far.
        """
        iterations = 0
        while True:
            now = self._clock.now()
            await self.run_due(now)
            iterations += 1
            if max_iterations is not None and iterations >= max_iterations:
                self.log.info("max_iterations=%d reached; returning", max_iterations)
                break
            await self._sleep(self._poll_interval)
        return dict(self.tick_count)

    async def run_due(self, now: datetime, *, on_tick: TickHook | None = None) -> int:
        """Run every task that is due AND whose window gate is open at ``now``.

        Deterministic test entrypoint; ``run`` calls this each poll. Returns
        the number of tasks executed this tick.
        """
        now = _require_aware(now, "now")
        due = [
            task
            for task in self._tasks
            if self._next_due_at.get(task.name, now) <= now
            and self._calendar.is_window_open(task.window_gate, now)
        ]
        if not due:
            return 0

        semaphore = asyncio.Semaphore(self._max_concurrency)

        async def _invoke(task: ScheduledTask) -> tuple[str, object | None]:
            async with semaphore:
                self.log.debug("tick %s at %s", task.name, now.isoformat())
                try:
                    result = await task.fn()
                except Exception as exc:  # noqa: BLE001 - single-tick isolation
                    self.log.warning(
                        "task %s raised %s: %s", task.name, type(exc).__name__, exc
                    )
                    self.error_count[task.name] += 1
                    result = None
                self.tick_count[task.name] += 1
                self.last_tick_at[task.name] = now
                self._next_due_at[task.name] = now + timedelta(
                    seconds=task.cadence_seconds
                )
                return task.name, result

        results = await asyncio.gather(*[_invoke(task) for task in due])

        if on_tick is not None:
            for name, result in results:
                try:
                    on_tick(name, now, result)
                except Exception as exc:  # noqa: BLE001 - hook isolation
                    self.log.warning("on_tick hook raised for %s: %s", name, exc)
        return len(results)

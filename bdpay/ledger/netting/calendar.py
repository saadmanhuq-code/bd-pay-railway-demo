"""PSO DNS window calendar + scheduler tasks (spec/19 PSO-2 window calendar).

Sessions ``AM``/``PM``/``EOD`` per spec/05; cutoffs come from the injectable
``PSO_DNS_SESSION_SCHEDULE`` config (defaults 11:59 / 14:59 / 17:59
Asia/Dhaka — spec/05 Open Question 2 assumption, VERIFY-BEFORE-EXTERNAL
FC-07). Windows open and cut ONLY on ``bb_calendar`` working days, and a
0-session day opens no window (``beftn_session_count`` respected via the
injectable session-count provider). Injectable clock throughout — the sweeper
never reads the wall clock.

PLATFORM_MODE gate (binding): ``build_pso_scheduler_tasks`` registers NOTHING
under ``PLATFORM_MODE=PSP`` — absence from the task registry, not a no-op
task (PSO-CERT-07 asserts the absence).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime, time

from bdpay.ledger.errors import PsoModeError
from bdpay.ledger.netting.config import PSO_SESSIONS
from bdpay.ledger.netting.engine import NettingEngine
from bdpay.ledger.netting.errors import NettingIntegrityError
from bdpay.ledger.netting.positions import NettingPositionStore
from bdpay.ledger.position import PositionService
from bdpay.platform.clock import Clock
from bdpay.platform.config import Settings
from bdpay.platform.ids import make_id
from bdpay.platform.scheduler import DHAKA_TZ, BangladeshBankCalendar, ScheduledTask

__all__ = [
    "PsoWindowCalendar",
    "WindowSweeper",
    "build_pso_scheduler_tasks",
]

#: Optional bb_calendar (0015) read: BEFTN session count for a date, or None
#: when the table carries no row (the working-day calendar then decides).
SessionCountProvider = Callable[[date], int | None]


class PsoWindowCalendar:
    """Session arithmetic over the BB working-day calendar (injectable)."""

    def __init__(
        self,
        *,
        calendar: BangladeshBankCalendar,
        session_schedule: Mapping[str, time],
        session_count_provider: SessionCountProvider | None = None,
    ) -> None:
        missing = [s for s in PSO_SESSIONS if s not in session_schedule]
        if missing:
            raise ValueError(f"session_schedule missing session(s) {missing}")
        self._calendar = calendar
        self._schedule = dict(session_schedule)
        self._session_counts = session_count_provider

    def is_session_day(self, day: date) -> bool:
        """Working day AND (no bb_calendar row OR session count > 0)."""
        if not self._calendar.is_working_day(day):
            return False
        if self._session_counts is not None:
            count = self._session_counts(day)
            if count is not None and count <= 0:
                return False
        return True

    def session_open_local(self, session: str) -> time:
        """A session opens when the previous session cuts off (AM at 00:00)."""
        self._require_session(session)
        index = PSO_SESSIONS.index(session)
        if index == 0:
            return time(0, 0)
        return self._schedule[PSO_SESSIONS[index - 1]]

    def session_cutoff_local(self, session: str) -> time:
        self._require_session(session)
        return self._schedule[session]

    def session_open_utc(self, day: date, session: str) -> datetime:
        local = datetime.combine(day, self.session_open_local(session), tzinfo=DHAKA_TZ)
        return local.astimezone(UTC)

    def session_cutoff_utc(self, day: date, session: str) -> datetime:
        local = datetime.combine(day, self.session_cutoff_local(session), tzinfo=DHAKA_TZ)
        return local.astimezone(UTC)

    def sessions_open_now(self, at: datetime) -> list[tuple[date, str]]:
        """The (day, session) pairs whose open instant has passed at ``at``
        and whose cutoff has not — i.e. windows that should be accumulating."""
        local = at.astimezone(DHAKA_TZ)
        day = local.date()
        if not self.is_session_day(day):
            return []
        out: list[tuple[date, str]] = []
        for session in PSO_SESSIONS:
            if self.session_open_local(session) <= local.time() < self._schedule[session]:
                out.append((day, session))
        return out

    def sessions_past_cutoff(self, at: datetime) -> list[tuple[date, str]]:
        """The (day, session) pairs whose cutoff instant has passed at ``at``."""
        local = at.astimezone(DHAKA_TZ)
        day = local.date()
        if not self.is_session_day(day):
            return []
        return [
            (day, session)
            for session in PSO_SESSIONS
            if local.time() >= self._schedule[session]
        ]

    def _require_session(self, session: str) -> None:
        if session not in self._schedule:
            raise ValueError(f"unknown PSO DNS session {session!r}; allowed: {PSO_SESSIONS}")


class WindowSweeper:
    """Deterministic open/cutoff/netting sweeps driven by the scheduler.

    Every sweep is idempotent: opening an already-open (day, session) window
    is a no-op, cutting an already-cut window is a no-op, and the netting
    sweep relies on the engine's content-addressed idempotency.
    """

    def __init__(
        self,
        *,
        calendar: PsoWindowCalendar,
        position_service: PositionService,
        position_store: NettingPositionStore,
        engine: NettingEngine,
        clock: Clock,
        settings: Settings,
    ) -> None:
        self._calendar = calendar
        self._positions = position_service
        self._store = position_store
        self._engine = engine
        self._clock = clock
        self._settings = settings

    def _require_pso(self) -> None:
        if self._settings.platform_mode != "PSO":
            raise PsoModeError(
                f"PSO-only operation refused: PLATFORM_MODE={self._settings.platform_mode!r}"
            )

    def open_sweep(self) -> list[str]:
        """Open + tick every due (day, session) window. Returns window ids."""
        self._require_pso()
        now = self._clock.now()
        opened: list[str] = []
        for day, session in self._calendar.sessions_open_now(now):
            existing = self._find_window(day, session)
            if existing is not None:
                if existing.status == "WINDOW_OPEN":
                    self._positions.window_tick(existing.window_id)
                    opened.append(existing.window_id)
                continue
            row = self._positions.open_window(day, session)
            self._positions.window_tick(row.window_id)
            opened.append(row.window_id)
        return opened

    def cutoff_sweep(self) -> list[str]:
        """Fire ``cutoff_reached`` for every accumulating window past cutoff.

        ``cutoff_at`` records the SESSION cutoff instant (deterministic from
        the schedule, not the sweep tick time) — it anchors the instruction
        release times.
        """
        self._require_pso()
        now = self._clock.now()
        cut: list[str] = []
        for day, session in self._calendar.sessions_past_cutoff(now):
            window = self._find_window(day, session)
            if window is None or window.status != "ACCUMULATING_POSITIONS":
                continue
            self._positions.transition_window(
                window.window_id,
                "cutoff_reached",
                "CUTOFF_REACHED",
                cutoff_at=self._calendar.session_cutoff_utc(day, session),
            )
            cut.append(window.window_id)
        return cut

    def netting_sweep(self) -> list[str]:
        """Run the netting engine over every CUTOFF_REACHED window.

        An integrity failure on one window never blocks the others; the
        engine has already recorded the FAILED_INTEGRITY run, audited it,
        and paged — the sweep moves on (the window itself stays refused).
        """
        self._require_pso()
        computed: list[str] = []
        now = self._clock.now()
        local_day = now.astimezone(DHAKA_TZ).date()
        for session in PSO_SESSIONS:
            window = self._find_window(local_day, session)
            if window is None or window.status != "CUTOFF_REACHED":
                continue
            try:
                run = self._engine.compute(window.window_id)
            except NettingIntegrityError:
                continue  # recorded + audited + paged by the engine; window stays put
            computed.append(run.netting_run_id)
        return computed

    def _find_window(self, day: date, session: str):
        window_id = make_id(
            "swin", {"window_date": day.isoformat(), "dns_session": session}
        )
        return self._store.get_window(window_id)


def build_pso_scheduler_tasks(
    *,
    settings: Settings,
    sweeper: WindowSweeper,
    cadence_seconds: float = 30.0,
) -> tuple[ScheduledTask, ...]:
    """PSO scheduler tasks — EMPTY under PSP (absence, not no-op)."""
    if settings.platform_mode != "PSO":
        return ()

    async def _open() -> object:
        return sweeper.open_sweep()

    async def _cutoff() -> object:
        return sweeper.cutoff_sweep()

    async def _net() -> object:
        return sweeper.netting_sweep()

    return (
        ScheduledTask(name="pso_window_open_sweep", fn=_open, cadence_seconds=cadence_seconds),
        ScheduledTask(name="pso_window_cutoff_sweep", fn=_cutoff, cadence_seconds=cadence_seconds),
        ScheduledTask(name="pso_netting_sweep", fn=_net, cadence_seconds=cadence_seconds),
    )

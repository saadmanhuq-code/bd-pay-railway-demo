"""Injectable clock. SystemClock is the ONLY wall-clock read in the codebase.

Spec 00 §7 (binding): no naive datetimes anywhere; all clock reads go through
the injectable clock so replay-critical paths never touch ``datetime.now``.
``scripts/check_bans.py`` enforces that this file is the single exemption.

Every other module takes a :class:`Clock` parameter and calls ``clock.now()``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable

__all__ = ["Clock", "FixedClock", "SteppingClock", "SystemClock"]


@runtime_checkable
class Clock(Protocol):
    """Time source contract: ``now()`` returns a timezone-aware UTC datetime."""

    def now(self) -> datetime:
        """Current time, timezone-aware, UTC."""
        ...


def _require_aware_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"clock start must be datetime, got {type(value).__name__}")
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError("clock requires a timezone-aware datetime (naive rejected)")
    return value.astimezone(UTC)


class SystemClock:
    """Production clock — the single permitted ``datetime.now`` call site."""

    def now(self) -> datetime:
        return datetime.now(UTC)


class FixedClock:
    """Test clock pinned to one instant; ``now()`` always returns it."""

    def __init__(self, start: datetime) -> None:
        self._now = _require_aware_utc(start)

    def now(self) -> datetime:
        return self._now


class SteppingClock:
    """Test clock that moves only when told.

    ``advance(seconds)`` moves time forward explicitly. If ``step_seconds`` is
    non-zero, every ``now()`` read also auto-advances by that amount *after*
    returning the current instant (useful for monotonic event sequences).
    """

    def __init__(self, start: datetime, *, step_seconds: int | float = 0) -> None:
        self._now = _require_aware_utc(start)
        if step_seconds < 0:
            raise ValueError("step_seconds must be >= 0")
        self._step = timedelta(seconds=step_seconds)

    def now(self) -> datetime:
        current = self._now
        self._now = self._now + self._step
        return current

    def advance(self, seconds: int | float) -> None:
        """Move the clock forward by ``seconds`` (must be >= 0)."""
        if seconds < 0:
            raise ValueError("cannot advance a clock backwards")
        self._now = self._now + timedelta(seconds=seconds)

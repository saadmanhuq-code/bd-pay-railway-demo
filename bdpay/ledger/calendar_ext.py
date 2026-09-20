"""Working-day arithmetic over the platform BangladeshBankCalendar (spec/04).

The platform calendar (``bdpay.platform.scheduler.BangladeshBankCalendar``)
provides ``is_working_day`` / ``next_working_day``; spec/04's settlement
algorithms additionally need ``add_working_days`` (the 5-working-day payout
mandate, delivery-verification holds) and ``business_days_between`` (the T+2
reconciliation timing-gap window). The platform module is owned by another
lane, so the extensions live here and take the calendar as a parameter.

Semantics (pinned by tests):

- ``add_working_days(cal, d, 0)`` — ``d`` itself when it is a working day,
  otherwise the next working day (spec/04 uses ``add_working_days(date, 0)``
  for the delivery-confirmation release day).
- ``add_working_days(cal, d, n)`` for n > 0 — advance n working days, never
  landing on a weekend/holiday.
- ``business_days_between(cal, a, b)`` — count of working days in the
  half-open interval (a, b]; 0 when b <= a.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

from bdpay.platform.scheduler import DHAKA_TZ, BangladeshBankCalendar

__all__ = [
    "add_working_days",
    "business_day_start_utc",
    "business_days_between",
]

#: Start of the BD business day used for release instants: 09:00 Dhaka time.
_BUSINESS_DAY_START_LOCAL = time(9, 0)


def add_working_days(cal: BangladeshBankCalendar, day: date, n: int) -> date:
    """The date n working days after ``day`` (n=0 snaps to the next working day)."""
    if not isinstance(day, date) or isinstance(day, datetime):
        raise ValueError(f"day must be a date, got {type(day).__name__}")
    if n < 0:
        raise ValueError(f"n must be >= 0, got {n}")
    current = day
    if n == 0:
        return current if cal.is_working_day(current) else cal.next_working_day(current)
    for _ in range(n):
        current = cal.next_working_day(current)
    return current


def business_days_between(cal: BangladeshBankCalendar, start: date, end: date) -> int:
    """Working days in the half-open interval (start, end]."""
    if end <= start:
        return 0
    count = 0
    current = start + timedelta(days=1)
    while current <= end:
        if cal.is_working_day(current):
            count += 1
        current += timedelta(days=1)
    return count


def business_day_start_utc(day: date) -> datetime:
    """09:00 BD local on ``day``, expressed in UTC (release instant, spec/04)."""
    return datetime.combine(day, _BUSINESS_DAY_START_LOCAL, tzinfo=DHAKA_TZ).astimezone(UTC)

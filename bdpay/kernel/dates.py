"""Calendar arithmetic helpers for review/refresh due dates (injected-clock).

Pure functions — no wall-clock reads; every caller passes datetimes produced
by the injected :class:`~bdpay.platform.clock.Clock`. Month/year addition is
calendar-safe (day clamped to the target month's length, so Jan 31 + 1 month
is Feb 28/29 — never an invalid date).
"""

from __future__ import annotations

from datetime import date, datetime

__all__ = ["add_months", "add_years", "age_years"]


def _days_in_month(year: int, month: int) -> int:
    if month == 2:
        leap = year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
        return 29 if leap else 28
    return 31 if month in (1, 3, 5, 7, 8, 10, 12) else 30


def add_months(when: datetime, months: int) -> datetime:
    """``when`` plus ``months`` calendar months, day clamped."""
    month_index = when.month - 1 + months
    year = when.year + month_index // 12
    month = month_index % 12 + 1
    day = min(when.day, _days_in_month(year, month))
    return when.replace(year=year, month=month, day=day)


def add_years(when: datetime, years: int) -> datetime:
    """``when`` plus ``years`` calendar years (Feb 29 clamps to Feb 28)."""
    return add_months(when, years * 12)


def age_years(dob: date, as_of: datetime) -> int:
    """Completed years between ``dob`` and ``as_of`` (date-of-birth semantics)."""
    today = as_of.date()
    years = today.year - dob.year
    if (today.month, today.day) < (dob.month, dob.day):
        years -= 1
    return years

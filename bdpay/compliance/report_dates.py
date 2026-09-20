"""Dhaka-timezone report-attribution rule (spec/16 LR-5 — binding query rule).

Every date-bounded report over ``TIMESTAMPTZ`` columns — merchant statements,
daily summaries, BB report extracts, dashboard date filters — MUST attribute a
row to the BANGLADESH calendar date of its timestamp:

- In Python: :func:`dhaka_report_date` (never ``value.date()`` on the UTC
  form).
- In SQL: the fragment from :func:`dhaka_date_sql` —
  ``((col AT TIME ZONE 'Asia/Dhaka')::date)`` — never ``col::date`` (UTC).
- Month-bounded ports use :func:`dhaka_month_bounds_utc` for the half-open
  UTC instant range covering a Dhaka-local calendar month.

``cycle_date`` (spec/04, already a BD-local ``DATE``) stays canonical for
settlement-batch attribution and is never re-derived from timestamps.

Rationale: the NPSB cutoff is 23:55 BD-local (spec/04), so the 23:55–24:00
band is inspection-visible — a ``2026-06-12T17:58:00Z`` transaction (= 23:58
Asia/Dhaka) belongs to BD-local 2026-06-12; ``2026-06-12T18:05:00Z`` (= 00:05
next day) belongs to 2026-06-13. Golden tests pin the boundary.

Asia/Dhaka is a fixed UTC+6 offset (no DST since 2009), so the fixed-offset
form is exact and avoids tzdata lookups in hot report paths.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone

from bdpay.platform.errors import InvalidRequestError

__all__ = [
    "DHAKA_TZ",
    "dhaka_date_sql",
    "dhaka_month_bounds_utc",
    "dhaka_report_date",
]

#: Asia/Dhaka — fixed UTC+6, no DST.
DHAKA_TZ = timezone(timedelta(hours=6), name="Asia/Dhaka")

#: The one binding SQL attribution form (column substituted by callers).
_SQL_TEMPLATE = "(({column} AT TIME ZONE 'Asia/Dhaka')::date)"


def dhaka_report_date(at: datetime) -> date:
    """BD-local calendar date of an aware timestamp (the attribution rule)."""
    if not isinstance(at, datetime):
        raise InvalidRequestError(
            "report attribution requires a datetime", code="naive_timestamp_rejected"
        )
    if at.tzinfo is None or at.tzinfo.utcoffset(at) is None:
        raise InvalidRequestError(
            "report attribution requires a timezone-aware timestamp",
            code="naive_timestamp_rejected",
        )
    return at.astimezone(DHAKA_TZ).date()


def dhaka_date_sql(column: str) -> str:
    """The binding SQL fragment attributing ``column`` to its Dhaka date.

    Refuses suspicious input — the column name is interpolated into SQL, so
    only plain (optionally qualified) identifiers are accepted.
    """
    if not column or not all(part.isidentifier() for part in column.split(".")):
        raise InvalidRequestError(f"not a plain column identifier: {column!r}")
    return _SQL_TEMPLATE.format(column=column)


def dhaka_month_bounds_utc(year: int, month: int) -> tuple[datetime, datetime]:
    """Half-open UTC instant range ``[start, end)`` of a Dhaka-local month.

    Month-bounded report ports (e.g. the monthly DFS summary reads) filter
    ``TIMESTAMPTZ`` columns with ``start <= col < end`` so every transaction
    lands in the month of its BD-local date.
    """
    if not 1 <= month <= 12:
        raise InvalidRequestError(f"month must be 1..12, got {month}")
    if year < 2020:
        raise InvalidRequestError(f"implausible reporting year {year}")
    start_local = datetime(year, month, 1, tzinfo=DHAKA_TZ)
    if month == 12:
        end_local = datetime(year + 1, 1, 1, tzinfo=DHAKA_TZ)
    else:
        end_local = datetime(year, month + 1, 1, tzinfo=DHAKA_TZ)
    return start_local.astimezone(UTC), end_local.astimezone(UTC)

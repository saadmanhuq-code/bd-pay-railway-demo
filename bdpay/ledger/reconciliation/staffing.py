"""Recon-exception staffing dimensioning + queue-vs-SLA panel data (spec/16 LR-5).

The spec/16 operational-quality note is binding on launch planning: before
merchant #1, finance staffing MUST be dimensioned against forecast exception
volume, and the ops console gains a queue-depth-vs-SLA panel. This module is
that note landed as CONFIG + COMPUTATION (not prose):

- :class:`ReconStaffingConfig` — the documented thresholds (forecast tx/day,
  per-rail exception-rate floors from simulator certification telemetry,
  reviewer throughput). The values are injectable config; the defaults are the
  launch-planning floors documented in ``docs/launch/recon-staffing.md``.
- :func:`required_reviewers` — the staffing calculation (ceil of forecast
  exceptions/day over per-reviewer throughput).
- :func:`queue_sla_report` — the spec/15 panel's data shape: open exceptions
  by tier, age vs ``due_by``, and the breach projection at current drain
  capacity. Pure function over exception rows + the injected clock instant.

All rates are expressed in basis points (integer per-10,000) — no floats
anywhere near the money path (spec/00 §6 posture).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, runtime_checkable

from bdpay.platform.errors import InvalidRequestError

__all__ = [
    "DEFAULT_EXCEPTION_RATE_FLOORS_BPS",
    "ReconStaffingConfig",
    "forecast_exceptions_per_day",
    "queue_sla_report",
    "required_reviewers",
]

#: Per-rail exception-rate FLOORS in basis points (exceptions per 10,000
#: transactions). Source: simulator certification telemetry is the floor per
#: spec/16; replace with measured rates as production telemetry accrues.
#: The three feeders named by spec/16: MFS pull recon, card fee true-up
#: (>25 bps delta), BEFTN two-session returns.
DEFAULT_EXCEPTION_RATE_FLOORS_BPS: dict[str, int] = {
    "mfs_pull_recon": 40,  # 0.40% of MFS pull transactions
    "card_fee_true_up": 15,  # 0.15% of card transactions (>25 bps delta)
    "beftn_returns": 25,  # 0.25% of BEFTN credits (two-session returns)
}

#: Open (workable) exception states — terminal rows leave the queue.
_OPEN_STATUSES = frozenset({"OPEN", "IN_REVIEW", "ESCALATED"})


@dataclass(frozen=True)
class ReconStaffingConfig:
    """The launch-planning staffing thresholds (documented config)."""

    projected_tx_per_day: int = 5_000
    exception_rate_floors_bps: dict[str, int] = field(
        default_factory=lambda: dict(DEFAULT_EXCEPTION_RATE_FLOORS_BPS)
    )
    exceptions_per_reviewer_per_day: int = 40
    minimum_reviewers: int = 1  # never staff zero before merchant #1

    def __post_init__(self) -> None:
        for name in (
            "projected_tx_per_day",
            "exceptions_per_reviewer_per_day",
            "minimum_reviewers",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise InvalidRequestError(f"{name} must be an int >= 1, got {value!r}")
        if not self.exception_rate_floors_bps:
            raise InvalidRequestError("exception_rate_floors_bps must be non-empty")
        for rail, bps in self.exception_rate_floors_bps.items():
            if isinstance(bps, bool) or not isinstance(bps, int) or not 0 <= bps <= 10_000:
                raise InvalidRequestError(
                    f"exception rate for {rail!r} must be int bps in 0..10000, got {bps!r}"
                )


def forecast_exceptions_per_day(config: ReconStaffingConfig) -> int:
    """Forecast daily exception volume (ceil; floors are conservative)."""
    total_bps = sum(config.exception_rate_floors_bps.values())
    numerator = config.projected_tx_per_day * total_bps
    return -(-numerator // 10_000)  # integer ceil


def required_reviewers(config: ReconStaffingConfig) -> int:
    """The staffing calculation the launch checklist must satisfy."""
    forecast = forecast_exceptions_per_day(config)
    reviewers = -(-forecast // config.exceptions_per_reviewer_per_day)  # ceil
    return max(config.minimum_reviewers, reviewers)


@runtime_checkable
class _ExceptionLike(Protocol):
    """The fields the panel reads from a reconciliation_exceptions row."""

    resolution_tier: str
    status: str
    raised_at: datetime
    due_by: datetime


def queue_sla_report(
    exceptions: list[_ExceptionLike] | tuple[_ExceptionLike, ...],
    *,
    now: datetime,
    config: ReconStaffingConfig,
    active_reviewers: int,
) -> dict:
    """Queue-depth-vs-SLA panel data (spec/15 panel addition, spec/16 LR-5).

    Per tier: open count, overdue count (past ``due_by``), oldest age in
    seconds. Plus the breach projection: with ``active_reviewers`` draining
    at the configured throughput, how many currently-open exceptions will
    still be open past their ``due_by`` if work proceeds oldest-first.
    """
    if isinstance(active_reviewers, bool) or not isinstance(active_reviewers, int):
        raise InvalidRequestError("active_reviewers must be an int")
    if active_reviewers < 0:
        raise InvalidRequestError("active_reviewers must be >= 0")
    if now.tzinfo is None or now.tzinfo.utcoffset(now) is None:
        raise InvalidRequestError("now must be timezone-aware")

    open_rows = [row for row in exceptions if row.status in _OPEN_STATUSES]
    by_tier: dict[str, dict[str, int]] = {}
    for row in open_rows:
        bucket = by_tier.setdefault(
            row.resolution_tier,
            {"open": 0, "overdue": 0, "oldest_age_seconds": 0},
        )
        bucket["open"] += 1
        if now > row.due_by:
            bucket["overdue"] += 1
        age = int((now - row.raised_at).total_seconds())
        bucket["oldest_age_seconds"] = max(bucket["oldest_age_seconds"], age)

    # Breach projection: oldest-first drain at the configured daily rate.
    drain_per_day = active_reviewers * config.exceptions_per_reviewer_per_day
    projected_breaches = 0
    if drain_per_day == 0:
        projected_breaches = len(open_rows)
    else:
        ordered = sorted(open_rows, key=lambda row: row.raised_at)
        for position, row in enumerate(ordered):
            # Whole days (ceil) needed before this row's turn completes.
            days_until_done = (position // drain_per_day) + 1
            remaining = (row.due_by - now).total_seconds()
            if remaining < days_until_done * 86_400:
                projected_breaches += 1

    return {
        "generated_at": now,
        "open_total": len(open_rows),
        "by_tier": by_tier,
        "active_reviewers": active_reviewers,
        "drain_capacity_per_day": drain_per_day,
        "projected_sla_breaches": projected_breaches,
        "staffing_floor_required_reviewers": required_reviewers(config),
    }

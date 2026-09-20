"""Postgres implementation of LimitDataPort (spec/02 pre-flight; spec/09 tier caps).

Reads:
  * ``kyc_records`` joined to ``customers`` for KYC tier and corporate flag.
  * ``payment_intents`` succeeded/in-flight rows for SIMPLIFIED monthly usage.
  * ``ledger.accounts`` materialized balances for wallet balance caps.
  * ``payment_intents`` succeeded/in-flight rows for 24-hour velocity.

This is read-only; it adds no migration.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from bdpay.kernel.preflight import KycTierInfo

__all__ = ["PostgresLimitDataPort"]


_IN_FLIGHT_STATUS_PREDICATE = (
    "pi.status IN ('PRE_FLIGHT', 'REQUIRES_ACTION', 'PROCESSING', 'REQUIRES_CAPTURE')"
)


class PostgresLimitDataPort:
    """LimitDataPort backed by real Postgres tables."""

    def __init__(self, connection_factory: Callable[[], Any]) -> None:
        self._connect = connection_factory

    def _fetchone(self, query: str, params: tuple) -> dict[str, Any] | None:
        from psycopg.rows import dict_row

        with self._connect() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(query, params)
                return cur.fetchone()

    def _fetchscalar(self, query: str, params: tuple) -> Any:
        with self._connect() as conn:
            row = conn.execute(query, params).fetchone()
            return None if row is None else row[0]

    def kyc_tier(self, customer_id: str) -> KycTierInfo | None:
        row = self._fetchone(
            """
            SELECT kr.tier, c.is_corporate
              FROM kyc_records kr
              JOIN customers c ON c.customer_id = kr.customer_id
             WHERE kr.customer_id = %s
               AND kr.state = 'ACTIVE'
               AND kr.tier IS NOT NULL
             ORDER BY kr.tier_assigned_at DESC NULLS LAST
             LIMIT 1
            """,
            (customer_id,),
        )
        if row is None:
            return None
        return KycTierInfo(tier=row["tier"], is_corporate=bool(row["is_corporate"]))

    def wallet_balance_minor(self, customer_id: str) -> int:
        value = self._fetchscalar(
            """
            SELECT COALESCE(SUM(a.balance_minor), 0)
              FROM ledger.accounts a
             WHERE a.owner_id = %s
               AND a.owner_type = 'CUSTOMER'
               AND a.account_type = 'LIABILITY'
               AND a.account_subtype = 'CUSTOMER_FLOAT'
               AND a.currency = 'BDT'
               AND a.closed_at IS NULL
            """,
            (customer_id,),
        )
        return int(value) if value is not None else 0

    def month_total_minor(self, customer_id: str, year_month: str) -> int:
        month_start, month_end = self._month_bounds(year_month)
        value = self._fetchscalar(
            f"""
            SELECT COALESCE(SUM(pi.amount_minor), 0)
              FROM payment_intents pi
             WHERE pi.customer_id = %s
               AND (
                    (
                        pi.succeeded_at IS NOT NULL
                        AND pi.succeeded_at >= %s
                        AND pi.succeeded_at < %s
                    )
                    OR (
                        {_IN_FLIGHT_STATUS_PREDICATE}
                        AND pi.created_at >= %s
                        AND pi.created_at < %s
                    )
               )
            """,
            (customer_id, month_start, month_end, month_start, month_end),
        )
        return int(value) if value is not None else 0

    def succeeded_count_24h(self, customer_id: str, method: str, now: datetime) -> int:
        window_start = now - timedelta(hours=24)
        value = self._fetchscalar(
            f"""
            SELECT COUNT(*)
              FROM payment_intents pi
             WHERE pi.customer_id = %s
               AND pi.method = %s::payment_method_type
               AND (
                    (
                        pi.succeeded_at IS NOT NULL
                        AND pi.succeeded_at > %s
                        AND pi.succeeded_at <= %s
                    )
                    OR (
                        {_IN_FLIGHT_STATUS_PREDICATE}
                        AND pi.created_at > %s
                        AND pi.created_at <= %s
                    )
               )
            """,
            (customer_id, method, window_start, now, window_start, now),
        )
        return int(value) if value is not None else 0

    def succeeded_amount_24h_minor(
        self, customer_id: str, method: str, now: datetime
    ) -> int:
        window_start = now - timedelta(hours=24)
        value = self._fetchscalar(
            f"""
            SELECT COALESCE(SUM(pi.amount_minor), 0)
              FROM payment_intents pi
             WHERE pi.customer_id = %s
               AND pi.method = %s::payment_method_type
               AND (
                    (
                        pi.succeeded_at IS NOT NULL
                        AND pi.succeeded_at > %s
                        AND pi.succeeded_at <= %s
                    )
                    OR (
                        {_IN_FLIGHT_STATUS_PREDICATE}
                        AND pi.created_at > %s
                        AND pi.created_at <= %s
                    )
               )
            """,
            (customer_id, method, window_start, now, window_start, now),
        )
        return int(value) if value is not None else 0

    @staticmethod
    def _month_bounds(year_month: str) -> tuple[datetime, datetime]:
        year_s, month_s = year_month.split("-", 1)
        year = int(year_s)
        month = int(month_s)
        month_start = datetime(year, month, 1, tzinfo=UTC)
        if month == 12:
            month_end = datetime(year + 1, 1, 1, tzinfo=UTC)
        else:
            month_end = datetime(year, month + 1, 1, tzinfo=UTC)
        return month_start, month_end

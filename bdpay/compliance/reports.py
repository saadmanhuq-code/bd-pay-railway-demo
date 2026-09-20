"""BB monthly DFS report builder (spec/06 regulatory mapping row:
"Monthly DFS reporting (TSA balances, e-money liabilities, trust funds) by
10th of following month" — BB Reporting Directive Jan 2026, research 03 §9.1).

The builder assembles data through a narrow read port (the ledger package
owns the actual balances; compliance never reads ledger tables directly) and
produces (a) a structured dict and (b) a rendered submission file. The
scheduler fires it on the 9th of each month; CAMLCO review precedes
submission (out of scope here — ops-console, spec/15).

All money is integer paisa; the rendered file shows BDT with two decimals
derived exactly (never floats).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from bdpay.compliance.report_dates import dhaka_month_bounds_utc
from bdpay.platform.canonical import sha256_canonical
from bdpay.platform.clock import Clock
from bdpay.platform.errors import InvalidRequestError
from bdpay.platform.interfaces import AuditEventSpec, AuditPort

__all__ = ["DfsReadPort", "MonthlyDfsReportBuilder", "StaticDfsReadPort"]

PRODUCER = "regulatory-reporting@1.0.0"


@runtime_checkable
class DfsReadPort(Protocol):
    """Read-only data the DFS summary needs, supplied by ledger/compliance.

    Every value is integer paisa or integer count for the (year, month)
    reporting period. Implementations live with their owning packages; the
    in-memory :class:`StaticDfsReadPort` serves unit tests.
    """

    def tsa_balance_minor(self, year: int, month: int) -> int: ...

    def emoney_liability_minor(self, year: int, month: int) -> int: ...

    def trust_fund_balance_minor(self, year: int, month: int) -> int: ...

    def transaction_count(self, year: int, month: int) -> int: ...

    def transaction_volume_minor(self, year: int, month: int) -> int: ...

    def active_wallet_count(self, year: int, month: int) -> int: ...

    def aml_alert_count(self, year: int, month: int) -> int: ...

    def str_filed_count(self, year: int, month: int) -> int: ...

    def ctr_filed_count(self, year: int, month: int) -> int: ...


class StaticDfsReadPort:
    """Deterministic DfsReadPort over a provided metric mapping (tests)."""

    def __init__(self, metrics: dict[str, int]) -> None:
        self._metrics = dict(metrics)

    def _get(self, key: str) -> int:
        value = self._metrics.get(key, 0)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise InvalidRequestError(f"metric {key} must be a non-negative int")
        return value

    def tsa_balance_minor(self, year: int, month: int) -> int:
        return self._get("tsa_balance_minor")

    def emoney_liability_minor(self, year: int, month: int) -> int:
        return self._get("emoney_liability_minor")

    def trust_fund_balance_minor(self, year: int, month: int) -> int:
        return self._get("trust_fund_balance_minor")

    def transaction_count(self, year: int, month: int) -> int:
        return self._get("transaction_count")

    def transaction_volume_minor(self, year: int, month: int) -> int:
        return self._get("transaction_volume_minor")

    def active_wallet_count(self, year: int, month: int) -> int:
        return self._get("active_wallet_count")

    def aml_alert_count(self, year: int, month: int) -> int:
        return self._get("aml_alert_count")

    def str_filed_count(self, year: int, month: int) -> int:
        return self._get("str_filed_count")

    def ctr_filed_count(self, year: int, month: int) -> int:
        return self._get("ctr_filed_count")


def _bdt(amount_minor: int) -> str:
    """Render integer paisa as an exact BDT decimal string (no floats)."""
    quant = (Decimal(amount_minor) / Decimal(100)).quantize(Decimal("0.01"))
    return f"BDT {quant}"


class MonthlyDfsReportBuilder:
    """Builds the monthly DFS summary: structured dict + rendered file."""

    def __init__(
        self,
        read_port: DfsReadPort,
        *,
        clock: Clock,
        audit: AuditPort,
        platform_mode: str = "PSP",
    ) -> None:
        self._port = read_port
        self._clock = clock
        self._audit = audit
        self._platform_mode = platform_mode

    def build(self, *, year: int, month: int, conn: Any | None = None) -> dict:
        """Assemble the structured monthly DFS summary for (year, month).

        The TSA-balance >= e-money-liability invariant (the 1:1 trust cover,
        spec/05) is asserted into the report as a compliance flag — a breach
        is surfaced, never hidden.
        """
        if not 1 <= month <= 12:
            raise InvalidRequestError(f"month must be 1..12, got {month}")
        if year < 2020:
            raise InvalidRequestError(f"implausible reporting year {year}")
        # spec/16 LR-5 Dhaka-attribution rule: the reporting period is the
        # BD-local calendar month; ports MUST bound their TIMESTAMPTZ reads
        # by these UTC instants (never the UTC month). The bounds ride the
        # report so an inspector can re-run the extraction exactly.
        period_start_utc, period_end_utc = dhaka_month_bounds_utc(year, month)
        tsa = self._port.tsa_balance_minor(year, month)
        emoney = self._port.emoney_liability_minor(year, month)
        trust = self._port.trust_fund_balance_minor(year, month)
        report = {
            "report_type": "BB_DFS_MONTHLY",
            "platform_mode": self._platform_mode,
            "reporting_period": f"{year:04d}-{month:02d}",
            "reporting_period_timezone": "Asia/Dhaka",
            "period_start_utc": period_start_utc,
            "period_end_utc": period_end_utc,
            "generated_at": self._clock.now(),
            "currency": "BDT",
            "balances": {
                "tsa_balance_minor": tsa,
                "emoney_liability_minor": emoney,
                "trust_fund_balance_minor": trust,
                "tsa_cover_ok": tsa >= emoney,
                "tsa_cover_gap_minor": max(emoney - tsa, 0),
            },
            "activity": {
                "transaction_count": self._port.transaction_count(year, month),
                "transaction_volume_minor": self._port.transaction_volume_minor(year, month),
                "active_wallet_count": self._port.active_wallet_count(year, month),
            },
            "compliance": {
                "aml_alert_count": self._port.aml_alert_count(year, month),
                "str_filed_count": self._port.str_filed_count(year, month),
                "ctr_filed_count": self._port.ctr_filed_count(year, month),
            },
        }
        report["report_hash"] = sha256_canonical(report)
        self._audit.append(
            AuditEventSpec(
                event_type="BB_DFS_REPORT_GENERATED",
                actor_id=PRODUCER,
                subject_type="DfsReport",
                subject_id=f"dfs_{year:04d}_{month:02d}",
                payload={
                    "reporting_period": report["reporting_period"],
                    "report_hash": report["report_hash"],
                },
            ),
            clock=self._clock,
            conn=conn,
        )
        return report

    def render(self, report: dict, target_path: Path) -> Path:
        """Render the structured report to a human-reviewable submission file."""
        balances = report["balances"]
        activity = report["activity"]
        compliance = report["compliance"]
        generated_at = report["generated_at"]
        generated_str = (
            generated_at.isoformat() if isinstance(generated_at, datetime) else str(generated_at)
        )
        lines = [
            "BANGLADESH BANK - MONTHLY DFS SUMMARY REPORT",
            f"Reporting period : {report['reporting_period']}",
            f"Platform mode    : {report['platform_mode']}",
            f"Generated at     : {generated_str}",
            f"Report hash      : {report['report_hash']}",
            "",
            "SECTION 1 - TRUST / SETTLEMENT BALANCES",
            f"  TSA balance              : {_bdt(balances['tsa_balance_minor'])}",
            f"  E-money liability        : {_bdt(balances['emoney_liability_minor'])}",
            f"  Trust fund balance       : {_bdt(balances['trust_fund_balance_minor'])}",
            f"  1:1 trust cover intact   : {'YES' if balances['tsa_cover_ok'] else 'NO'}",
            f"  Cover gap                : {_bdt(balances['tsa_cover_gap_minor'])}",
            "",
            "SECTION 2 - TRANSACTION ACTIVITY",
            f"  Transactions             : {activity['transaction_count']}",
            f"  Volume                   : {_bdt(activity['transaction_volume_minor'])}",
            f"  Active wallets           : {activity['active_wallet_count']}",
            "",
            "SECTION 3 - AML/CFT ACTIVITY",
            f"  AML alerts raised        : {compliance['aml_alert_count']}",
            f"  STRs filed               : {compliance['str_filed_count']}",
            f"  CTRs filed               : {compliance['ctr_filed_count']}",
            "",
            "Prepared by regulatory-reporting; CAMLCO review required before",
            "submission (due by the 10th of the following month).",
        ]
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return target_path

"""CTR cash aggregation per account per Dhaka day (spec/06 CtrAggregation).

Cash classification rule (spec/06): only ``CASH_AGENT`` and ``CASH_ATM``
channels count toward the BDT 10 lakh threshold; ``DIGITAL_TRANSFER``,
``MERCHANT_PAYMENT``, and ``SALARY_DISBURSEMENT`` are excluded (research 03
§7.1 carve-out). DEBIT = cash-out, CREDIT = cash-in.

Day boundary: the aggregation date is the BANGLADESH calendar date (UTC+6,
Asia/Dhaka has no DST) of the event's ``occurred_at``, NOT the UTC date —
a 17:59Z movement belongs to that Dhaka day, an 18:00Z movement to the next.

Idempotency: at-least-once event delivery dedupes on ``event_id``; the
``(account_id, aggregation_date)`` uniqueness makes the row content
re-derivable without double counting (mirrors the ctr_aggregations UNIQUE
constraint, migration 0073).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime
from typing import Any, Protocol, runtime_checkable

from bdpay.compliance.goaml import build_ctr_payload, validate_ctr_payload
from bdpay.compliance.report_dates import DHAKA_TZ, dhaka_report_date
from bdpay.platform.bangla import normalize_bengali_digits
from bdpay.platform.clock import Clock
from bdpay.platform.errors import ConflictError, InvalidRequestError
from bdpay.platform.ids import make_id
from bdpay.platform.interfaces import AuditEventSpec, AuditPort, OutboxPort
from bdpay.platform.outbox import build_event

__all__ = [
    "CASH_CHANNELS",
    "DHAKA_TZ",
    "CtrAggregation",
    "CtrBatch",
    "CtrStore",
    "InMemoryCtrStore",
    "dhaka_date",
]

PRODUCER = "regulatory-reporting@1.0.0"

#: Channels that count toward CTR cash aggregation (spec/06 classification).
CASH_CHANNELS = ("CASH_AGENT", "CASH_ATM")

_NON_CASH_CHANNELS = ("DIGITAL_TRANSFER", "MERCHANT_PAYMENT", "SALARY_DISBURSEMENT")


def dhaka_date(at: datetime) -> date:
    """The Bangladesh calendar date of an aware timestamp (spec day boundary).

    Delegates to the spec/16 LR-5 canonical attribution rule
    (:mod:`bdpay.compliance.report_dates`) — one implementation, every report
    path.
    """
    return dhaka_report_date(at)


@dataclass(frozen=True)
class CtrAggregation:
    """One ctr_aggregations row (migration 0073)."""

    ctr_id: str
    account_id: str
    aggregation_date: date
    total_cash_in_minor: int
    total_cash_out_minor: int
    threshold_minor: int
    contributing_txn_count: int
    computed_at: datetime
    currency: str = "BDT"
    ctr_filed: bool = False
    ctr_filed_at: datetime | None = None
    goaml_ref: str | None = None
    annual_batch_year: int | None = None
    schema_version: int = 1

    @property
    def threshold_crossed(self) -> bool:
        """Generated column semantics: total cash movement >= threshold."""
        return (self.total_cash_in_minor + self.total_cash_out_minor) >= self.threshold_minor


@runtime_checkable
class CtrStore(Protocol):
    """Storage contract for ctr_aggregations."""

    def upsert(self, row: CtrAggregation) -> None: ...

    def get(self, account_id: str, aggregation_date: date) -> CtrAggregation | None: ...

    def get_by_id(self, ctr_id: str) -> CtrAggregation | None: ...

    def crossed_unfiled(self) -> list[CtrAggregation]: ...

    def filed_in_year(self, year: int) -> list[CtrAggregation]: ...


class InMemoryCtrStore:
    """Deterministic in-memory CTR store for unit tests."""

    def __init__(self) -> None:
        self._rows: dict[tuple[str, date], CtrAggregation] = {}

    def upsert(self, row: CtrAggregation) -> None:
        self._rows[(row.account_id, row.aggregation_date)] = row

    def get(self, account_id: str, aggregation_date: date) -> CtrAggregation | None:
        return self._rows.get((account_id, aggregation_date))

    def get_by_id(self, ctr_id: str) -> CtrAggregation | None:
        for row in self._rows.values():
            if row.ctr_id == ctr_id:
                return row
        return None

    def crossed_unfiled(self) -> list[CtrAggregation]:
        return sorted(
            (r for r in self._rows.values() if r.threshold_crossed and not r.ctr_filed),
            key=lambda r: (r.aggregation_date, r.account_id),
        )

    def filed_in_year(self, year: int) -> list[CtrAggregation]:
        return sorted(
            (
                r
                for r in self._rows.values()
                if r.ctr_filed and r.aggregation_date.year == year
            ),
            key=lambda r: (r.aggregation_date, r.account_id),
        )


@dataclass
class _DayBucket:
    cash_in_minor: int = 0
    cash_out_minor: int = 0
    txn_count: int = 0
    event_ids: set[str] = field(default_factory=set)


class CtrBatch:
    """The daily cash-aggregation pipeline + filing payload builder."""

    def __init__(
        self,
        store: CtrStore,
        *,
        clock: Clock,
        audit: AuditPort,
        outbox: OutboxPort,
        threshold_minor: int = 100_000_000,
    ) -> None:
        if threshold_minor <= 0:
            raise InvalidRequestError(
                "CTR threshold must be positive paisa", code="invalid_threshold"
            )
        self._store = store
        self._clock = clock
        self._audit = audit
        self._outbox = outbox
        self._threshold_minor = threshold_minor
        self._buckets: dict[tuple[str, date], _DayBucket] = {}

    # -- intake ------------------------------------------------------------------

    @staticmethod
    def _parse_amount_minor(value: object) -> int:
        """Accept int paisa; normalize Bengali-numeral strings (spec fixture)."""
        if isinstance(value, bool):
            raise InvalidRequestError("amount_minor must be int paisa", code="invalid_amount")
        if isinstance(value, int):
            return value
        if isinstance(value, str):
            cleaned = normalize_bengali_digits(value).replace(",", "").strip()
            if cleaned.isdigit():
                return int(cleaned)
        raise InvalidRequestError(
            "amount_minor must be integer paisa (Bengali numerals normalized first)",
            code="invalid_amount",
        )

    def intake_cash_event(
        self,
        *,
        event_id: str,
        account_id: str,
        direction: str,  # CREDIT = cash-in | DEBIT = cash-out
        amount_minor: object,
        channel: str,
        occurred_at: datetime,
    ) -> bool:
        """Record one cash movement; returns False for duplicates/non-cash.

        Idempotent on ``event_id`` (at-least-once delivery rule, spec/00 §5).
        Non-cash channels are excluded from aggregation by the spec carve-out.
        """
        if channel not in CASH_CHANNELS:
            if channel not in _NON_CASH_CHANNELS:
                raise InvalidRequestError(
                    f"unknown payment channel {channel!r}", code="unknown_channel"
                )
            return False
        if direction not in ("CREDIT", "DEBIT"):
            raise InvalidRequestError(
                "direction must be CREDIT (cash-in) or DEBIT (cash-out)",
                code="invalid_direction",
            )
        amount = self._parse_amount_minor(amount_minor)
        if amount <= 0:
            raise InvalidRequestError("amount_minor must be > 0", code="invalid_amount")
        key = (account_id, dhaka_date(occurred_at))
        bucket = self._buckets.setdefault(key, _DayBucket())
        if event_id in bucket.event_ids:
            return False  # duplicate delivery — no double count
        bucket.event_ids.add(event_id)
        if direction == "CREDIT":
            bucket.cash_in_minor += amount
        else:
            bucket.cash_out_minor += amount
        bucket.txn_count += 1
        return True

    # -- the 23:30 BST daily pipeline ------------------------------------------------

    def run_daily_aggregation(
        self, aggregation_date: date, *, conn: Any | None = None
    ) -> list[CtrAggregation]:
        """Create/update ctr_aggregations rows for one Dhaka calendar date.

        Steps 2-6 of the spec/06 pipeline: sum per account by direction,
        upsert the row, emit ``ctr_aggregation.threshold_crossed`` and queue a
        filing (``ctr_aggregation.filing_pending``) for crossed, unfiled rows.
        """
        now = self._clock.now()
        results: list[CtrAggregation] = []
        for (account_id, day), bucket in sorted(self._buckets.items()):
            if day != aggregation_date:
                continue
            ctr_id = make_id("ctr", {"account_id": account_id, "aggregation_date": str(day)})
            existing = self._store.get(account_id, day)
            row = CtrAggregation(
                ctr_id=ctr_id,
                account_id=account_id,
                aggregation_date=day,
                total_cash_in_minor=bucket.cash_in_minor,
                total_cash_out_minor=bucket.cash_out_minor,
                threshold_minor=self._threshold_minor,
                contributing_txn_count=bucket.txn_count,
                computed_at=now,
                ctr_filed=existing.ctr_filed if existing else False,
                ctr_filed_at=existing.ctr_filed_at if existing else None,
                goaml_ref=existing.goaml_ref if existing else None,
                annual_batch_year=existing.annual_batch_year if existing else None,
            )
            self._store.upsert(row)
            results.append(row)
            if row.threshold_crossed and not row.ctr_filed:
                total = row.total_cash_in_minor + row.total_cash_out_minor
                self._outbox.enqueue(
                    build_event(
                        event_type="ctr_aggregation.threshold_crossed",
                        subject_type="CtrAggregation",
                        subject_id=ctr_id,
                        producer=PRODUCER,
                        topic="aml.events",
                        payload={
                            "ctr_id": ctr_id,
                            "account_id": account_id,
                            "aggregation_date": str(day),
                            "total_minor": total,
                        },
                        occurred_at=now,
                    ),
                    conn=conn,
                )
                self._outbox.enqueue(
                    build_event(
                        event_type="ctr_aggregation.filing_pending",
                        subject_type="CtrAggregation",
                        subject_id=ctr_id,
                        producer=PRODUCER,
                        topic="aml.events",
                        payload={
                            "ctr_id": ctr_id,
                            "account_id": account_id,
                            "aggregation_date": str(day),
                            "total_minor": total,
                        },
                        occurred_at=now,
                    ),
                    conn=conn,
                )
                self._audit.append(
                    AuditEventSpec(
                        event_type="CTR_THRESHOLD_CROSSED",
                        actor_id=PRODUCER,
                        subject_type="CtrAggregation",
                        subject_id=ctr_id,
                        payload={"account_id": account_id, "total_minor": total},
                    ),
                    clock=self._clock,
                    conn=conn,
                )
        return results

    # -- filing -----------------------------------------------------------------

    def build_filing_payload(self, ctr_id: str) -> dict:
        """The validated goAML CTR payload for one crossed aggregation."""
        row = self._store.get_by_id(ctr_id)
        if row is None:
            raise InvalidRequestError(f"unknown CTR aggregation {ctr_id}", code="ctr_not_found")
        if not row.threshold_crossed:
            raise ConflictError(
                "CTR filing applies only to threshold-crossed aggregations",
                code="ctr_not_crossed",
            )
        payload = build_ctr_payload(
            ctr_id=row.ctr_id,
            account_id=row.account_id,
            aggregation_date=row.aggregation_date,
            total_cash_in_minor=row.total_cash_in_minor,
            total_cash_out_minor=row.total_cash_out_minor,
            contributing_txn_count=row.contributing_txn_count,
        )
        validate_ctr_payload(payload)
        return payload

    async def file_pending(
        self, *, connector: Any, conn: Any | None = None
    ) -> list[CtrAggregation]:
        """File every crossed, unfiled aggregation via ``file_ctr`` (never drop).

        A connector failure leaves the row unfiled (it stays in the
        crossed-unfiled queue; the goAML portal being down never loses a CTR —
        spec/06 cut-last #3: accumulate locally, file when available).
        """
        filed: list[CtrAggregation] = []
        for row in self._store.crossed_unfiled():
            payload = self.build_filing_payload(row.ctr_id)
            try:
                goaml_ref = await connector.file_ctr(payload)
            except Exception:  # noqa: BLE001 - stays queued; retried next run
                continue
            now = self._clock.now()
            # ctr_filed must mean "submitted to the goAML portal", not merely
            # "enqueued". file_ctr returns a durable ref immediately at enqueue
            # (filing state QUEUED); the never-drop submit sweep advances it to
            # SUBMITTED once the portal accepts. Marking ctr_filed=True on a bare
            # enqueue conflates "durably queued" with "filed to BFIU". Gate on the
            # connector's real filing state; a still-QUEUED row keeps its durable
            # handoff ref but stays crossed-unfiled so the next run retries
            # (file_ctr is idempotent per report_ref) — the filing is never lost.
            report_ref = str(payload.get("report_ref", ""))
            latest_for = getattr(connector, "latest_for", None)
            filing = latest_for(report_ref) if (latest_for is not None and report_ref) else None
            submitted = filing is not None and getattr(filing, "state", "QUEUED") in {
                "SUBMITTED",
                "ACK_PENDING",
                "ACKED",
            }
            if not submitted:
                self._store.upsert(replace(row, goaml_ref=goaml_ref))
                continue
            updated = replace(row, ctr_filed=True, ctr_filed_at=now, goaml_ref=goaml_ref)
            self._store.upsert(updated)
            self._audit.append(
                AuditEventSpec(
                    event_type="CTR_FILED",
                    actor_id=PRODUCER,
                    subject_type="CtrAggregation",
                    subject_id=row.ctr_id,
                    payload={"goaml_ref": goaml_ref},
                ),
                clock=self._clock,
                conn=conn,
            )
            filed.append(updated)
        return filed

    def build_annual_batch(self, year: int) -> dict:
        """The annual BFIU batch (31 March submission, spec/06 step 7).

        Aggregates the year's filed CTR records and stamps
        ``annual_batch_year`` on each included row.
        """
        rows = self._store.filed_in_year(year)
        for row in rows:
            self._store.upsert(replace(row, annual_batch_year=year))
        return {
            "report_type": "CTR_ANNUAL_BATCH",
            "filing_institution": "BD-PAY",
            "batch_year": year,
            "record_count": len(rows),
            "total_cash_in_minor": sum(r.total_cash_in_minor for r in rows),
            "total_cash_out_minor": sum(r.total_cash_out_minor for r in rows),
            "records": [
                {
                    "ctr_id": r.ctr_id,
                    "account_ref": r.account_id,
                    "aggregation_date": r.aggregation_date.isoformat(),
                    "total_cash_in_minor": r.total_cash_in_minor,
                    "total_cash_out_minor": r.total_cash_out_minor,
                    "goaml_ref": r.goaml_ref,
                }
                for r in rows
            ],
        }

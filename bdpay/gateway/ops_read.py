"""Read models for the operator console's live dashboard and ledger endpoints."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from math import ceil
from threading import Lock
from typing import Any

from bdpay.gateway.pagination import build_page
from bdpay.ledger.payloads import entry_id_from_pointer
from bdpay.ledger.service import MONEY_DOMAIN
from bdpay.ledger.types import VERIFY_COMPLETED
from bdpay.platform.clock import SystemClock
from bdpay.platform.errors import InvalidRequestError


class GatewaySlaRecorder:
    """In-process route-group latency samples for the ops SLA dashboard."""

    def __init__(self, *, max_samples_per_group: int = 512) -> None:
        self._max_samples = max_samples_per_group
        self._samples: dict[str, list[int]] = {}
        self._lock = Lock()

    def record(self, route_group: str | None, duration_ms: int) -> None:
        if not route_group:
            return
        if duration_ms < 0:
            duration_ms = 0
        with self._lock:
            samples = self._samples.setdefault(route_group, [])
            samples.append(duration_ms)
            if len(samples) > self._max_samples:
                del samples[: len(samples) - self._max_samples]

    def snapshot(self) -> list[dict[str, int | str]]:
        with self._lock:
            copied = {group: tuple(samples) for group, samples in self._samples.items()}
        rows: list[dict[str, int | str]] = []
        for group, samples in sorted(copied.items()):
            if not samples:
                continue
            rows.append(
                {
                    "group": group,
                    "p50_ms": _percentile(samples, 50),
                    "p95_ms": _percentile(samples, 95),
                    "p99_ms": _percentile(samples, 99),
                }
            )
        return rows


def ledger_journal_entries(deps: Any, *, limit: int, cursor: str | None) -> dict:
    ledger = _ledger_service(deps)
    if ledger is None:
        return build_page([], None)
    offset = _cursor_offset(cursor)
    list_entries = getattr(ledger, "list_journal_entries", None)
    if callable(list_entries):
        rows = list(list_entries(limit=limit + 1, offset=offset))
    else:
        rows = list(ledger.store.list_journal_entries(limit=limit + 1, offset=offset))
    page = [_journal_entry_view(ledger, row) for row in rows[:limit]]
    next_cursor = str(offset + limit) if len(rows) > limit else None
    return build_page(page, next_cursor)


def ledger_chain_entries(deps: Any, *, limit: int, cursor: str | None) -> dict:
    ledger = _ledger_service(deps)
    if ledger is None:
        return build_page([], None)
    offset = _cursor_offset(cursor)
    store = ledger.store
    rows = [
        row
        for row in store.iter_chain(MONEY_DOMAIN, offset + 1, offset + limit + 1)
        if _attr(row, "entry_type") != "genesis"
    ]
    page = [_chain_entry_view(row) for row in rows[:limit]]
    next_cursor = str(offset + limit) if len(rows) > limit else None
    return build_page(page, next_cursor)


def ledger_chain_verify_status(deps: Any) -> dict[str, object]:
    now = _now(deps)
    ledger = _ledger_service(deps)
    if ledger is None:
        return {
            "status": "VERIFIED",
            "last_verified_at": _rfc3339(now),
            "entries_checked": 0,
            "first_bad_seq": None,
        }
    verify = getattr(ledger, "verify_chain_status", None)
    result = verify(chain_domain=MONEY_DOMAIN) if callable(verify) else ledger.verify_chain()
    ok = _attr(result, "status") == VERIFY_COMPLETED or bool(_attr(result, "ok", False))
    return {
        "status": "VERIFIED" if ok else "FAILED",
        "last_verified_at": _rfc3339(now),
        "entries_checked": int(_attr(result, "entries_checked", 0) or 0),
        "first_bad_seq": _attr(result, "first_broken_index"),
    }


def tcsa_dashboard(deps: Any) -> dict[str, object]:
    now = _now(deps)
    store = getattr(deps, "tcsa_store", None)
    latest = _call_optional(store, "latest_snapshot")
    trend_rows = _call_optional(store, "list_snapshots") or []
    if latest is not None:
        required = int(_attr(latest, "total_outstanding_liability_minor", 0) or 0)
        available = int(_attr(latest, "tcsa_balance_minor", 0) or 0)
        coverage = _attr(latest, "coverage_ratio_bps")
        coverage_bps = int(coverage if coverage is not None else 10_000)
        shortfall = int(_attr(latest, "shortfall_minor", max(0, required - available)) or 0)
        as_of = _rfc3339(_attr(latest, "snapshotted_at", now))
        trend = [
            {
                "at": _rfc3339(_attr(row, "snapshotted_at", now)),
                "coverage_bps": int(_attr(row, "coverage_ratio_bps", 10_000) or 10_000),
            }
            for row in trend_rows[-12:]
        ]
    else:
        required, available, coverage_bps, shortfall = _tcsa_from_ledger(deps)
        as_of = _rfc3339(now)
        trend = [{"at": as_of, "coverage_bps": coverage_bps}]
    return {
        "as_of": as_of,
        "required_minor": str(required),
        "available_minor": str(available),
        "coverage_bps": coverage_bps,
        "shortfall_minor": str(shortfall),
        "trend": trend,
        "open_compensation_over_attestation_threshold": 0,
    }


def settlement_dashboard(deps: Any) -> dict[str, object]:
    now = _now(deps)
    store = getattr(deps, "settlement_store", None)
    instructions = _call_optional(store, "list_instructions") or []
    batches = _call_optional(store, "list_batches") or []
    grouped: dict[str, dict[str, int | str]] = defaultdict(
        lambda: {"state": "", "count": 0, "amount_minor": "0"}
    )
    active_release_times: list[datetime] = []
    deadline_breaches = 0
    for row in instructions:
        state = str(_attr(row, "status", "UNKNOWN"))
        bucket = grouped[state]
        bucket["state"] = state
        bucket["count"] = int(bucket["count"]) + 1
        bucket["amount_minor"] = str(
            int(bucket["amount_minor"]) + int(_attr(row, "amount_minor", 0) or 0)
        )
        if state not in {"SETTLED", "RETURNED", "CANCELLED", "FAILED"}:
            earliest = _attr(row, "earliest_release_at")
            latest = _attr(row, "latest_release_at")
            if isinstance(earliest, datetime):
                active_release_times.append(earliest)
            if isinstance(latest, datetime):
                active_release_times.append(latest)
                if latest < now:
                    deadline_breaches += 1
    open_batches = sum(
        1 for row in batches if str(_attr(row, "status", "")) not in {"CONFIRMED", "CANCELLED"}
    )
    return {
        "as_of": _rfc3339(now),
        "instructions_by_state": sorted(grouped.values(), key=lambda row: str(row["state"])),
        "open_batches": open_batches,
        "earliest_release_at": _rfc3339(min(active_release_times)) if active_release_times else "",
        "latest_release_at": _rfc3339(max(active_release_times)) if active_release_times else "",
        "working_day_deadline_breaches": deadline_breaches,
    }


def connectors_dashboard(deps: Any) -> dict[str, object]:
    now = _now(deps)
    registry = getattr(deps, "connector_registry", None)
    list_all = getattr(registry, "list_all", None)
    registrations = list(list_all()) if callable(list_all) else []
    connectors = []
    for row in registrations:
        connectors.append(
            {
                "connector_id": str(_attr(row, "connector_id", "")),
                "display_name": str(_attr(row, "display_name", _attr(row, "connector_id", ""))),
                "active_mode": _enum_text(_attr(row, "active_mode", "DISABLED")),
                "health_status": str(_attr(row, "health_status", "UNKNOWN")),
                "circuit_state": "CLOSED",
                "last_health_at": _rfc3339(
                    _attr(row, "last_health_at") or _attr(row, "updated_at", now)
                ),
            }
        )
    return {"as_of": _rfc3339(now), "connectors": connectors}


def aml_dashboard(deps: Any) -> dict[str, object]:
    now = _now(deps)
    alerts = _alerts_for_statuses(
        getattr(deps, "aml_alert_store", None), ("RAISED", "TRIAGING", "IN_CASE")
    )
    str_reports = _str_reports_for_statuses(
        getattr(deps, "str_store", None),
        ("PENDING_CAMLCO_REVIEW", "CAMLCO_APPROVED", "FILING_PENDING", "FILING_FAILED"),
    )
    goaml_rows = _goaml_rows(getattr(deps, "goaml_connector", None))
    ctr_unfiled = _call_optional(getattr(deps, "ctr_store", None), "crossed_unfiled") or []
    sanctions_age, sanctions_stale = _sanctions_feed_age(deps, now)
    return {
        "as_of": _rfc3339(now),
        "alert_queue_depth": len(alerts),
        "alert_age_histogram": _alert_histogram(alerts, now),
        "str_queue": [
            {
                "str_id": str(_attr(row, "str_id", "")),
                "subject_id": str(_attr(row, "subject_id", "")),
                "state": str(_attr(row, "status", "")),
                "created_at": _rfc3339(_attr(row, "created_at", now)),
            }
            for row in str_reports[:20]
        ],
        "goaml_filings": [
            {
                "filing_id": str(_attr(row, "filing_id", "")),
                "state": str(_attr(row, "state", "")),
                "submitted_at": _optional_ts(_attr(row, "submitted_at")),
            }
            for row in goaml_rows[:20]
        ],
        "sanctions_feed_age_seconds": sanctions_age,
        "sanctions_feed_stale": sanctions_stale,
        "ctr_queue_depth": len(ctr_unfiled),
    }


def sla_dashboard(deps: Any) -> dict[str, object]:
    recorder = getattr(deps, "sla_recorder", None)
    snapshot = recorder.snapshot() if callable(getattr(recorder, "snapshot", None)) else []
    return {"as_of": _rfc3339(_now(deps)), "route_groups": snapshot}


def _ledger_service(deps: Any) -> Any | None:
    ledger = getattr(deps, "ledger", None)
    if ledger is not None:
        return ledger
    orchestrator = getattr(getattr(deps, "payment_intents", None), "_orch", None)
    ledger_port = getattr(orchestrator, "_ledger", None)
    return getattr(ledger_port, "_svc", None)


def _journal_entry_view(ledger: Any, row: Any) -> dict[str, object]:
    entry_id = str(_attr(row, "entry_id", ""))
    postings = []
    get_postings = getattr(ledger, "get_postings_for_entry", None)
    if callable(get_postings):
        postings = list(get_postings(entry_id))
    return {
        "journal_id": entry_id,
        "description": str(_attr(row, "description", "")),
        "subject_type": str(_attr(row, "reference_type", "")),
        "subject_id": str(_attr(row, "reference_id", "")),
        "posted_at": _rfc3339(_attr(row, "produced_at")),
        "postings": [
            {
                "posting_id": str(_attr(posting, "posting_id", "")),
                "account": str(_attr(posting, "account_id", "")),
                "side": str(_attr(posting, "side", "")),
                "amount_minor": str(_attr(posting, "amount_minor", 0)),
                "currency": str(_attr(posting, "currency", "BDT")),
            }
            for posting in postings
        ],
    }


def _chain_entry_view(row: Any) -> dict[str, object]:
    pointer = str(_attr(row, "payload_pointer", ""))
    journal_id = entry_id_from_pointer(pointer) or pointer
    return {
        "chain_seq": int(_attr(row, "chain_index", 0) or 0),
        "journal_id": journal_id,
        "entry_hash": str(_attr(row, "chain_hash", "")),
        "prev_hash": str(_attr(row, "prev_chain_hash", "")),
        "chained_at": _rfc3339(_attr(row, "produced_at")),
    }


def _tcsa_from_ledger(deps: Any) -> tuple[int, int, int, int]:
    ledger = _ledger_service(deps)
    if ledger is None:
        return 0, 0, 10_000, 0
    store = ledger.store
    available = int(store.sum_balance_by_subtype("SPONSOR_BANK_TCSA"))
    required = int(store.sum_balance_by_subtype("MERCHANT_SETTLEMENT")) + int(
        store.sum_balance_by_subtype("CUSTOMER_FLOAT")
    )
    coverage_bps = (available * 10_000) // required if required > 0 else 10_000
    shortfall = max(0, required - available)
    return required, available, coverage_bps, shortfall


def _alerts_for_statuses(store: Any, statuses: tuple[str, ...]) -> list[Any]:
    rows: list[Any] = []
    by_status = getattr(store, "by_status", None)
    if not callable(by_status):
        return rows
    for status in statuses:
        rows.extend(by_status(status))
    return sorted(rows, key=lambda row: _attr(row, "raised_at", datetime.min.replace(tzinfo=UTC)))


def _str_reports_for_statuses(store: Any, statuses: tuple[str, ...]) -> list[Any]:
    rows: list[Any] = []
    by_status = getattr(store, "by_status", None)
    if not callable(by_status):
        return rows
    for status in statuses:
        rows.extend(by_status(status))
    return sorted(rows, key=lambda row: _attr(row, "created_at", datetime.min.replace(tzinfo=UTC)))


def _goaml_rows(connector: Any) -> list[Any]:
    filings = getattr(connector, "_filings", None)
    rows = getattr(filings, "rows", None)
    return list(rows()) if callable(rows) else []


def _sanctions_feed_age(deps: Any, now: datetime) -> tuple[int, bool]:
    store = getattr(deps, "sanctions_store", None)
    active_versions = _call_optional(store, "active_versions") or []
    ingested = [
        _attr(row, "ingested_at")
        for row in active_versions
        if isinstance(_attr(row, "ingested_at"), datetime)
    ]
    if not ingested:
        return 0, True
    latest = max(ingested)
    age = max(0, int((now - latest).total_seconds()))
    return age, age > 24 * 60 * 60


def _alert_histogram(alerts: list[Any], now: datetime) -> list[dict[str, int | str]]:
    buckets = {"<1h": 0, "1-24h": 0, "1-3d": 0, ">3d": 0}
    for row in alerts:
        raised_at = _attr(row, "raised_at")
        if not isinstance(raised_at, datetime):
            continue
        age_seconds = max(0, (now - raised_at).total_seconds())
        if age_seconds < 60 * 60:
            buckets["<1h"] += 1
        elif age_seconds < 24 * 60 * 60:
            buckets["1-24h"] += 1
        elif age_seconds < 3 * 24 * 60 * 60:
            buckets["1-3d"] += 1
        else:
            buckets[">3d"] += 1
    return [{"bucket": bucket, "count": count} for bucket, count in buckets.items()]


def _cursor_offset(cursor: str | None) -> int:
    if cursor is None:
        return 0
    try:
        offset = int(cursor, 10)
    except ValueError as exc:
        raise InvalidRequestError(
            "cursor must be a non-negative integer offset", code="invalid_cursor"
        ) from exc
    if offset < 0:
        raise InvalidRequestError(
            "cursor must be a non-negative integer offset", code="invalid_cursor"
        )
    return offset


def _call_optional(obj: Any, method_name: str) -> Any:
    method = getattr(obj, method_name, None)
    return method() if callable(method) else None


def _attr(row: Any, name: str, default: Any = None) -> Any:
    if isinstance(row, dict):
        return row.get(name, default)
    return getattr(row, name, default)


def _enum_text(value: Any) -> str:
    return str(getattr(value, "value", value))


def _optional_ts(value: Any) -> str | None:
    if value is None:
        return None
    return _rfc3339(value)


def _rfc3339(value: Any) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, datetime):
        return ""
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _now(deps: Any) -> datetime:
    clock = getattr(deps, "clock", None) or SystemClock()
    now = clock.now()
    if now.tzinfo is None or now.tzinfo.utcoffset(now) is None:
        return now.replace(tzinfo=UTC)
    return now.astimezone(UTC)


def _percentile(samples: tuple[int, ...], pct: int) -> int:
    ordered = sorted(samples)
    index = max(0, min(len(ordered) - 1, ceil((pct / 100) * len(ordered)) - 1))
    return ordered[index]

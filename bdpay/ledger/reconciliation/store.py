"""ReconStore protocol + deterministic in-memory implementation (spec/04)."""

from __future__ import annotations

import dataclasses
from typing import Protocol

from bdpay.ledger.errors import ReconciliationError
from bdpay.ledger.reconciliation.types import (
    EXCEPTION_STATES,
    RECORD_STATES,
    ReconciliationExceptionRow,
    ReconciliationRecordRow,
)

_RECORD_MUTABLE = frozenset(
    {"match_status", "match_level", "matched_instruction_id", "delta_minor", "matched_at"}
)

_EXCEPTION_MUTABLE = frozenset(
    {
        "status",
        "assigned_to",
        "resolved_at",
        "closed_at",
        "resolution_action",
        "adjustment_amount_minor",
        "journal_entry_id",
        "approval_request_id",
        "notes",
        "supporting_ref",
        "resolved_by",
        "aml_alert_id",
        "resolution_tier",  # spec/PT-H: tier-upgrade path (AUTO_TIMING_GAP → FINANCE_QUEUE)
    }
)


class ReconStore(Protocol):
    """Persistence boundary for reconciliation records + exceptions."""

    def insert_record(self, row: ReconciliationRecordRow) -> None: ...

    def get_record(self, record_id: str) -> ReconciliationRecordRow | None: ...

    def update_record(self, record_id: str, **changes: object) -> ReconciliationRecordRow: ...

    def list_records(
        self, batch_id: str | None = None, match_status: str | None = None
    ) -> list[ReconciliationRecordRow]: ...

    def insert_exception(self, row: ReconciliationExceptionRow) -> None: ...

    def get_exception(self, exception_id: str) -> ReconciliationExceptionRow | None: ...

    def update_exception(
        self, exception_id: str, **changes: object
    ) -> ReconciliationExceptionRow: ...

    def list_exceptions(
        self, status: str | None = None, resolution_tier: str | None = None
    ) -> list[ReconciliationExceptionRow]: ...

    def find_exception_for_record(
        self, record_id: str
    ) -> ReconciliationExceptionRow | None: ...


def _checked(allowed: frozenset[str], changes: dict[str, object]) -> dict[str, object]:
    unknown = set(changes) - allowed
    if unknown:
        raise ReconciliationError(f"non-updatable column(s) {sorted(unknown)}")
    return changes


class InMemoryReconStore:
    """Deterministic in-memory store; semantics mirror the Postgres store."""

    def __init__(self) -> None:
        self._records: dict[str, ReconciliationRecordRow] = {}
        self._exceptions: dict[str, ReconciliationExceptionRow] = {}
        self._record_order: list[str] = []
        self._exception_order: list[str] = []

    # --- records ---

    def insert_record(self, row: ReconciliationRecordRow) -> None:
        if row.record_id in self._records:
            raise ReconciliationError(f"duplicate record_id {row.record_id}")
        if row.match_status not in RECORD_STATES:
            raise ReconciliationError(f"unknown match_status {row.match_status!r}")
        self._records[row.record_id] = row
        self._record_order.append(row.record_id)

    def get_record(self, record_id: str) -> ReconciliationRecordRow | None:
        return self._records.get(record_id)

    def update_record(self, record_id: str, **changes: object) -> ReconciliationRecordRow:
        row = self._records.get(record_id)
        if row is None:
            raise ReconciliationError(f"unknown record {record_id}")
        checked = _checked(_RECORD_MUTABLE, changes)
        status = checked.get("match_status")
        if status is not None and status not in RECORD_STATES:
            raise ReconciliationError(f"unknown match_status {status!r}")
        updated = dataclasses.replace(row, **checked)  # type: ignore[arg-type]
        self._records[record_id] = updated
        return updated

    def list_records(
        self, batch_id: str | None = None, match_status: str | None = None
    ) -> list[ReconciliationRecordRow]:
        out = []
        for record_id in self._record_order:
            row = self._records[record_id]
            if batch_id is not None and row.batch_id != batch_id:
                continue
            if match_status is not None and row.match_status != match_status:
                continue
            out.append(row)
        return out

    # --- exceptions ---

    def insert_exception(self, row: ReconciliationExceptionRow) -> None:
        if row.exception_id in self._exceptions:
            raise ReconciliationError(f"duplicate exception_id {row.exception_id}")
        if row.status not in EXCEPTION_STATES:
            raise ReconciliationError(f"unknown exception status {row.status!r}")
        self._exceptions[row.exception_id] = row
        self._exception_order.append(row.exception_id)

    def get_exception(self, exception_id: str) -> ReconciliationExceptionRow | None:
        return self._exceptions.get(exception_id)

    def update_exception(
        self, exception_id: str, **changes: object
    ) -> ReconciliationExceptionRow:
        row = self._exceptions.get(exception_id)
        if row is None:
            raise ReconciliationError(f"unknown exception {exception_id}")
        checked = _checked(_EXCEPTION_MUTABLE, changes)
        status = checked.get("status")
        if status is not None and status not in EXCEPTION_STATES:
            raise ReconciliationError(f"unknown exception status {status!r}")
        updated = dataclasses.replace(row, **checked)  # type: ignore[arg-type]
        self._exceptions[exception_id] = updated
        return updated

    def list_exceptions(
        self, status: str | None = None, resolution_tier: str | None = None
    ) -> list[ReconciliationExceptionRow]:
        out = []
        for exception_id in self._exception_order:
            row = self._exceptions[exception_id]
            if status is not None and row.status != status:
                continue
            if resolution_tier is not None and row.resolution_tier != resolution_tier:
                continue
            out.append(row)
        return out

    def find_exception_for_record(
        self, record_id: str
    ) -> ReconciliationExceptionRow | None:
        for exception_id in self._exception_order:
            row = self._exceptions[exception_id]
            if row.record_id == record_id and row.status in ("OPEN", "IN_REVIEW", "ESCALATED"):
                return row
        return None

"""Persistence contracts and in-memory storage for spec/17 disbursements."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace
from typing import Any, Protocol, runtime_checkable

from bdpay.kernel.disbursement.models import DisbursementBatch, DisbursementItem
from bdpay.platform.errors import ConflictError, NotFoundError

__all__ = [
    "DisbursementStore",
    "InMemoryDisbursementStore",
]


@runtime_checkable
class DisbursementStore(Protocol):
    def insert_batch(self, batch: DisbursementBatch, *, conn: Any | None = None) -> None: ...

    def update_batch(
        self,
        batch_id: str,
        *,
        conn: Any | None = None,
        expected_status: str | None = None,
        **changes: object,
    ) -> DisbursementBatch: ...

    def get_batch(
        self, batch_id: str, *, conn: Any | None = None, lock: bool = False
    ) -> DisbursementBatch | None: ...

    def find_batch_by_client_ref(
        self, merchant_id: str, client_batch_ref: str, *, conn: Any | None = None
    ) -> DisbursementBatch | None: ...

    def insert_items(
        self, items: Iterable[DisbursementItem], *, conn: Any | None = None
    ) -> None: ...

    def update_item(
        self,
        item_id: str,
        *,
        conn: Any | None = None,
        expected_status: str | None = None,
        **changes: object,
    ) -> DisbursementItem: ...

    def claim_item_for_dispatch(
        self, item_id: str, *, conn: Any | None = None
    ) -> DisbursementItem | None: ...

    def claim_item_returned(
        self, item_id: str, *, conn: Any | None = None
    ) -> DisbursementItem | None: ...

    def get_item(
        self, item_id: str, *, conn: Any | None = None
    ) -> DisbursementItem | None: ...

    def list_items(
        self, batch_id: str, *, status: str | None = None, conn: Any | None = None
    ) -> tuple[DisbursementItem, ...]: ...


class InMemoryDisbursementStore:
    """Deterministic in-memory store for unit tests and local composition."""

    def __init__(self) -> None:
        self._batches: dict[str, DisbursementBatch] = {}
        self._batch_ref_index: dict[tuple[str, str], str] = {}
        self._items: dict[str, DisbursementItem] = {}
        self._items_by_batch: dict[str, list[str]] = {}

    def insert_batch(self, batch: DisbursementBatch, *, conn: Any | None = None) -> None:
        del conn
        if batch.batch_id in self._batches:
            raise ConflictError("disbursement batch already exists", code="batch_exists")
        ref_key = (batch.merchant_id, batch.client_batch_ref)
        if ref_key in self._batch_ref_index:
            raise ConflictError(
                "client_batch_ref has already been used for this merchant",
                code="client_batch_ref_exists",
            )
        self._batches[batch.batch_id] = batch
        self._batch_ref_index[ref_key] = batch.batch_id
        self._items_by_batch.setdefault(batch.batch_id, [])

    def update_batch(
        self,
        batch_id: str,
        *,
        conn: Any | None = None,
        expected_status: str | None = None,
        **changes: object,
    ) -> DisbursementBatch:
        del conn
        batch = self._batches.get(batch_id)
        if batch is None:
            raise NotFoundError("disbursement batch not found", code="batch_not_found")
        if expected_status is not None and batch.status != expected_status:
            raise ConflictError("stale batch transition", code="stale_state_transition")
        updated = replace(batch, **changes)
        self._batches[batch_id] = updated
        return updated

    def get_batch(
        self, batch_id: str, *, conn: Any | None = None, lock: bool = False
    ) -> DisbursementBatch | None:
        del conn, lock
        return self._batches.get(batch_id)

    def find_batch_by_client_ref(
        self, merchant_id: str, client_batch_ref: str, *, conn: Any | None = None
    ) -> DisbursementBatch | None:
        del conn
        batch_id = self._batch_ref_index.get((merchant_id, client_batch_ref))
        return None if batch_id is None else self._batches[batch_id]

    def insert_items(
        self, items: Iterable[DisbursementItem], *, conn: Any | None = None
    ) -> None:
        del conn
        items_tuple = tuple(items)
        for item in items_tuple:
            if item.item_id in self._items:
                raise ConflictError("disbursement item already exists", code="item_exists")
            if item.batch_id not in self._batches:
                raise NotFoundError("disbursement batch not found", code="batch_not_found")
        for item in items_tuple:
            self._items[item.item_id] = item
            self._items_by_batch.setdefault(item.batch_id, []).append(item.item_id)

    def update_item(
        self,
        item_id: str,
        *,
        conn: Any | None = None,
        expected_status: str | None = None,
        **changes: object,
    ) -> DisbursementItem:
        del conn
        item = self._items.get(item_id)
        if item is None:
            raise NotFoundError("disbursement item not found", code="item_not_found")
        if expected_status is not None and item.status != expected_status:
            raise ConflictError("stale item transition", code="stale_state_transition")
        updated = replace(item, **changes)
        self._items[item_id] = updated
        return updated

    def claim_item_for_dispatch(
        self, item_id: str, *, conn: Any | None = None
    ) -> DisbursementItem | None:
        del conn
        item = self._items.get(item_id)
        if item is None or item.status != "QUEUED":
            return None
        updated = replace(item, status="DISPATCHING")
        self._items[item_id] = updated
        return updated

    def claim_item_returned(
        self, item_id: str, *, conn: Any | None = None
    ) -> DisbursementItem | None:
        del conn
        item = self._items.get(item_id)
        if item is None or item.status not in ("DISPATCHED", "DISPATCHING"):
            return None
        updated = replace(item, status="RETURNED")
        self._items[item_id] = updated
        return updated

    def get_item(
        self, item_id: str, *, conn: Any | None = None
    ) -> DisbursementItem | None:
        del conn
        return self._items.get(item_id)

    def list_items(
        self, batch_id: str, *, status: str | None = None, conn: Any | None = None
    ) -> tuple[DisbursementItem, ...]:
        del conn
        item_ids = self._items_by_batch.get(batch_id, [])
        items = [self._items[item_id] for item_id in item_ids]
        if status is not None:
            items = [item for item in items if item.status == status]
        return tuple(sorted(items, key=lambda item: item.ordinal))

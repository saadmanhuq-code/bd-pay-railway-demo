"""QR repository — Protocol + in-memory + Postgres (IMPLEMENTATION.md rule).

The in-memory implementation backs the deterministic unit suite; the Postgres
implementation matches ``db/migrations/0051_qr.sql`` exactly and is exercised
by ``@pytest.mark.integration`` tests. ``qr_scan_resolutions`` is append-only:
the only permitted mutation is setting ``payment_intent_id`` when ``/pay``
proceeds (spec/13 data model).
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from dataclasses import replace
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from bdpay.qr.codec import QrType
from bdpay.qr.model import (
    DynamicPayloadState,
    InteropDirection,
    MerchantQr,
    MerchantQrState,
    QrInteropTestCase,
    QrPayload,
    QrScanResolution,
    ValidationResult,
)

__all__ = ["InMemoryQrRepository", "PostgresQrRepository", "QrRepository"]


@runtime_checkable
class QrRepository(Protocol):
    def transaction(self) -> Any: ...

    # MerchantQr
    def insert_merchant_qr(self, row: MerchantQr) -> None: ...

    def get_merchant_qr(self, merchant_qr_id: str) -> MerchantQr | None: ...

    def save_merchant_qr(self, row: MerchantQr) -> None: ...

    def list_merchant_qrs(self, merchant_id: str) -> list[MerchantQr]: ...

    def find_merchant_qr_by_label(self, merchant_id: str, label: str) -> MerchantQr | None: ...

    # QrPayload
    def insert_payload(self, row: QrPayload) -> None: ...

    def get_payload(self, payload_id: str) -> QrPayload | None: ...

    def get_payload_by_hash(self, payload_hash: str) -> QrPayload | None: ...

    def save_payload(self, row: QrPayload) -> None: ...

    def find_dynamic_by_intent(self, payment_intent_id: str) -> QrPayload | None: ...

    def list_expirable(self, now: datetime) -> list[QrPayload]: ...

    # QrScanResolution (append-only + the single /pay mutation)
    def insert_resolution(self, row: QrScanResolution) -> None: ...

    def get_resolution(self, resolution_id: str) -> QrScanResolution | None: ...

    def set_resolution_intent(self, resolution_id: str, payment_intent_id: str) -> None: ...

    def list_resolutions(self) -> list[QrScanResolution]: ...

    # QrInteropTestCase
    def upsert_interop_case(self, row: QrInteropTestCase) -> None: ...

    def list_interop_cases(self, counterparty_class: str | None) -> list[QrInteropTestCase]: ...


class InMemoryQrRepository:
    """Deterministic dict-backed implementation for the unit suite."""

    def __init__(self) -> None:
        self._merchant_qrs: dict[str, MerchantQr] = {}
        self._payloads: dict[str, QrPayload] = {}
        self._payloads_by_hash: dict[str, str] = {}
        self._resolutions: dict[str, QrScanResolution] = {}
        self._resolution_order: list[str] = []
        self._interop: dict[str, QrInteropTestCase] = {}

    @contextlib.contextmanager
    def transaction(self) -> Iterator[None]:
        merchant_qrs = dict(self._merchant_qrs)
        payloads = dict(self._payloads)
        payloads_by_hash = dict(self._payloads_by_hash)
        resolutions = dict(self._resolutions)
        resolution_order = list(self._resolution_order)
        interop = dict(self._interop)
        try:
            yield
        except Exception:
            self._merchant_qrs = merchant_qrs
            self._payloads = payloads
            self._payloads_by_hash = payloads_by_hash
            self._resolutions = resolutions
            self._resolution_order = resolution_order
            self._interop = interop
            raise

    # -- MerchantQr ---------------------------------------------------------

    def insert_merchant_qr(self, row: MerchantQr) -> None:
        if row.merchant_qr_id in self._merchant_qrs:
            raise ValueError(f"duplicate merchant_qr_id {row.merchant_qr_id}")
        self._merchant_qrs[row.merchant_qr_id] = row

    def get_merchant_qr(self, merchant_qr_id: str) -> MerchantQr | None:
        return self._merchant_qrs.get(merchant_qr_id)

    def save_merchant_qr(self, row: MerchantQr) -> None:
        if row.merchant_qr_id not in self._merchant_qrs:
            raise ValueError(f"unknown merchant_qr_id {row.merchant_qr_id}")
        self._merchant_qrs[row.merchant_qr_id] = row

    def list_merchant_qrs(self, merchant_id: str) -> list[MerchantQr]:
        return [r for r in self._merchant_qrs.values() if r.merchant_id == merchant_id]

    def find_merchant_qr_by_label(self, merchant_id: str, label: str) -> MerchantQr | None:
        for row in self._merchant_qrs.values():
            if row.merchant_id == merchant_id and row.label == label:
                return row
        return None

    # -- QrPayload ----------------------------------------------------------

    def insert_payload(self, row: QrPayload) -> None:
        if row.payload_id in self._payloads or row.payload_hash in self._payloads_by_hash:
            raise ValueError(f"duplicate payload {row.payload_id}")
        self._payloads[row.payload_id] = row
        self._payloads_by_hash[row.payload_hash] = row.payload_id

    def get_payload(self, payload_id: str) -> QrPayload | None:
        return self._payloads.get(payload_id)

    def get_payload_by_hash(self, payload_hash: str) -> QrPayload | None:
        payload_id = self._payloads_by_hash.get(payload_hash)
        return self._payloads.get(payload_id) if payload_id else None

    def save_payload(self, row: QrPayload) -> None:
        if row.payload_id not in self._payloads:
            raise ValueError(f"unknown payload_id {row.payload_id}")
        self._payloads[row.payload_id] = row

    def find_dynamic_by_intent(self, payment_intent_id: str) -> QrPayload | None:
        for row in self._payloads.values():
            if row.qr_type is QrType.DYNAMIC and row.payment_intent_id == payment_intent_id:
                return row
        return None

    def list_expirable(self, now: datetime) -> list[QrPayload]:
        return [
            row
            for row in self._payloads.values()
            if row.qr_type is QrType.DYNAMIC
            and row.state in (DynamicPayloadState.ISSUED, DynamicPayloadState.SCANNED)
            and row.expires_at is not None
            and row.expires_at < now
        ]

    # -- QrScanResolution ----------------------------------------------------

    def insert_resolution(self, row: QrScanResolution) -> None:
        if row.resolution_id in self._resolutions:
            raise ValueError(f"duplicate resolution_id {row.resolution_id}")
        self._resolutions[row.resolution_id] = row
        self._resolution_order.append(row.resolution_id)

    def get_resolution(self, resolution_id: str) -> QrScanResolution | None:
        return self._resolutions.get(resolution_id)

    def set_resolution_intent(self, resolution_id: str, payment_intent_id: str) -> None:
        row = self._resolutions[resolution_id]
        self._resolutions[resolution_id] = replace(row, payment_intent_id=payment_intent_id)

    def list_resolutions(self) -> list[QrScanResolution]:
        return [self._resolutions[rid] for rid in self._resolution_order]

    # -- QrInteropTestCase ----------------------------------------------------

    def upsert_interop_case(self, row: QrInteropTestCase) -> None:
        self._interop[row.test_case_id] = row

    def list_interop_cases(self, counterparty_class: str | None) -> list[QrInteropTestCase]:
        rows = sorted(self._interop.values(), key=lambda r: (r.name, r.version))
        if counterparty_class is None:
            return rows
        return [r for r in rows if r.counterparty_class == counterparty_class]


class PostgresQrRepository:
    """psycopg3 implementation matching db/migrations/0051_qr.sql.

    ``connection`` is any psycopg connection-like object with ``cursor()``
    and ``commit()``; transaction scope is managed by the caller/service
    wiring. Exercised only by integration-marked tests.
    """

    def __init__(self, connection: Any) -> None:
        self._conn = connection

    def transaction(self) -> Any:
        return self._conn.transaction()

    def _execute(self, sql: str, params: tuple) -> Any:
        cur = self._conn.cursor()
        cur.execute(sql, params)
        return cur

    # -- MerchantQr ---------------------------------------------------------

    _MQR_COLS = (
        "merchant_qr_id, merchant_id, state, label, store_id, terminal_id, "
        "current_payload_id, suspend_reason, created_at, updated_at, schema_version"
    )

    @staticmethod
    def _mqr_from_row(row: tuple) -> MerchantQr:
        return MerchantQr(
            merchant_qr_id=row[0],
            merchant_id=row[1],
            state=MerchantQrState(row[2]),
            label=row[3],
            store_id=row[4],
            terminal_id=row[5],
            current_payload_id=row[6],
            suspend_reason=row[7],
            created_at=row[8],
            updated_at=row[9],
            schema_version=row[10],
        )

    def insert_merchant_qr(self, row: MerchantQr) -> None:
        self._execute(
            f"INSERT INTO merchant_qrs ({self._MQR_COLS}) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                row.merchant_qr_id,
                row.merchant_id,
                row.state.value,
                row.label,
                row.store_id,
                row.terminal_id,
                row.current_payload_id,
                row.suspend_reason,
                row.created_at,
                row.updated_at,
                row.schema_version,
            ),
        )

    def get_merchant_qr(self, merchant_qr_id: str) -> MerchantQr | None:
        cur = self._execute(
            f"SELECT {self._MQR_COLS} FROM merchant_qrs WHERE merchant_qr_id = %s",
            (merchant_qr_id,),
        )
        row = cur.fetchone()
        return self._mqr_from_row(row) if row else None

    def save_merchant_qr(self, row: MerchantQr) -> None:
        self._execute(
            "UPDATE merchant_qrs SET state=%s, current_payload_id=%s, suspend_reason=%s, "
            "updated_at=%s WHERE merchant_qr_id=%s",
            (
                row.state.value,
                row.current_payload_id,
                row.suspend_reason,
                row.updated_at,
                row.merchant_qr_id,
            ),
        )

    def list_merchant_qrs(self, merchant_id: str) -> list[MerchantQr]:
        cur = self._execute(
            f"SELECT {self._MQR_COLS} FROM merchant_qrs WHERE merchant_id = %s ORDER BY label",
            (merchant_id,),
        )
        return [self._mqr_from_row(r) for r in cur.fetchall()]

    def find_merchant_qr_by_label(self, merchant_id: str, label: str) -> MerchantQr | None:
        cur = self._execute(
            f"SELECT {self._MQR_COLS} FROM merchant_qrs WHERE merchant_id=%s AND label=%s",
            (merchant_id, label),
        )
        row = cur.fetchone()
        return self._mqr_from_row(row) if row else None

    # -- QrPayload ----------------------------------------------------------

    _QRP_COLS = (
        "payload_id, qr_type, merchant_qr_id, payment_intent_id, state, payload_string, "
        "payload_hash, amount_minor, currency, mcc, merchant_name, merchant_name_bn, "
        "merchant_city, expires_at, created_at, schema_version"
    )

    @staticmethod
    def _qrp_from_row(row: tuple) -> QrPayload:
        return QrPayload(
            payload_id=row[0],
            qr_type=QrType(row[1]),
            merchant_qr_id=row[2],
            payment_intent_id=row[3],
            state=DynamicPayloadState(row[4]),
            payload_string=row[5],
            payload_hash=row[6],
            amount_minor=row[7],
            currency=row[8],
            mcc=row[9],
            merchant_name=row[10],
            merchant_name_bn=row[11],
            merchant_city=row[12],
            expires_at=row[13],
            created_at=row[14],
            schema_version=row[15],
        )

    def insert_payload(self, row: QrPayload) -> None:
        self._execute(
            f"INSERT INTO qr_payloads ({self._QRP_COLS}) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                row.payload_id,
                row.qr_type.value,
                row.merchant_qr_id,
                row.payment_intent_id,
                row.state.value,
                row.payload_string,
                row.payload_hash,
                row.amount_minor,
                row.currency,
                row.mcc,
                row.merchant_name,
                row.merchant_name_bn,
                row.merchant_city,
                row.expires_at,
                row.created_at,
                row.schema_version,
            ),
        )

    def get_payload(self, payload_id: str) -> QrPayload | None:
        cur = self._execute(
            f"SELECT {self._QRP_COLS} FROM qr_payloads WHERE payload_id = %s", (payload_id,)
        )
        row = cur.fetchone()
        return self._qrp_from_row(row) if row else None

    def get_payload_by_hash(self, payload_hash: str) -> QrPayload | None:
        cur = self._execute(
            f"SELECT {self._QRP_COLS} FROM qr_payloads WHERE payload_hash = %s", (payload_hash,)
        )
        row = cur.fetchone()
        return self._qrp_from_row(row) if row else None

    def save_payload(self, row: QrPayload) -> None:
        self._execute(
            "UPDATE qr_payloads SET state=%s WHERE payload_id=%s",
            (row.state.value, row.payload_id),
        )

    def find_dynamic_by_intent(self, payment_intent_id: str) -> QrPayload | None:
        cur = self._execute(
            f"SELECT {self._QRP_COLS} FROM qr_payloads "
            "WHERE qr_type='DYNAMIC' AND payment_intent_id = %s",
            (payment_intent_id,),
        )
        row = cur.fetchone()
        return self._qrp_from_row(row) if row else None

    def list_expirable(self, now: datetime) -> list[QrPayload]:
        cur = self._execute(
            f"SELECT {self._QRP_COLS} FROM qr_payloads WHERE qr_type='DYNAMIC' "
            "AND state IN ('ISSUED','SCANNED') AND expires_at < %s",
            (now,),
        )
        return [self._qrp_from_row(r) for r in cur.fetchall()]

    # -- QrScanResolution ----------------------------------------------------

    _QRES_COLS = (
        "resolution_id, payload_hash, payload_id, on_us, customer_ref, qr_type, "
        "validation_result, failure_code, quoted_fee_minor, payment_intent_id, "
        "scanned_at, schema_version"
    )

    @staticmethod
    def _qres_from_row(row: tuple) -> QrScanResolution:
        return QrScanResolution(
            resolution_id=row[0],
            payload_hash=row[1],
            payload_id=row[2],
            on_us=row[3],
            customer_ref=row[4],
            qr_type=row[5],
            validation_result=ValidationResult(row[6]),
            failure_code=row[7],
            quoted_fee_minor=row[8],
            payment_intent_id=row[9],
            scanned_at=row[10],
            schema_version=row[11],
        )

    def insert_resolution(self, row: QrScanResolution) -> None:
        self._execute(
            f"INSERT INTO qr_scan_resolutions ({self._QRES_COLS}) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                row.resolution_id,
                row.payload_hash,
                row.payload_id,
                row.on_us,
                row.customer_ref,
                row.qr_type,
                row.validation_result.value,
                row.failure_code,
                row.quoted_fee_minor,
                row.payment_intent_id,
                row.scanned_at,
                row.schema_version,
            ),
        )

    def get_resolution(self, resolution_id: str) -> QrScanResolution | None:
        cur = self._execute(
            f"SELECT {self._QRES_COLS} FROM qr_scan_resolutions WHERE resolution_id = %s",
            (resolution_id,),
        )
        row = cur.fetchone()
        return self._qres_from_row(row) if row else None

    def set_resolution_intent(self, resolution_id: str, payment_intent_id: str) -> None:
        self._execute(
            "UPDATE qr_scan_resolutions SET payment_intent_id=%s WHERE resolution_id=%s",
            (payment_intent_id, resolution_id),
        )

    def list_resolutions(self) -> list[QrScanResolution]:
        cur = self._execute(
            f"SELECT {self._QRES_COLS} FROM qr_scan_resolutions ORDER BY scanned_at", ()
        )
        return [self._qres_from_row(r) for r in cur.fetchall()]

    # -- QrInteropTestCase ----------------------------------------------------

    _QTEST_COLS = (
        "test_case_id, name, version, direction, payload_string, expected, "
        "counterparty_class, last_result, last_run_at, created_at, schema_version"
    )

    @staticmethod
    def _qtest_from_row(row: tuple) -> QrInteropTestCase:
        return QrInteropTestCase(
            test_case_id=row[0],
            name=row[1],
            version=row[2],
            direction=InteropDirection(row[3]),
            payload_string=row[4],
            expected=row[5],
            counterparty_class=row[6],
            last_result=row[7],
            last_run_at=row[8],
            created_at=row[9],
            schema_version=row[10],
        )

    def upsert_interop_case(self, row: QrInteropTestCase) -> None:
        from psycopg.types.json import Jsonb  # local import: unit suite has no pg

        self._execute(
            f"INSERT INTO qr_interop_test_cases ({self._QTEST_COLS}) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (test_case_id) DO UPDATE SET "
            "payload_string=EXCLUDED.payload_string, expected=EXCLUDED.expected, "
            "last_result=EXCLUDED.last_result, last_run_at=EXCLUDED.last_run_at",
            (
                row.test_case_id,
                row.name,
                row.version,
                row.direction.value,
                row.payload_string,
                Jsonb(row.expected),
                row.counterparty_class,
                row.last_result,
                row.last_run_at,
                row.created_at,
                row.schema_version,
            ),
        )

    def list_interop_cases(self, counterparty_class: str | None) -> list[QrInteropTestCase]:
        if counterparty_class is None:
            cur = self._execute(
                f"SELECT {self._QTEST_COLS} FROM qr_interop_test_cases ORDER BY name, version", ()
            )
        else:
            cur = self._execute(
                f"SELECT {self._QTEST_COLS} FROM qr_interop_test_cases "
                "WHERE counterparty_class = %s ORDER BY name, version",
                (counterparty_class,),
            )
        return [self._qtest_from_row(r) for r in cur.fetchall()]

"""Gateway customer intake/read adapter backed by the kernel KYC tables."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from bdpay.kernel.kyc.kyc_models import CustomerRecord, KycRecordModel
from bdpay.kernel.kyc.kyc_repository import KycStore
from bdpay.platform.clock import Clock
from bdpay.platform.ids import make_id

__all__ = ["InMemoryCustomerService", "PostgresCustomerService"]


class InMemoryCustomerService:
    """Minimal in-memory customer service for memory-mode tests."""

    def __init__(self, *, balance_cap_minor: int = 40_000_000) -> None:
        self._customers: dict[str, dict] = {}
        self._balance_cap = balance_cap_minor

    def submit_customer(
        self, payload: Any, *, merchant_id: str | None, idempotency_key: str, clock: Clock
    ) -> Any:
        cust_id = make_id("cust", {"k": idempotency_key, "m": merchant_id})
        record = {
            "customer_id": cust_id,
            "status": "SUBMITTED",
            "kyc_tier": None,
            "merchant_id": merchant_id,
        }
        self._customers[cust_id] = record
        return record

    def get_customer(self, customer_id: str) -> Any | None:
        return self._customers.get(customer_id)

    def get_balance(self, customer_id: str) -> Any | None:
        if customer_id not in self._customers:
            return None
        return {
            "customer_id": customer_id,
            "balance_minor": 0,
            "currency": "BDT",
            "balance_cap_minor": self._balance_cap,
        }


class PostgresCustomerService:
    """Durable gateway customer profile over canonical KYC customer rows."""

    def __init__(
        self,
        connection_factory: Callable[[], Any],
        *,
        kyc_store: KycStore,
        balance_cap_minor: int,
    ) -> None:
        self._connect = connection_factory
        self._kyc = kyc_store
        self._balance_cap = balance_cap_minor

    def submit_customer(
        self,
        payload: Mapping[str, object],
        *,
        merchant_id: str | None,
        idempotency_key: str,
        clock: Clock,
    ) -> dict[str, object]:
        now = clock.now()
        customer_id = make_id("cust", {"k": idempotency_key, "m": merchant_id})
        kyc_record_id = make_id(
            "kyc",
            {"customer_id": customer_id, "idempotency_key": idempotency_key},
        )
        with self._connect() as conn:
            existing = self._profile_row(customer_id, conn=conn)
            if existing is not None:
                return self._to_customer(existing)
            if self._kyc.get_customer(customer_id, conn=conn) is None:
                self._kyc.insert_customer(
                    CustomerRecord(
                        customer_id=customer_id,
                        full_name_en=str(
                            payload.get("name_en")
                            or payload.get("full_name_en")
                            or "Customer"
                        ),
                        full_name_bn=(
                            str(payload["name_bn"]) if payload.get("name_bn") is not None else None
                        ),
                        is_corporate=bool(payload.get("is_corporate", False)),
                        created_at=now,
                        updated_at=now,
                    ),
                    conn=conn,
                )
            if self._kyc.get_record(kyc_record_id, conn=conn) is None:
                self._kyc.insert_record(
                    KycRecordModel(
                        kyc_record_id=kyc_record_id,
                        customer_id=customer_id,
                        state="SUBMITTED",
                        tier_requested=str(payload.get("tier_requested") or "SIMPLIFIED"),
                        submitted_at=now,
                        created_at=now,
                        updated_at=now,
                    ),
                    conn=conn,
                )
            conn.execute(
                """
                INSERT INTO customer_profiles (
                    customer_id,
                    merchant_id,
                    status,
                    kyc_record_id,
                    balance_minor,
                    currency,
                    balance_cap_minor,
                    created_at,
                    updated_at,
                    schema_version
                )
                VALUES (%s, %s, 'SUBMITTED', %s, 0, 'BDT', %s, %s, %s, 1)
                """,
                (customer_id, merchant_id, kyc_record_id, self._balance_cap, now, now),
            )
            conn.commit()
            row = self._profile_row(customer_id, conn=conn)
            assert row is not None
            return self._to_customer(row)

    def get_customer(self, customer_id: str) -> dict[str, object] | None:
        with self._connect() as conn:
            row = self._profile_row(customer_id, conn=conn)
        return None if row is None else self._to_customer(row)

    def get_balance(self, customer_id: str) -> dict[str, object] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT customer_id, balance_minor, currency, balance_cap_minor
                FROM customer_profiles
                WHERE customer_id = %s
                """,
                (customer_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "customer_id": row[0],
            "balance_minor": row[1],
            "currency": row[2],
            "balance_cap_minor": row[3],
        }

    def _profile_row(self, customer_id: str, *, conn: Any) -> Mapping[str, object] | None:
        from psycopg.rows import dict_row

        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT
                    p.customer_id,
                    p.merchant_id,
                    p.status,
                    p.balance_minor,
                    p.currency,
                    p.balance_cap_minor,
                    c.full_name_en,
                    c.full_name_bn,
                    c.is_corporate,
                    kr.kyc_record_id,
                    kr.state AS kyc_state,
                    kr.tier
                FROM customer_profiles p
                JOIN customers c ON c.customer_id = p.customer_id
                LEFT JOIN kyc_records kr ON kr.kyc_record_id = p.kyc_record_id
                WHERE p.customer_id = %s
                """,
                (customer_id,),
            )
            return cur.fetchone()

    @staticmethod
    def _to_customer(row: Mapping[str, object]) -> dict[str, object]:
        return {
            "customer_id": row["customer_id"],
            "status": row["kyc_state"] or row["status"],
            "kyc_tier": row["tier"],
            "merchant_id": row["merchant_id"],
            "full_name_en": row["full_name_en"],
            "full_name_bn": row["full_name_bn"],
            "is_corporate": row["is_corporate"],
        }

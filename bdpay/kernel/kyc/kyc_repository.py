"""KYC storage — repository protocol, in-memory and Postgres stores.

Postgres statements match ``db/migrations/0031`` (customers) and
``0045..0047`` exactly; the in-memory store is the deterministic unit-test
double.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Protocol, runtime_checkable

from bdpay.kernel.kyc.kyc_models import (
    BiometricAttemptRecord,
    CustomerRecord,
    KycManualReviewRecord,
    KycRecordModel,
)
from bdpay.platform.errors import ConflictError, NotFoundError

__all__ = ["InMemoryKycStore", "KycStore", "PostgresKycStore"]

#: Intake states where a second concurrent session is refused (spec/09 §1).
OPEN_SESSION_STATES = (
    "SUBMITTED",
    "NID_OCR_RECEIVED",
    "BIOMETRIC_PENDING",
    "BIOMETRIC_RETRY_PENDING",
    "BIOMETRIC_PASSED",
    "PENDING_HUMAN_REVIEW",
    "REFRESH_PENDING",
)


@runtime_checkable
class KycStore(Protocol):
    """Storage contract for customers + KYC aggregates."""

    def insert_customer(self, record: CustomerRecord, *, conn: Any | None = None) -> None: ...

    def get_customer(
        self, customer_id: str, *, conn: Any | None = None
    ) -> CustomerRecord | None: ...

    def insert_record(self, record: KycRecordModel, *, conn: Any | None = None) -> None: ...

    def get_record(
        self, kyc_record_id: str, *, conn: Any | None = None
    ) -> KycRecordModel | None: ...

    def update_record(self, record: KycRecordModel, *, conn: Any | None = None) -> None: ...

    def find_active_record(
        self, customer_id: str, *, conn: Any | None = None
    ) -> KycRecordModel | None: ...

    def find_open_session(
        self, customer_id: str, *, conn: Any | None = None
    ) -> KycRecordModel | None: ...

    def list_records(
        self, *, customer_id: str | None = None, state: str | None = None,
        conn: Any | None = None,
    ) -> list[KycRecordModel]: ...

    def insert_attempt(
        self, record: BiometricAttemptRecord, *, conn: Any | None = None
    ) -> None: ...

    def list_attempts(
        self, kyc_record_id: str, *, conn: Any | None = None
    ) -> list[BiometricAttemptRecord]: ...

    def insert_review(
        self, record: KycManualReviewRecord, *, conn: Any | None = None
    ) -> None: ...

    def update_review(
        self, record: KycManualReviewRecord, *, conn: Any | None = None
    ) -> None: ...

    def find_open_review(
        self, kyc_record_id: str, *, conn: Any | None = None
    ) -> KycManualReviewRecord | None: ...

    def count_open_reviews(
        self, review_reason: str, *, conn: Any | None = None
    ) -> int: ...


class InMemoryKycStore:
    """Deterministic in-memory store for unit tests."""

    def __init__(self) -> None:
        self._customers: dict[str, CustomerRecord] = {}
        self._records: dict[str, KycRecordModel] = {}
        self._attempts: dict[str, BiometricAttemptRecord] = {}
        self._reviews: dict[str, KycManualReviewRecord] = {}

    def insert_customer(self, record: CustomerRecord, *, conn: Any | None = None) -> None:
        if record.customer_id in self._customers:
            raise ConflictError(f"customer {record.customer_id} exists")
        self._customers[record.customer_id] = record

    def get_customer(
        self, customer_id: str, *, conn: Any | None = None
    ) -> CustomerRecord | None:
        return self._customers.get(customer_id)

    def insert_record(self, record: KycRecordModel, *, conn: Any | None = None) -> None:
        if record.kyc_record_id in self._records:
            raise ConflictError(f"kyc record {record.kyc_record_id} exists")
        if record.state == "ACTIVE" and self.find_active_record(record.customer_id):
            raise ConflictError("one ACTIVE kyc record per customer")
        self._records[record.kyc_record_id] = record

    def get_record(
        self, kyc_record_id: str, *, conn: Any | None = None
    ) -> KycRecordModel | None:
        return self._records.get(kyc_record_id)

    def update_record(self, record: KycRecordModel, *, conn: Any | None = None) -> None:
        if record.kyc_record_id not in self._records:
            raise NotFoundError(f"unknown kyc record {record.kyc_record_id}")
        if record.state == "ACTIVE":
            active = self.find_active_record(record.customer_id)
            if active is not None and active.kyc_record_id != record.kyc_record_id:
                raise ConflictError("one ACTIVE kyc record per customer")
        self._records[record.kyc_record_id] = record

    def find_active_record(
        self, customer_id: str, *, conn: Any | None = None
    ) -> KycRecordModel | None:
        for record in self._records.values():
            if record.customer_id == customer_id and record.state == "ACTIVE":
                return record
        return None

    def find_open_session(
        self, customer_id: str, *, conn: Any | None = None
    ) -> KycRecordModel | None:
        for record in sorted(self._records.values(), key=lambda r: r.created_at):
            if record.customer_id == customer_id and record.state in OPEN_SESSION_STATES:
                return record
        return None

    def list_records(
        self, *, customer_id: str | None = None, state: str | None = None,
        conn: Any | None = None,
    ) -> list[KycRecordModel]:
        rows = [
            r
            for r in self._records.values()
            if (customer_id is None or r.customer_id == customer_id)
            and (state is None or r.state == state)
        ]
        return sorted(rows, key=lambda r: r.created_at)

    def insert_attempt(
        self, record: BiometricAttemptRecord, *, conn: Any | None = None
    ) -> None:
        if record.attempt_id in self._attempts:
            raise ConflictError(f"attempt {record.attempt_id} exists")
        for existing in self._attempts.values():
            if (
                existing.kyc_record_id == record.kyc_record_id
                and existing.attempt_number == record.attempt_number
            ):
                raise ConflictError("duplicate biometric attempt number")
        self._attempts[record.attempt_id] = record

    def list_attempts(
        self, kyc_record_id: str, *, conn: Any | None = None
    ) -> list[BiometricAttemptRecord]:
        return sorted(
            (a for a in self._attempts.values() if a.kyc_record_id == kyc_record_id),
            key=lambda a: a.attempt_number,
        )

    def insert_review(
        self, record: KycManualReviewRecord, *, conn: Any | None = None
    ) -> None:
        if record.review_id in self._reviews:
            raise ConflictError(f"review {record.review_id} exists")
        self._reviews[record.review_id] = record

    def update_review(
        self, record: KycManualReviewRecord, *, conn: Any | None = None
    ) -> None:
        if record.review_id not in self._reviews:
            raise NotFoundError(f"unknown review {record.review_id}")
        self._reviews[record.review_id] = record

    def find_open_review(
        self, kyc_record_id: str, *, conn: Any | None = None
    ) -> KycManualReviewRecord | None:
        for review in sorted(self._reviews.values(), key=lambda r: r.created_at):
            if review.kyc_record_id == kyc_record_id and review.review_state != "REVIEW_CLOSED":
                return review
        return None

    def count_open_reviews(self, review_reason: str, *, conn: Any | None = None) -> int:
        """Open manual reviews for ``review_reason`` (spec/16 §I queue depth).

        Errata E-S16-09: spec/16 counts ``kyc_records`` rows by
        ``review_reason``, but the built ``kyc_records`` schema carries no
        review_reason column — the reason lives on the manual-review row.
        The equivalent depth is the count of NON-CLOSED reviews with the
        reason (every PENDING_HUMAN_REVIEW record has exactly one open
        review, opened in the same transaction).
        """
        return sum(
            1
            for review in self._reviews.values()
            if review.review_reason == review_reason
            and review.review_state != "REVIEW_CLOSED"
        )


# ---------------------------------------------------------------------------
# Postgres implementation (migrations 0031, 0045-0047)
# ---------------------------------------------------------------------------

_CUSTOMER_COLS = (
    "customer_id, full_name_en, full_name_bn, is_corporate, created_at, updated_at, "
    "schema_version"
)

_RECORD_COLS = (
    "kyc_record_id, customer_id, state, tier_requested, tier, tier_assigned_at, "
    "previous_kyc_record_id, channel, is_minor, guardian_customer_id, nid_hash, "
    "nid_encrypted, nid_type, nid_dob_iso, name_en_norm, name_bn_norm, risk_tier, "
    "refresh_due_at, refresh_hard_deadline_at, rejection_reason_code, submitted_at, "
    "last_biometric_at, archived_at, expired_at, produced_by, created_at, updated_at, "
    "schema_version"
)

_ATTEMPT_COLS = (
    "attempt_id, kyc_record_id, attempt_number, attempted_at, matched, porichoy_ref, "
    "porichoy_response_hash, connector_result_id, liveness_token_valid, "
    "liveness_sdk_version, schema_version"
)

_REVIEW_COLS = (
    "review_id, kyc_record_id, review_state, review_reason, approval_request_id, "
    "assigned_to_operator_id, reviewer_notes, decision, rejection_reason_code, "
    "decided_at, decided_by_operator_id, created_at, schema_version"
)


class PostgresKycStore:
    """psycopg3 store matching migrations 0031 + 0045-0047 exactly."""

    def __init__(self, connection_factory: Callable[[], Any]) -> None:
        self._connect = connection_factory

    def _execute(self, conn: Any | None, sql: str, params: tuple) -> None:
        if conn is not None:
            conn.execute(sql, params)
            return
        with self._connect() as owned:
            owned.execute(sql, params)
            owned.commit()

    def _fetchone(self, conn: Any | None, sql: str, params: tuple) -> Mapping[str, Any] | None:
        from psycopg.rows import dict_row

        if conn is not None:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(sql, params)
                return cur.fetchone()
        with self._connect() as owned:
            with owned.cursor(row_factory=dict_row) as cur:
                cur.execute(sql, params)
                return cur.fetchone()

    def _fetchall(self, conn: Any | None, sql: str, params: tuple) -> list[Mapping[str, Any]]:
        from psycopg.rows import dict_row

        if conn is not None:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(sql, params)
                return cur.fetchall()
        with self._connect() as owned:
            with owned.cursor(row_factory=dict_row) as cur:
                cur.execute(sql, params)
                return cur.fetchall()

    # -- customers --------------------------------------------------------------

    def insert_customer(self, record: CustomerRecord, *, conn: Any | None = None) -> None:
        self._execute(
            conn,
            f"INSERT INTO customers ({_CUSTOMER_COLS}) VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (
                record.customer_id,
                record.full_name_en,
                record.full_name_bn,
                record.is_corporate,
                record.created_at,
                record.updated_at,
                record.schema_version,
            ),
        )

    def get_customer(
        self, customer_id: str, *, conn: Any | None = None
    ) -> CustomerRecord | None:
        row = self._fetchone(
            conn,
            f"SELECT {_CUSTOMER_COLS} FROM customers WHERE customer_id = %s",
            (customer_id,),
        )
        if row is None:
            return None
        return CustomerRecord(
            customer_id=row["customer_id"],
            full_name_en=row["full_name_en"],
            full_name_bn=row["full_name_bn"],
            is_corporate=row["is_corporate"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            schema_version=row["schema_version"],
        )

    # -- kyc_records --------------------------------------------------------------

    @staticmethod
    def _row_to_record(row: Mapping[str, Any]) -> KycRecordModel:
        return KycRecordModel(
            kyc_record_id=row["kyc_record_id"],
            customer_id=row["customer_id"],
            state=row["state"],
            tier_requested=row["tier_requested"],
            tier=row["tier"],
            tier_assigned_at=row["tier_assigned_at"],
            previous_kyc_record_id=row["previous_kyc_record_id"],
            channel=row["channel"],
            is_minor=row["is_minor"],
            guardian_customer_id=row["guardian_customer_id"],
            nid_hash=row["nid_hash"],
            nid_encrypted=bytes(row["nid_encrypted"]) if row["nid_encrypted"] else None,
            nid_type=row["nid_type"],
            nid_dob_iso=row["nid_dob_iso"],
            name_en_norm=row["name_en_norm"],
            name_bn_norm=row["name_bn_norm"],
            risk_tier=row["risk_tier"],
            refresh_due_at=row["refresh_due_at"],
            refresh_hard_deadline_at=row["refresh_hard_deadline_at"],
            rejection_reason_code=row["rejection_reason_code"],
            submitted_at=row["submitted_at"],
            last_biometric_at=row["last_biometric_at"],
            archived_at=row["archived_at"],
            expired_at=row["expired_at"],
            produced_by=row["produced_by"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            schema_version=row["schema_version"],
        )

    def insert_record(self, record: KycRecordModel, *, conn: Any | None = None) -> None:
        placeholders = ", ".join(["%s"] * 28)
        self._execute(
            conn,
            f"INSERT INTO kyc_records ({_RECORD_COLS}) VALUES ({placeholders})",
            (
                record.kyc_record_id,
                record.customer_id,
                record.state,
                record.tier_requested,
                record.tier,
                record.tier_assigned_at,
                record.previous_kyc_record_id,
                record.channel,
                record.is_minor,
                record.guardian_customer_id,
                record.nid_hash,
                record.nid_encrypted,
                record.nid_type,
                record.nid_dob_iso,
                record.name_en_norm,
                record.name_bn_norm,
                record.risk_tier,
                record.refresh_due_at,
                record.refresh_hard_deadline_at,
                record.rejection_reason_code,
                record.submitted_at,
                record.last_biometric_at,
                record.archived_at,
                record.expired_at,
                record.produced_by,
                record.created_at,
                record.updated_at,
                record.schema_version,
            ),
        )

    def get_record(
        self, kyc_record_id: str, *, conn: Any | None = None
    ) -> KycRecordModel | None:
        row = self._fetchone(
            conn,
            f"SELECT {_RECORD_COLS} FROM kyc_records WHERE kyc_record_id = %s",
            (kyc_record_id,),
        )
        return None if row is None else self._row_to_record(row)

    def update_record(self, record: KycRecordModel, *, conn: Any | None = None) -> None:
        self._execute(
            conn,
            "UPDATE kyc_records SET state = %s, tier = %s, tier_assigned_at = %s, "
            "is_minor = %s, guardian_customer_id = %s, nid_hash = %s, nid_encrypted = %s, "
            "nid_type = %s, nid_dob_iso = %s, name_en_norm = %s, name_bn_norm = %s, "
            "risk_tier = %s, refresh_due_at = %s, refresh_hard_deadline_at = %s, "
            "rejection_reason_code = %s, submitted_at = %s, last_biometric_at = %s, "
            "archived_at = %s, expired_at = %s, updated_at = %s WHERE kyc_record_id = %s",
            (
                record.state,
                record.tier,
                record.tier_assigned_at,
                record.is_minor,
                record.guardian_customer_id,
                record.nid_hash,
                record.nid_encrypted,
                record.nid_type,
                record.nid_dob_iso,
                record.name_en_norm,
                record.name_bn_norm,
                record.risk_tier,
                record.refresh_due_at,
                record.refresh_hard_deadline_at,
                record.rejection_reason_code,
                record.submitted_at,
                record.last_biometric_at,
                record.archived_at,
                record.expired_at,
                record.updated_at,
                record.kyc_record_id,
            ),
        )

    def find_active_record(
        self, customer_id: str, *, conn: Any | None = None
    ) -> KycRecordModel | None:
        row = self._fetchone(
            conn,
            f"SELECT {_RECORD_COLS} FROM kyc_records "
            "WHERE customer_id = %s AND state = 'ACTIVE'",
            (customer_id,),
        )
        return None if row is None else self._row_to_record(row)

    def find_open_session(
        self, customer_id: str, *, conn: Any | None = None
    ) -> KycRecordModel | None:
        row = self._fetchone(
            conn,
            f"SELECT {_RECORD_COLS} FROM kyc_records "
            "WHERE customer_id = %s AND state = ANY(%s) ORDER BY created_at LIMIT 1",
            (customer_id, list(OPEN_SESSION_STATES)),
        )
        return None if row is None else self._row_to_record(row)

    def list_records(
        self, *, customer_id: str | None = None, state: str | None = None,
        conn: Any | None = None,
    ) -> list[KycRecordModel]:
        clauses = []
        params: list[Any] = []
        if customer_id is not None:
            clauses.append("customer_id = %s")
            params.append(customer_id)
        if state is not None:
            clauses.append("state = %s")
            params.append(state)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._fetchall(
            conn,
            f"SELECT {_RECORD_COLS} FROM kyc_records{where} ORDER BY created_at",
            tuple(params),
        )
        return [self._row_to_record(row) for row in rows]

    # -- biometric attempts ---------------------------------------------------------

    def insert_attempt(
        self, record: BiometricAttemptRecord, *, conn: Any | None = None
    ) -> None:
        self._execute(
            conn,
            f"INSERT INTO kyc_biometric_attempts ({_ATTEMPT_COLS}) VALUES "
            "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                record.attempt_id,
                record.kyc_record_id,
                record.attempt_number,
                record.attempted_at,
                record.matched,
                record.porichoy_ref,
                record.porichoy_response_hash,
                record.connector_result_id,
                record.liveness_token_valid,
                record.liveness_sdk_version,
                record.schema_version,
            ),
        )

    def list_attempts(
        self, kyc_record_id: str, *, conn: Any | None = None
    ) -> list[BiometricAttemptRecord]:
        rows = self._fetchall(
            conn,
            f"SELECT {_ATTEMPT_COLS} FROM kyc_biometric_attempts "
            "WHERE kyc_record_id = %s ORDER BY attempt_number",
            (kyc_record_id,),
        )
        return [
            BiometricAttemptRecord(
                attempt_id=row["attempt_id"],
                kyc_record_id=row["kyc_record_id"],
                attempt_number=row["attempt_number"],
                attempted_at=row["attempted_at"],
                matched=row["matched"],
                porichoy_ref=row["porichoy_ref"],
                porichoy_response_hash=row["porichoy_response_hash"],
                connector_result_id=row["connector_result_id"],
                liveness_token_valid=row["liveness_token_valid"],
                liveness_sdk_version=row["liveness_sdk_version"],
                schema_version=row["schema_version"],
            )
            for row in rows
        ]

    # -- manual reviews ---------------------------------------------------------------

    @staticmethod
    def _row_to_review(row: Mapping[str, Any]) -> KycManualReviewRecord:
        return KycManualReviewRecord(
            review_id=row["review_id"],
            kyc_record_id=row["kyc_record_id"],
            review_state=row["review_state"],
            review_reason=row["review_reason"],
            approval_request_id=row["approval_request_id"],
            assigned_to_operator_id=row["assigned_to_operator_id"],
            reviewer_notes=row["reviewer_notes"],
            decision=row["decision"],
            rejection_reason_code=row["rejection_reason_code"],
            decided_at=row["decided_at"],
            decided_by_operator_id=row["decided_by_operator_id"],
            created_at=row["created_at"],
            schema_version=row["schema_version"],
        )

    def insert_review(
        self, record: KycManualReviewRecord, *, conn: Any | None = None
    ) -> None:
        self._execute(
            conn,
            f"INSERT INTO kyc_manual_reviews ({_REVIEW_COLS}) VALUES "
            "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                record.review_id,
                record.kyc_record_id,
                record.review_state,
                record.review_reason,
                record.approval_request_id,
                record.assigned_to_operator_id,
                record.reviewer_notes,
                record.decision,
                record.rejection_reason_code,
                record.decided_at,
                record.decided_by_operator_id,
                record.created_at,
                record.schema_version,
            ),
        )

    def update_review(
        self, record: KycManualReviewRecord, *, conn: Any | None = None
    ) -> None:
        self._execute(
            conn,
            "UPDATE kyc_manual_reviews SET review_state = %s, "
            "assigned_to_operator_id = %s, reviewer_notes = %s, decision = %s, "
            "rejection_reason_code = %s, decided_at = %s, decided_by_operator_id = %s "
            "WHERE review_id = %s",
            (
                record.review_state,
                record.assigned_to_operator_id,
                record.reviewer_notes,
                record.decision,
                record.rejection_reason_code,
                record.decided_at,
                record.decided_by_operator_id,
                record.review_id,
            ),
        )

    def find_open_review(
        self, kyc_record_id: str, *, conn: Any | None = None
    ) -> KycManualReviewRecord | None:
        row = self._fetchone(
            conn,
            f"SELECT {_REVIEW_COLS} FROM kyc_manual_reviews "
            "WHERE kyc_record_id = %s AND review_state <> 'REVIEW_CLOSED' "
            "ORDER BY created_at LIMIT 1",
            (kyc_record_id,),
        )
        return None if row is None else self._row_to_review(row)

    def count_open_reviews(self, review_reason: str, *, conn: Any | None = None) -> int:
        """Open manual reviews for ``review_reason`` (spec/16 §I; E-S16-09)."""
        row = self._fetchone(
            conn,
            "SELECT count(*) AS n FROM kyc_manual_reviews "
            "WHERE review_reason = %s AND review_state <> 'REVIEW_CLOSED'",
            (review_reason,),
        )
        return 0 if row is None else int(row["n"])

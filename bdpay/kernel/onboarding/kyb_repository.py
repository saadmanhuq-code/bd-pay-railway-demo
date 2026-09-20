"""KYB storage — repository protocol, in-memory and Postgres stores.

Postgres statements match ``db/migrations/0035..0044`` exactly; the
in-memory store is the deterministic unit-test double. Both implement
:class:`KybStore`.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Iterator, Mapping
from typing import Any, Protocol, runtime_checkable

from bdpay.kernel.onboarding.kyb_models import (
    ApiKeyIssuanceRecord,
    KybDocumentRecord,
    KybRecord,
    MerchantRecord,
    UboEdgeRecord,
    UboNodeRecord,
)
from bdpay.platform.errors import ConflictError, NotFoundError

__all__ = ["InMemoryKybStore", "KybStore", "PostgresKybStore"]


@runtime_checkable
class KybStore(Protocol):
    """Storage contract for merchants + KYB aggregates."""

    def insert_merchant(self, record: MerchantRecord, *, conn: Any | None = None) -> None: ...

    def get_merchant(
        self, merchant_id: str, *, conn: Any | None = None
    ) -> MerchantRecord | None: ...

    def update_merchant(self, record: MerchantRecord, *, conn: Any | None = None) -> None: ...

    def list_merchants(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        kyb_status: str | None = None,
        conn: Any | None = None,
    ) -> list[MerchantRecord]: ...

    def insert_record(self, record: KybRecord, *, conn: Any | None = None) -> None: ...

    def get_record(self, kyb_record_id: str, *, conn: Any | None = None) -> KybRecord | None: ...

    def update_record(self, record: KybRecord, *, conn: Any | None = None) -> None: ...

    def list_records(
        self, *, status: str | None = None, conn: Any | None = None
    ) -> list[KybRecord]: ...

    def insert_document(
        self, record: KybDocumentRecord, *, conn: Any | None = None
    ) -> None: ...

    def update_document(
        self, record: KybDocumentRecord, *, conn: Any | None = None
    ) -> None: ...

    def list_documents(
        self, kyb_record_id: str, *, conn: Any | None = None
    ) -> list[KybDocumentRecord]: ...

    def insert_ubo_node(self, record: UboNodeRecord, *, conn: Any | None = None) -> None: ...

    def update_ubo_node(self, record: UboNodeRecord, *, conn: Any | None = None) -> None: ...

    def get_ubo_node(
        self, ubo_node_id: str, *, conn: Any | None = None
    ) -> UboNodeRecord | None: ...

    def list_ubo_nodes(
        self, kyb_record_id: str, *, conn: Any | None = None
    ) -> list[UboNodeRecord]: ...

    def insert_ubo_edge(self, record: UboEdgeRecord, *, conn: Any | None = None) -> None: ...

    def list_ubo_edges(
        self, kyb_record_id: str, *, conn: Any | None = None
    ) -> list[UboEdgeRecord]: ...

    def insert_api_key_issuance(
        self, record: ApiKeyIssuanceRecord, *, conn: Any | None = None
    ) -> None: ...

    def list_api_key_issuances(
        self, merchant_id: str, *, conn: Any | None = None
    ) -> list[ApiKeyIssuanceRecord]: ...


class InMemoryKybStore:
    """Deterministic in-memory store for unit tests."""

    def __init__(self) -> None:
        self._merchants: dict[str, MerchantRecord] = {}
        self._records: dict[str, KybRecord] = {}
        self._documents: dict[str, KybDocumentRecord] = {}
        self._nodes: dict[str, UboNodeRecord] = {}
        self._edges: dict[str, UboEdgeRecord] = {}
        self._api_keys: dict[str, ApiKeyIssuanceRecord] = {}

    def insert_merchant(self, record: MerchantRecord, *, conn: Any | None = None) -> None:
        if record.merchant_id in self._merchants:
            raise ConflictError(f"merchant {record.merchant_id} exists")
        self._merchants[record.merchant_id] = record

    def get_merchant(
        self, merchant_id: str, *, conn: Any | None = None
    ) -> MerchantRecord | None:
        return self._merchants.get(merchant_id)

    def update_merchant(self, record: MerchantRecord, *, conn: Any | None = None) -> None:
        if record.merchant_id not in self._merchants:
            raise NotFoundError(f"unknown merchant {record.merchant_id}")
        self._merchants[record.merchant_id] = record

    def list_merchants(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        kyb_status: str | None = None,
        conn: Any | None = None,
    ) -> list[MerchantRecord]:
        rows = [
            m
            for m in self._merchants.values()
            if kyb_status is None or m.kyb_status == kyb_status
        ]
        # Match Postgres ORDER BY created_at, merchant_id for deterministic tiebreaking.
        rows.sort(key=lambda m: (m.created_at, m.merchant_id))
        return rows[offset : offset + limit]

    def insert_record(self, record: KybRecord, *, conn: Any | None = None) -> None:
        if record.kyb_record_id in self._records:
            raise ConflictError(f"kyb record {record.kyb_record_id} exists")
        for existing in self._records.values():
            if (
                existing.subject_type == record.subject_type
                and existing.subject_id == record.subject_id
            ):
                raise ConflictError(
                    "one KYB record per subject", code="duplicate_kyb_record"
                )
        self._records[record.kyb_record_id] = record

    def get_record(self, kyb_record_id: str, *, conn: Any | None = None) -> KybRecord | None:
        return self._records.get(kyb_record_id)

    def update_record(self, record: KybRecord, *, conn: Any | None = None) -> None:
        if record.kyb_record_id not in self._records:
            raise NotFoundError(f"unknown kyb record {record.kyb_record_id}")
        self._records[record.kyb_record_id] = record

    def list_records(
        self, *, status: str | None = None, conn: Any | None = None
    ) -> list[KybRecord]:
        rows = [
            r for r in self._records.values() if status is None or r.kyb_status == status
        ]
        return sorted(rows, key=lambda r: r.created_at)

    def insert_document(self, record: KybDocumentRecord, *, conn: Any | None = None) -> None:
        if record.document_id in self._documents:
            raise ConflictError(f"document {record.document_id} exists")
        self._documents[record.document_id] = record

    def update_document(self, record: KybDocumentRecord, *, conn: Any | None = None) -> None:
        if record.document_id not in self._documents:
            raise NotFoundError(f"unknown document {record.document_id}")
        self._documents[record.document_id] = record

    def list_documents(
        self, kyb_record_id: str, *, conn: Any | None = None
    ) -> list[KybDocumentRecord]:
        return sorted(
            (d for d in self._documents.values() if d.kyb_record_id == kyb_record_id),
            key=lambda d: d.uploaded_at,
        )

    def insert_ubo_node(self, record: UboNodeRecord, *, conn: Any | None = None) -> None:
        if record.ubo_node_id in self._nodes:
            raise ConflictError(f"ubo node {record.ubo_node_id} exists")
        self._nodes[record.ubo_node_id] = record

    def update_ubo_node(self, record: UboNodeRecord, *, conn: Any | None = None) -> None:
        if record.ubo_node_id not in self._nodes:
            raise NotFoundError(f"unknown ubo node {record.ubo_node_id}")
        self._nodes[record.ubo_node_id] = record

    def get_ubo_node(
        self, ubo_node_id: str, *, conn: Any | None = None
    ) -> UboNodeRecord | None:
        return self._nodes.get(ubo_node_id)

    def list_ubo_nodes(
        self, kyb_record_id: str, *, conn: Any | None = None
    ) -> list[UboNodeRecord]:
        return sorted(
            (n for n in self._nodes.values() if n.kyb_record_id == kyb_record_id),
            key=lambda n: n.created_at,
        )

    def insert_ubo_edge(self, record: UboEdgeRecord, *, conn: Any | None = None) -> None:
        if record.edge_id in self._edges:
            raise ConflictError(f"ubo edge {record.edge_id} exists")
        for existing in self._edges.values():
            if (
                existing.parent_node_id == record.parent_node_id
                and existing.child_node_id == record.child_node_id
            ):
                raise ConflictError("duplicate UBO edge")
        self._edges[record.edge_id] = record

    def list_ubo_edges(
        self, kyb_record_id: str, *, conn: Any | None = None
    ) -> list[UboEdgeRecord]:
        return sorted(
            (e for e in self._edges.values() if e.kyb_record_id == kyb_record_id),
            key=lambda e: e.created_at,
        )

    def insert_api_key_issuance(
        self, record: ApiKeyIssuanceRecord, *, conn: Any | None = None
    ) -> None:
        if record.issuance_id in self._api_keys:
            raise ConflictError(f"issuance {record.issuance_id} exists")
        self._api_keys[record.issuance_id] = record

    def list_api_key_issuances(
        self, merchant_id: str, *, conn: Any | None = None
    ) -> list[ApiKeyIssuanceRecord]:
        return sorted(
            (k for k in self._api_keys.values() if k.merchant_id == merchant_id),
            key=lambda k: k.issued_at,
        )


# ---------------------------------------------------------------------------
# Postgres implementation (migrations 0035-0044)
# ---------------------------------------------------------------------------

_MERCHANT_COLS = (
    "merchant_id, legal_name, kyb_status, kyb_record_id, risk_tier, payout_blocked, "
    "mdr_basis_points, settlement_cycle_days, activated_at, created_at, updated_at, "
    "schema_version"
)

_RECORD_COLS = (
    "kyb_record_id, subject_type, subject_id, kyb_status, risk_tier, legal_name, "
    "legal_name_bn, merchant_display_name, registration_type, tin_number, "
    "primary_business_mcc, requested_services, approved_services, contact_phone_e164, "
    "registered_address, bank_routing_number, website_url, bangla_qr_merchant_pan, "
    "bangla_qr_enabled, mdr_basis_points, settlement_cycle_days, rolling_reserve_pct, "
    "sanctions_cleared_at, sanctions_screened_at, approval_request_id, "
    "application_submitted_at, kyb_submitted_at, activated_at, rejected_at, "
    "rejection_reason_code, rejection_notes, suspended_at, suspension_reason, "
    "terminated_at, termination_reason, next_refresh_due_at, last_refreshed_at, "
    "refresh_interval_months, last_document_request_at, agreement_requested_at, "
    "created_at, updated_at, schema_version"
)

_DOC_COLS = (
    "document_id, kyb_record_id, document_type, content_hash, storage_pointer, "
    "review_status, ocr_status, ubo_node_id, reviewed_by, reviewed_at, review_notes, "
    "uploaded_at, uploaded_by, schema_version"
)

_NODE_COLS = (
    "ubo_node_id, kyb_record_id, node_type, full_name_en, full_name_bn, "
    "nid_number_hash, dob, nationality, is_ultimate_beneficial_owner, "
    "ownership_percentage_direct, role, porichoy_status, porichoy_ref, "
    "porichoy_verified_at, manual_verify_by, manual_verify_at, manual_verify_note, "
    "sanctions_status, sanctions_screened_at, sanctions_hit_id, created_at, "
    "created_by, schema_version"
)

_EDGE_COLS = (
    "edge_id, kyb_record_id, parent_node_id, child_node_id, ownership_percentage, "
    "created_at"
)

_APIK_COLS = (
    "issuance_id, merchant_id, key_id, secret_hash, issued_at, issued_by, schema_version"
)


def _jsonb(payload: Mapping[str, Any]) -> Any:
    from psycopg.types.json import Jsonb

    from bdpay.platform.canonical import canonical_json

    return Jsonb(dict(payload), dumps=lambda o: canonical_json(o).decode("utf-8"))


class PostgresKybStore:
    """psycopg3 store matching migrations 0035-0044 exactly."""

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

    @contextlib.contextmanager
    def transaction(self) -> Iterator[Any]:
        with self._connect() as owned:
            with owned.transaction():
                yield owned

    # -- merchants -------------------------------------------------------------

    @staticmethod
    def _row_to_merchant(row: Mapping[str, Any]) -> MerchantRecord:
        return MerchantRecord(
            merchant_id=row["merchant_id"],
            legal_name=row["legal_name"],
            kyb_status=row["kyb_status"],
            kyb_record_id=row["kyb_record_id"],
            risk_tier=row["risk_tier"],
            payout_blocked=row["payout_blocked"],
            mdr_basis_points=row["mdr_basis_points"],
            settlement_cycle_days=row["settlement_cycle_days"],
            activated_at=row["activated_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            schema_version=row["schema_version"],
        )

    def insert_merchant(self, record: MerchantRecord, *, conn: Any | None = None) -> None:
        self._execute(
            conn,
            f"INSERT INTO merchants ({_MERCHANT_COLS}) VALUES "
            "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                record.merchant_id,
                record.legal_name,
                record.kyb_status,
                record.kyb_record_id,
                record.risk_tier,
                record.payout_blocked,
                record.mdr_basis_points,
                record.settlement_cycle_days,
                record.activated_at,
                record.created_at,
                record.updated_at,
                record.schema_version,
            ),
        )

    def get_merchant(
        self, merchant_id: str, *, conn: Any | None = None
    ) -> MerchantRecord | None:
        row = self._fetchone(
            conn,
            f"SELECT {_MERCHANT_COLS} FROM merchants WHERE merchant_id = %s",
            (merchant_id,),
        )
        return None if row is None else self._row_to_merchant(row)

    def update_merchant(self, record: MerchantRecord, *, conn: Any | None = None) -> None:
        self._execute(
            conn,
            "UPDATE merchants SET kyb_status = %s, kyb_record_id = %s, risk_tier = %s, "
            "payout_blocked = %s, mdr_basis_points = %s, settlement_cycle_days = %s, "
            "activated_at = %s, updated_at = %s WHERE merchant_id = %s",
            (
                record.kyb_status,
                record.kyb_record_id,
                record.risk_tier,
                record.payout_blocked,
                record.mdr_basis_points,
                record.settlement_cycle_days,
                record.activated_at,
                record.updated_at,
                record.merchant_id,
            ),
        )

    def list_merchants(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        kyb_status: str | None = None,
        conn: Any | None = None,
    ) -> list[MerchantRecord]:
        if kyb_status is None:
            rows = self._fetchall(
                conn,
                f"SELECT {_MERCHANT_COLS} FROM merchants "
                "ORDER BY created_at, merchant_id "
                "LIMIT %s OFFSET %s",
                (limit, offset),
            )
        else:
            rows = self._fetchall(
                conn,
                f"SELECT {_MERCHANT_COLS} FROM merchants "
                "WHERE kyb_status = %s "
                "ORDER BY created_at, merchant_id "
                "LIMIT %s OFFSET %s",
                (kyb_status, limit, offset),
            )
        return [self._row_to_merchant(row) for row in rows]

    # -- kyb_records --------------------------------------------------------------

    @staticmethod
    def _row_to_record(row: Mapping[str, Any]) -> KybRecord:
        return KybRecord(
            kyb_record_id=row["kyb_record_id"],
            subject_type=row["subject_type"],
            subject_id=row["subject_id"],
            kyb_status=row["kyb_status"],
            risk_tier=row["risk_tier"],
            legal_name=row["legal_name"],
            legal_name_bn=row["legal_name_bn"],
            merchant_display_name=row["merchant_display_name"],
            registration_type=row["registration_type"],
            tin_number=row["tin_number"],
            primary_business_mcc=row["primary_business_mcc"],
            requested_services=tuple(row["requested_services"] or ()),
            approved_services=tuple(row["approved_services"] or ()),
            contact_phone_e164=row["contact_phone_e164"],
            registered_address=dict(row["registered_address"] or {}),
            bank_routing_number=row["bank_routing_number"],
            website_url=row["website_url"],
            bangla_qr_merchant_pan=row["bangla_qr_merchant_pan"],
            bangla_qr_enabled=row["bangla_qr_enabled"],
            mdr_basis_points=row["mdr_basis_points"],
            settlement_cycle_days=row["settlement_cycle_days"],
            rolling_reserve_pct=row["rolling_reserve_pct"],
            sanctions_cleared_at=row["sanctions_cleared_at"],
            sanctions_screened_at=row["sanctions_screened_at"],
            approval_request_id=row["approval_request_id"],
            application_submitted_at=row["application_submitted_at"],
            kyb_submitted_at=row["kyb_submitted_at"],
            activated_at=row["activated_at"],
            rejected_at=row["rejected_at"],
            rejection_reason_code=row["rejection_reason_code"],
            rejection_notes=row["rejection_notes"],
            suspended_at=row["suspended_at"],
            suspension_reason=row["suspension_reason"],
            terminated_at=row["terminated_at"],
            termination_reason=row["termination_reason"],
            next_refresh_due_at=row["next_refresh_due_at"],
            last_refreshed_at=row["last_refreshed_at"],
            refresh_interval_months=row["refresh_interval_months"],
            last_document_request_at=row["last_document_request_at"],
            agreement_requested_at=row["agreement_requested_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            schema_version=row["schema_version"],
        )

    def insert_record(self, record: KybRecord, *, conn: Any | None = None) -> None:
        placeholders = ", ".join(["%s"] * 43)
        self._execute(
            conn,
            f"INSERT INTO kyb_records ({_RECORD_COLS}) VALUES ({placeholders})",
            (
                record.kyb_record_id,
                record.subject_type,
                record.subject_id,
                record.kyb_status,
                record.risk_tier,
                record.legal_name,
                record.legal_name_bn,
                record.merchant_display_name,
                record.registration_type,
                record.tin_number,
                record.primary_business_mcc,
                list(record.requested_services),
                list(record.approved_services),
                record.contact_phone_e164,
                _jsonb(record.registered_address),
                record.bank_routing_number,
                record.website_url,
                record.bangla_qr_merchant_pan,
                record.bangla_qr_enabled,
                record.mdr_basis_points,
                record.settlement_cycle_days,
                record.rolling_reserve_pct,
                record.sanctions_cleared_at,
                record.sanctions_screened_at,
                record.approval_request_id,
                record.application_submitted_at,
                record.kyb_submitted_at,
                record.activated_at,
                record.rejected_at,
                record.rejection_reason_code,
                record.rejection_notes,
                record.suspended_at,
                record.suspension_reason,
                record.terminated_at,
                record.termination_reason,
                record.next_refresh_due_at,
                record.last_refreshed_at,
                record.refresh_interval_months,
                record.last_document_request_at,
                record.agreement_requested_at,
                record.created_at,
                record.updated_at,
                record.schema_version,
            ),
        )

    def get_record(self, kyb_record_id: str, *, conn: Any | None = None) -> KybRecord | None:
        row = self._fetchone(
            conn,
            f"SELECT {_RECORD_COLS} FROM kyb_records WHERE kyb_record_id = %s",
            (kyb_record_id,),
        )
        return None if row is None else self._row_to_record(row)

    def update_record(self, record: KybRecord, *, conn: Any | None = None) -> None:
        self._execute(
            conn,
            "UPDATE kyb_records SET kyb_status = %s, risk_tier = %s, "
            "legal_name_bn = %s, merchant_display_name = %s, registered_address = %s, "
            "approved_services = %s, bangla_qr_merchant_pan = %s, bangla_qr_enabled = %s, "
            "mdr_basis_points = %s, settlement_cycle_days = %s, rolling_reserve_pct = %s, "
            "sanctions_cleared_at = %s, "
            "sanctions_screened_at = %s, approval_request_id = %s, kyb_submitted_at = %s, "
            "activated_at = %s, rejected_at = %s, rejection_reason_code = %s, "
            "rejection_notes = %s, suspended_at = %s, suspension_reason = %s, "
            "terminated_at = %s, termination_reason = %s, next_refresh_due_at = %s, "
            "last_refreshed_at = %s, refresh_interval_months = %s, "
            "last_document_request_at = %s, agreement_requested_at = %s, updated_at = %s "
            "WHERE kyb_record_id = %s",
            (
                record.kyb_status,
                record.risk_tier,
                record.legal_name_bn,
                record.merchant_display_name,
                _jsonb(record.registered_address),
                list(record.approved_services),
                record.bangla_qr_merchant_pan,
                record.bangla_qr_enabled,
                record.mdr_basis_points,
                record.settlement_cycle_days,
                record.rolling_reserve_pct,
                record.sanctions_cleared_at,
                record.sanctions_screened_at,
                record.approval_request_id,
                record.kyb_submitted_at,
                record.activated_at,
                record.rejected_at,
                record.rejection_reason_code,
                record.rejection_notes,
                record.suspended_at,
                record.suspension_reason,
                record.terminated_at,
                record.termination_reason,
                record.next_refresh_due_at,
                record.last_refreshed_at,
                record.refresh_interval_months,
                record.last_document_request_at,
                record.agreement_requested_at,
                record.updated_at,
                record.kyb_record_id,
            ),
        )

    def list_records(
        self, *, status: str | None = None, conn: Any | None = None
    ) -> list[KybRecord]:
        if status is None:
            rows = self._fetchall(
                conn, f"SELECT {_RECORD_COLS} FROM kyb_records ORDER BY created_at", ()
            )
        else:
            rows = self._fetchall(
                conn,
                f"SELECT {_RECORD_COLS} FROM kyb_records WHERE kyb_status = %s "
                "ORDER BY created_at",
                (status,),
            )
        return [self._row_to_record(row) for row in rows]

    # -- documents ----------------------------------------------------------------

    @staticmethod
    def _row_to_document(row: Mapping[str, Any]) -> KybDocumentRecord:
        return KybDocumentRecord(
            document_id=row["document_id"],
            kyb_record_id=row["kyb_record_id"],
            document_type=row["document_type"],
            content_hash=row["content_hash"],
            storage_pointer=row["storage_pointer"],
            review_status=row["review_status"],
            ocr_status=row["ocr_status"],
            ubo_node_id=row["ubo_node_id"],
            reviewed_by=row["reviewed_by"],
            reviewed_at=row["reviewed_at"],
            review_notes=row["review_notes"],
            uploaded_at=row["uploaded_at"],
            uploaded_by=row["uploaded_by"],
            schema_version=row["schema_version"],
        )

    def insert_document(self, record: KybDocumentRecord, *, conn: Any | None = None) -> None:
        self._execute(
            conn,
            f"INSERT INTO kyb_documents ({_DOC_COLS}) VALUES "
            "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                record.document_id,
                record.kyb_record_id,
                record.document_type,
                record.content_hash,
                record.storage_pointer,
                record.review_status,
                record.ocr_status,
                record.ubo_node_id,
                record.reviewed_by,
                record.reviewed_at,
                record.review_notes,
                record.uploaded_at,
                record.uploaded_by,
                record.schema_version,
            ),
        )

    def update_document(self, record: KybDocumentRecord, *, conn: Any | None = None) -> None:
        self._execute(
            conn,
            "UPDATE kyb_documents SET review_status = %s, ocr_status = %s, "
            "reviewed_by = %s, reviewed_at = %s, review_notes = %s WHERE document_id = %s",
            (
                record.review_status,
                record.ocr_status,
                record.reviewed_by,
                record.reviewed_at,
                record.review_notes,
                record.document_id,
            ),
        )

    def list_documents(
        self, kyb_record_id: str, *, conn: Any | None = None
    ) -> list[KybDocumentRecord]:
        rows = self._fetchall(
            conn,
            f"SELECT {_DOC_COLS} FROM kyb_documents WHERE kyb_record_id = %s "
            "ORDER BY uploaded_at",
            (kyb_record_id,),
        )
        return [self._row_to_document(row) for row in rows]

    # -- UBO graph ----------------------------------------------------------------

    @staticmethod
    def _row_to_node(row: Mapping[str, Any]) -> UboNodeRecord:
        return UboNodeRecord(
            ubo_node_id=row["ubo_node_id"],
            kyb_record_id=row["kyb_record_id"],
            node_type=row["node_type"],
            full_name_en=row["full_name_en"],
            full_name_bn=row["full_name_bn"],
            nid_number_hash=row["nid_number_hash"],
            dob=row["dob"],
            nationality=row["nationality"],
            is_ultimate_beneficial_owner=row["is_ultimate_beneficial_owner"],
            ownership_percentage_direct=row["ownership_percentage_direct"],
            role=row["role"],
            porichoy_status=row["porichoy_status"],
            porichoy_ref=row["porichoy_ref"],
            porichoy_verified_at=row["porichoy_verified_at"],
            manual_verify_by=row["manual_verify_by"],
            manual_verify_at=row["manual_verify_at"],
            manual_verify_note=row["manual_verify_note"],
            sanctions_status=row["sanctions_status"],
            sanctions_screened_at=row["sanctions_screened_at"],
            sanctions_hit_id=row["sanctions_hit_id"],
            created_at=row["created_at"],
            created_by=row["created_by"],
            schema_version=row["schema_version"],
        )

    def insert_ubo_node(self, record: UboNodeRecord, *, conn: Any | None = None) -> None:
        placeholders = ", ".join(["%s"] * 23)
        self._execute(
            conn,
            f"INSERT INTO kyb_ubo_nodes ({_NODE_COLS}) VALUES ({placeholders})",
            (
                record.ubo_node_id,
                record.kyb_record_id,
                record.node_type,
                record.full_name_en,
                record.full_name_bn,
                record.nid_number_hash,
                record.dob,
                record.nationality,
                record.is_ultimate_beneficial_owner,
                record.ownership_percentage_direct,
                record.role,
                record.porichoy_status,
                record.porichoy_ref,
                record.porichoy_verified_at,
                record.manual_verify_by,
                record.manual_verify_at,
                record.manual_verify_note,
                record.sanctions_status,
                record.sanctions_screened_at,
                record.sanctions_hit_id,
                record.created_at,
                record.created_by,
                record.schema_version,
            ),
        )

    def update_ubo_node(self, record: UboNodeRecord, *, conn: Any | None = None) -> None:
        self._execute(
            conn,
            "UPDATE kyb_ubo_nodes SET porichoy_status = %s, porichoy_ref = %s, "
            "porichoy_verified_at = %s, manual_verify_by = %s, manual_verify_at = %s, "
            "manual_verify_note = %s, sanctions_status = %s, sanctions_screened_at = %s, "
            "sanctions_hit_id = %s WHERE ubo_node_id = %s",
            (
                record.porichoy_status,
                record.porichoy_ref,
                record.porichoy_verified_at,
                record.manual_verify_by,
                record.manual_verify_at,
                record.manual_verify_note,
                record.sanctions_status,
                record.sanctions_screened_at,
                record.sanctions_hit_id,
                record.ubo_node_id,
            ),
        )

    def get_ubo_node(
        self, ubo_node_id: str, *, conn: Any | None = None
    ) -> UboNodeRecord | None:
        row = self._fetchone(
            conn,
            f"SELECT {_NODE_COLS} FROM kyb_ubo_nodes WHERE ubo_node_id = %s",
            (ubo_node_id,),
        )
        return None if row is None else self._row_to_node(row)

    def list_ubo_nodes(
        self, kyb_record_id: str, *, conn: Any | None = None
    ) -> list[UboNodeRecord]:
        rows = self._fetchall(
            conn,
            f"SELECT {_NODE_COLS} FROM kyb_ubo_nodes WHERE kyb_record_id = %s "
            "ORDER BY created_at",
            (kyb_record_id,),
        )
        return [self._row_to_node(row) for row in rows]

    def insert_ubo_edge(self, record: UboEdgeRecord, *, conn: Any | None = None) -> None:
        self._execute(
            conn,
            f"INSERT INTO kyb_ubo_edges ({_EDGE_COLS}) VALUES (%s, %s, %s, %s, %s, %s)",
            (
                record.edge_id,
                record.kyb_record_id,
                record.parent_node_id,
                record.child_node_id,
                record.ownership_percentage,
                record.created_at,
            ),
        )

    def list_ubo_edges(
        self, kyb_record_id: str, *, conn: Any | None = None
    ) -> list[UboEdgeRecord]:
        rows = self._fetchall(
            conn,
            f"SELECT {_EDGE_COLS} FROM kyb_ubo_edges WHERE kyb_record_id = %s "
            "ORDER BY created_at",
            (kyb_record_id,),
        )
        return [
            UboEdgeRecord(
                edge_id=row["edge_id"],
                kyb_record_id=row["kyb_record_id"],
                parent_node_id=row["parent_node_id"],
                child_node_id=row["child_node_id"],
                ownership_percentage=row["ownership_percentage"],
                created_at=row["created_at"],
            )
            for row in rows
        ]

    # -- API key issuances ----------------------------------------------------------

    def insert_api_key_issuance(
        self, record: ApiKeyIssuanceRecord, *, conn: Any | None = None
    ) -> None:
        self._execute(
            conn,
            f"INSERT INTO kyb_api_key_issuances ({_APIK_COLS}) VALUES "
            "(%s, %s, %s, %s, %s, %s, %s)",
            (
                record.issuance_id,
                record.merchant_id,
                record.key_id,
                record.secret_hash,
                record.issued_at,
                record.issued_by,
                record.schema_version,
            ),
        )

    def list_api_key_issuances(
        self, merchant_id: str, *, conn: Any | None = None
    ) -> list[ApiKeyIssuanceRecord]:
        rows = self._fetchall(
            conn,
            f"SELECT {_APIK_COLS} FROM kyb_api_key_issuances WHERE merchant_id = %s "
            "ORDER BY issued_at",
            (merchant_id,),
        )
        return [
            ApiKeyIssuanceRecord(
                issuance_id=row["issuance_id"],
                merchant_id=row["merchant_id"],
                key_id=row["key_id"],
                secret_hash=row["secret_hash"],
                issued_at=row["issued_at"],
                issued_by=row["issued_by"],
                schema_version=row["schema_version"],
            )
            for row in rows
        ]

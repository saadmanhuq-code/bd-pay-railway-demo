"""Participant onboarding storage — protocol, in-memory and Postgres stores.

Postgres statements match ``db/migrations/0041 + 0042 + 0100 + 0101``
exactly; the in-memory store is the deterministic unit-test double. Both
implement :class:`ParticipantStore`. Conformance-run status mutations go
through the migration-0101 ``conformance_fsm_advance`` function (refusal
first at the database layer too).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any, Protocol, runtime_checkable

from bdpay.kernel.participants.models import (
    ConformanceRunRecord,
    ParticipantDocumentRecord,
    ParticipantMouRecord,
    ParticipantRecord,
)
from bdpay.platform.errors import ConflictError, NotFoundError

__all__ = [
    "InMemoryParticipantStore",
    "ParticipantStore",
    "PostgresParticipantStore",
]


@runtime_checkable
class ParticipantStore(Protocol):
    """Storage contract for participants + PSO-1 aggregates."""

    # -- participants ------------------------------------------------------

    def insert_participant(
        self, record: ParticipantRecord, *, conn: Any | None = None
    ) -> None: ...

    def get_participant(
        self, participant_id: str, *, conn: Any | None = None
    ) -> ParticipantRecord | None: ...

    def update_participant(
        self, record: ParticipantRecord, *, conn: Any | None = None
    ) -> None: ...

    def list_participants(
        self, *, status: str | None = None, conn: Any | None = None
    ) -> list[ParticipantRecord]: ...

    # -- documents ---------------------------------------------------------

    def insert_document(
        self, record: ParticipantDocumentRecord, *, conn: Any | None = None
    ) -> None: ...

    def update_document(
        self, record: ParticipantDocumentRecord, *, conn: Any | None = None
    ) -> None: ...

    def get_document(
        self, document_id: str, *, conn: Any | None = None
    ) -> ParticipantDocumentRecord | None: ...

    def list_documents(
        self, participant_id: str, *, conn: Any | None = None
    ) -> list[ParticipantDocumentRecord]: ...

    # -- license verifications (spec/08 table, written by BB_LICENSE verify) --

    def insert_license_verification(
        self, row: Mapping[str, Any], *, conn: Any | None = None
    ) -> None: ...

    def list_license_verifications(
        self, participant_id: str, *, conn: Any | None = None
    ) -> list[Mapping[str, Any]]: ...

    # -- MOUs ----------------------------------------------------------------

    def insert_mou(self, record: ParticipantMouRecord, *, conn: Any | None = None) -> None: ...

    def get_mou(self, mou_id: str, *, conn: Any | None = None) -> ParticipantMouRecord | None: ...

    # -- conformance runs ----------------------------------------------------

    def insert_conformance_run(
        self, record: ConformanceRunRecord, *, conn: Any | None = None
    ) -> None: ...

    def advance_conformance_run(
        self, record: ConformanceRunRecord, *, from_status: str, conn: Any | None = None
    ) -> None: ...

    def get_conformance_run(
        self, conformance_run_id: str, *, conn: Any | None = None
    ) -> ConformanceRunRecord | None: ...

    def list_conformance_runs(
        self, participant_id: str, *, conn: Any | None = None
    ) -> list[ConformanceRunRecord]: ...


# ---------------------------------------------------------------------------
# In-memory store (deterministic unit-test double)
# ---------------------------------------------------------------------------


class InMemoryParticipantStore:
    """Dict-backed :class:`ParticipantStore`; insertion-ordered listings."""

    def __init__(self) -> None:
        self._participants: dict[str, ParticipantRecord] = {}
        self._documents: dict[str, ParticipantDocumentRecord] = {}
        self._license_verifications: list[dict[str, Any]] = []
        self._mous: dict[str, ParticipantMouRecord] = {}
        self._runs: dict[str, ConformanceRunRecord] = {}

    # -- participants ------------------------------------------------------

    def insert_participant(
        self, record: ParticipantRecord, *, conn: Any | None = None
    ) -> None:
        if record.participant_id in self._participants:
            raise ConflictError(
                f"participant {record.participant_id} already exists",
                code="duplicate_participant",
            )
        self._participants[record.participant_id] = record

    def get_participant(
        self, participant_id: str, *, conn: Any | None = None
    ) -> ParticipantRecord | None:
        return self._participants.get(participant_id)

    def update_participant(
        self, record: ParticipantRecord, *, conn: Any | None = None
    ) -> None:
        if record.participant_id not in self._participants:
            raise NotFoundError(f"unknown participant {record.participant_id}")
        self._participants[record.participant_id] = record

    def list_participants(
        self, *, status: str | None = None, conn: Any | None = None
    ) -> list[ParticipantRecord]:
        records = list(self._participants.values())
        if status is not None:
            records = [r for r in records if r.kyb_status == status]
        return records

    # -- documents ---------------------------------------------------------

    def insert_document(
        self, record: ParticipantDocumentRecord, *, conn: Any | None = None
    ) -> None:
        if record.document_id in self._documents:
            raise ConflictError(
                f"document {record.document_id} already exists", code="duplicate_document"
            )
        live = [
            d
            for d in self._documents.values()
            if d.participant_id == record.participant_id
            and d.document_class == record.document_class
            and d.review_status in ("PENDING", "VERIFIED")
        ]
        if record.review_status in ("PENDING", "VERIFIED") and live:
            # Mirrors uidx_pdoc_live_class: one live document per class.
            raise ConflictError(
                f"a live {record.document_class} document already exists for "
                f"{record.participant_id}",
                code="document_class_live_conflict",
            )
        self._documents[record.document_id] = record

    def update_document(
        self, record: ParticipantDocumentRecord, *, conn: Any | None = None
    ) -> None:
        if record.document_id not in self._documents:
            raise NotFoundError(f"unknown document {record.document_id}")
        self._documents[record.document_id] = record

    def get_document(
        self, document_id: str, *, conn: Any | None = None
    ) -> ParticipantDocumentRecord | None:
        return self._documents.get(document_id)

    def list_documents(
        self, participant_id: str, *, conn: Any | None = None
    ) -> list[ParticipantDocumentRecord]:
        return [d for d in self._documents.values() if d.participant_id == participant_id]

    # -- license verifications ----------------------------------------------

    def insert_license_verification(
        self, row: Mapping[str, Any], *, conn: Any | None = None
    ) -> None:
        self._license_verifications.append(dict(row))

    def list_license_verifications(
        self, participant_id: str, *, conn: Any | None = None
    ) -> list[Mapping[str, Any]]:
        return [
            dict(r)
            for r in self._license_verifications
            if r["participant_id"] == participant_id
        ]

    # -- MOUs ----------------------------------------------------------------

    def insert_mou(self, record: ParticipantMouRecord, *, conn: Any | None = None) -> None:
        if record.mou_id in self._mous:
            raise ConflictError(f"mou {record.mou_id} already exists", code="duplicate_mou")
        for existing in self._mous.values():
            if (
                existing.participant_id == record.participant_id
                and existing.mou_template_version == record.mou_template_version
            ):
                raise ConflictError(
                    "an MOU for this participant + template version already exists",
                    code="duplicate_mou",
                )
        self._mous[record.mou_id] = record

    def get_mou(self, mou_id: str, *, conn: Any | None = None) -> ParticipantMouRecord | None:
        return self._mous.get(mou_id)

    # -- conformance runs ----------------------------------------------------

    def insert_conformance_run(
        self, record: ConformanceRunRecord, *, conn: Any | None = None
    ) -> None:
        if record.conformance_run_id in self._runs:
            raise ConflictError(
                f"conformance run {record.conformance_run_id} already exists",
                code="duplicate_conformance_run",
            )
        self._runs[record.conformance_run_id] = record

    def advance_conformance_run(
        self, record: ConformanceRunRecord, *, from_status: str, conn: Any | None = None
    ) -> None:
        existing = self._runs.get(record.conformance_run_id)
        if existing is None:
            raise NotFoundError(f"unknown conformance run {record.conformance_run_id}")
        if existing.status != from_status:
            raise ConflictError(
                f"run {record.conformance_run_id} is not in state {from_status} "
                "(stale transition refused)",
                code="invalid_state_transition",
            )
        self._runs[record.conformance_run_id] = record

    def get_conformance_run(
        self, conformance_run_id: str, *, conn: Any | None = None
    ) -> ConformanceRunRecord | None:
        return self._runs.get(conformance_run_id)

    def list_conformance_runs(
        self, participant_id: str, *, conn: Any | None = None
    ) -> list[ConformanceRunRecord]:
        return [r for r in self._runs.values() if r.participant_id == participant_id]


# ---------------------------------------------------------------------------
# Postgres store (psycopg3; matches the migration DDL exactly)
# ---------------------------------------------------------------------------

_PARTICIPANT_COLS = (
    "participant_id, institution_name, institution_type, kyb_status, "
    "bb_license_number, bb_license_type, bb_license_expiry, net_debit_cap_id, "
    "activated_at, mou_id, conformance_passed_at, suspended_reason, "
    "created_at, updated_at, schema_version"
)

_PDOC_COLS = (
    "document_id, participant_id, document_class, storage_pointer, "
    "content_sha256, metadata, review_status, reject_reason, uploaded_by, "
    "verified_by, uploaded_at, verified_at, schema_version"
)

_PMOU_COLS = (
    "mou_id, participant_id, mou_template_version, executed_date, "
    "signed_document_pointer, signed_document_sha256, counterparty_signatory, "
    "non_binding, contingent_on_license, zero_capital_commitment, "
    "founding_fee_terms_ref, recorded_by, recorded_at, schema_version"
)

_CONFR_COLS = (
    "conformance_run_id, participant_id, direction, suite_version, status, "
    "checks, evidence_pointer, evidence_sha256, report_pointer, report_hash, "
    "triggered_by, started_at, finished_at, created_at, schema_version"
)

_PLV_COLS = (
    "verification_id, participant_id, bb_license_number, bb_license_type, "
    "bb_license_expiry, verification_method, verified_by, verified_at, "
    "verification_notes, document_pointer, schema_version, created_at"
)


class PostgresParticipantStore:
    """psycopg3 store matching migrations 0041/0042/0100/0101 exactly."""

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

    @staticmethod
    def _jsonb(payload: Any) -> Any:
        from psycopg.types.json import Jsonb

        return Jsonb(payload)

    # -- participants ------------------------------------------------------

    @staticmethod
    def _row_to_participant(row: Mapping[str, Any]) -> ParticipantRecord:
        return ParticipantRecord(
            participant_id=row["participant_id"],
            institution_name=row["institution_name"],
            institution_type=row["institution_type"],
            kyb_status=row["kyb_status"],
            bb_license_number=row["bb_license_number"],
            bb_license_type=row["bb_license_type"],
            bb_license_expiry=row["bb_license_expiry"],
            net_debit_cap_id=row["net_debit_cap_id"],
            activated_at=row["activated_at"],
            mou_id=row["mou_id"],
            conformance_passed_at=row["conformance_passed_at"],
            suspended_reason=row["suspended_reason"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            schema_version=row["schema_version"],
        )

    def insert_participant(
        self, record: ParticipantRecord, *, conn: Any | None = None
    ) -> None:
        self._execute(
            conn,
            f"INSERT INTO participants ({_PARTICIPANT_COLS}) VALUES "
            "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                record.participant_id,
                record.institution_name,
                record.institution_type,
                record.kyb_status,
                record.bb_license_number,
                record.bb_license_type,
                record.bb_license_expiry,
                record.net_debit_cap_id,
                record.activated_at,
                record.mou_id,
                record.conformance_passed_at,
                record.suspended_reason,
                record.created_at,
                record.updated_at,
                record.schema_version,
            ),
        )

    def get_participant(
        self, participant_id: str, *, conn: Any | None = None
    ) -> ParticipantRecord | None:
        row = self._fetchone(
            conn,
            f"SELECT {_PARTICIPANT_COLS} FROM participants WHERE participant_id = %s",
            (participant_id,),
        )
        return None if row is None else self._row_to_participant(row)

    def update_participant(
        self, record: ParticipantRecord, *, conn: Any | None = None
    ) -> None:
        self._execute(
            conn,
            "UPDATE participants SET kyb_status = %s, bb_license_number = %s, "
            "bb_license_type = %s, bb_license_expiry = %s, net_debit_cap_id = %s, "
            "activated_at = %s, mou_id = %s, conformance_passed_at = %s, "
            "suspended_reason = %s, updated_at = %s WHERE participant_id = %s",
            (
                record.kyb_status,
                record.bb_license_number,
                record.bb_license_type,
                record.bb_license_expiry,
                record.net_debit_cap_id,
                record.activated_at,
                record.mou_id,
                record.conformance_passed_at,
                record.suspended_reason,
                record.updated_at,
                record.participant_id,
            ),
        )

    def list_participants(
        self, *, status: str | None = None, conn: Any | None = None
    ) -> list[ParticipantRecord]:
        if status is None:
            rows = self._fetchall(
                conn,
                f"SELECT {_PARTICIPANT_COLS} FROM participants ORDER BY created_at",
                (),
            )
        else:
            rows = self._fetchall(
                conn,
                f"SELECT {_PARTICIPANT_COLS} FROM participants "
                "WHERE kyb_status = %s ORDER BY created_at",
                (status,),
            )
        return [self._row_to_participant(r) for r in rows]

    # -- documents ---------------------------------------------------------

    @staticmethod
    def _row_to_document(row: Mapping[str, Any]) -> ParticipantDocumentRecord:
        metadata = row["metadata"]
        if isinstance(metadata, str):
            metadata = json.loads(metadata)
        return ParticipantDocumentRecord(
            document_id=row["document_id"],
            participant_id=row["participant_id"],
            document_class=row["document_class"],
            storage_pointer=row["storage_pointer"],
            content_sha256=row["content_sha256"],
            metadata=dict(metadata),
            review_status=row["review_status"],
            reject_reason=row["reject_reason"],
            uploaded_by=row["uploaded_by"],
            verified_by=row["verified_by"],
            uploaded_at=row["uploaded_at"],
            verified_at=row["verified_at"],
            schema_version=row["schema_version"],
        )

    def insert_document(
        self, record: ParticipantDocumentRecord, *, conn: Any | None = None
    ) -> None:
        self._execute(
            conn,
            f"INSERT INTO participant_documents ({_PDOC_COLS}) VALUES "
            "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                record.document_id,
                record.participant_id,
                record.document_class,
                record.storage_pointer,
                record.content_sha256,
                self._jsonb(record.metadata),
                record.review_status,
                record.reject_reason,
                record.uploaded_by,
                record.verified_by,
                record.uploaded_at,
                record.verified_at,
                record.schema_version,
            ),
        )

    def update_document(
        self, record: ParticipantDocumentRecord, *, conn: Any | None = None
    ) -> None:
        self._execute(
            conn,
            "UPDATE participant_documents SET review_status = %s, reject_reason = %s, "
            "verified_by = %s, verified_at = %s WHERE document_id = %s",
            (
                record.review_status,
                record.reject_reason,
                record.verified_by,
                record.verified_at,
                record.document_id,
            ),
        )

    def get_document(
        self, document_id: str, *, conn: Any | None = None
    ) -> ParticipantDocumentRecord | None:
        row = self._fetchone(
            conn,
            f"SELECT {_PDOC_COLS} FROM participant_documents WHERE document_id = %s",
            (document_id,),
        )
        return None if row is None else self._row_to_document(row)

    def list_documents(
        self, participant_id: str, *, conn: Any | None = None
    ) -> list[ParticipantDocumentRecord]:
        rows = self._fetchall(
            conn,
            f"SELECT {_PDOC_COLS} FROM participant_documents "
            "WHERE participant_id = %s ORDER BY uploaded_at",
            (participant_id,),
        )
        return [self._row_to_document(r) for r in rows]

    # -- license verifications ----------------------------------------------

    def insert_license_verification(
        self, row: Mapping[str, Any], *, conn: Any | None = None
    ) -> None:
        self._execute(
            conn,
            f"INSERT INTO participant_license_verifications ({_PLV_COLS}) VALUES "
            "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                row["verification_id"],
                row["participant_id"],
                row["bb_license_number"],
                row["bb_license_type"],
                row["bb_license_expiry"],
                row["verification_method"],
                row["verified_by"],
                row["verified_at"],
                row.get("verification_notes"),
                row.get("document_pointer"),
                row.get("schema_version", 1),
                row["created_at"],
            ),
        )

    def list_license_verifications(
        self, participant_id: str, *, conn: Any | None = None
    ) -> list[Mapping[str, Any]]:
        return self._fetchall(
            conn,
            f"SELECT {_PLV_COLS} FROM participant_license_verifications "
            "WHERE participant_id = %s ORDER BY verified_at",
            (participant_id,),
        )

    # -- MOUs ----------------------------------------------------------------

    @staticmethod
    def _row_to_mou(row: Mapping[str, Any]) -> ParticipantMouRecord:
        signatory = row["counterparty_signatory"]
        if isinstance(signatory, str):
            signatory = json.loads(signatory)
        return ParticipantMouRecord(
            mou_id=row["mou_id"],
            participant_id=row["participant_id"],
            mou_template_version=row["mou_template_version"],
            executed_date=row["executed_date"],
            signed_document_pointer=row["signed_document_pointer"],
            signed_document_sha256=row["signed_document_sha256"],
            counterparty_signatory=dict(signatory),
            non_binding=row["non_binding"],
            contingent_on_license=row["contingent_on_license"],
            zero_capital_commitment=row["zero_capital_commitment"],
            founding_fee_terms_ref=row["founding_fee_terms_ref"],
            recorded_by=row["recorded_by"],
            recorded_at=row["recorded_at"],
            schema_version=row["schema_version"],
        )

    def insert_mou(self, record: ParticipantMouRecord, *, conn: Any | None = None) -> None:
        self._execute(
            conn,
            f"INSERT INTO participant_mous ({_PMOU_COLS}) VALUES "
            "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                record.mou_id,
                record.participant_id,
                record.mou_template_version,
                record.executed_date,
                record.signed_document_pointer,
                record.signed_document_sha256,
                self._jsonb(record.counterparty_signatory),
                record.non_binding,
                record.contingent_on_license,
                record.zero_capital_commitment,
                record.founding_fee_terms_ref,
                record.recorded_by,
                record.recorded_at,
                record.schema_version,
            ),
        )

    def get_mou(self, mou_id: str, *, conn: Any | None = None) -> ParticipantMouRecord | None:
        row = self._fetchone(
            conn,
            f"SELECT {_PMOU_COLS} FROM participant_mous WHERE mou_id = %s",
            (mou_id,),
        )
        return None if row is None else self._row_to_mou(row)

    # -- conformance runs ----------------------------------------------------

    @staticmethod
    def _row_to_run(row: Mapping[str, Any]) -> ConformanceRunRecord:
        checks = row["checks"]
        if isinstance(checks, str):
            checks = json.loads(checks)
        return ConformanceRunRecord(
            conformance_run_id=row["conformance_run_id"],
            participant_id=row["participant_id"],
            direction=row["direction"],
            suite_version=row["suite_version"],
            status=row["status"],
            checks=tuple(checks),
            evidence_pointer=row["evidence_pointer"],
            evidence_sha256=row["evidence_sha256"],
            report_pointer=row["report_pointer"],
            report_hash=row["report_hash"],
            triggered_by=row["triggered_by"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            created_at=row["created_at"],
            schema_version=row["schema_version"],
        )

    def insert_conformance_run(
        self, record: ConformanceRunRecord, *, conn: Any | None = None
    ) -> None:
        self._execute(
            conn,
            f"INSERT INTO conformance_runs ({_CONFR_COLS}) VALUES "
            "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                record.conformance_run_id,
                record.participant_id,
                record.direction,
                record.suite_version,
                record.status,
                self._jsonb(list(record.checks)),
                record.evidence_pointer,
                record.evidence_sha256,
                record.report_pointer,
                record.report_hash,
                record.triggered_by,
                record.started_at,
                record.finished_at,
                record.created_at,
                record.schema_version,
            ),
        )

    def advance_conformance_run(
        self, record: ConformanceRunRecord, *, from_status: str, conn: Any | None = None
    ) -> None:
        # The 0101 FSM function is the only sanctioned mutation path; it
        # refuses undeclared transitions and stale from-states server-side.
        self._execute(
            conn,
            "SELECT conformance_fsm_advance(%s, %s, %s, %s, %s, %s, %s, %s)",
            (
                record.conformance_run_id,
                from_status,
                record.status,
                self._jsonb(list(record.checks)),
                record.report_pointer,
                record.report_hash,
                record.started_at,
                record.finished_at,
            ),
        )

    def get_conformance_run(
        self, conformance_run_id: str, *, conn: Any | None = None
    ) -> ConformanceRunRecord | None:
        row = self._fetchone(
            conn,
            f"SELECT {_CONFR_COLS} FROM conformance_runs WHERE conformance_run_id = %s",
            (conformance_run_id,),
        )
        return None if row is None else self._row_to_run(row)

    def list_conformance_runs(
        self, participant_id: str, *, conn: Any | None = None
    ) -> list[ConformanceRunRecord]:
        rows = self._fetchall(
            conn,
            f"SELECT {_CONFR_COLS} FROM conformance_runs "
            "WHERE participant_id = %s ORDER BY created_at",
            (participant_id,),
        )
        return [self._row_to_run(r) for r in rows]

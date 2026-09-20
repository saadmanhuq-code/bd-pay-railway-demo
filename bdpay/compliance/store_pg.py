"""Postgres (psycopg3) repositories matching db/migrations 0070-0078 exactly.

Unit tests run on the in-memory stores; these implementations are exercised
by the ``@pytest.mark.integration`` suite (``BDPAY_TEST_PG_DSN``). Each store
takes a ``connection_factory`` returning a new psycopg connection, mirroring
:class:`bdpay.platform.outbox.PostgresOutboxStore`.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import date
from typing import Any

from bdpay.compliance.alerts import AmlAlert
from bdpay.compliance.ctr import CtrAggregation
from bdpay.compliance.monitor import MonitoringRuleEvent
from bdpay.compliance.packs import RulePack
from bdpay.compliance.pep import PepRecord
from bdpay.compliance.sanctions.screening import (
    SanctionsHit,
    SanctionsListEntry,
    SanctionsListVersion,
    ScreeningRecord,
    WhitelistRow,
)
from bdpay.compliance.signing import SigningKeyRecord
from bdpay.compliance.str_workflow import StrReport
from bdpay.platform.canonical import canonical_json
from bdpay.platform.errors import ConflictError

__all__ = [
    "PostgresAlertStore",
    "PostgresCtrStore",
    "PostgresMonitoringEventStore",
    "PostgresPepStore",
    "PostgresRulePackStore",
    "PostgresSanctionsStore",
    "PostgresSigningKeyStore",
    "PostgresStrStore",
]

ConnectionFactory = Callable[[], Any]


def _jsonb(payload: Mapping[str, object]) -> Any:
    from psycopg.types.json import Jsonb

    return Jsonb(dict(payload), dumps=lambda o: canonical_json(o).decode("utf-8"))


def _fetchall(factory: ConnectionFactory, sql: str, params: tuple | dict) -> list[dict]:
    from psycopg.rows import dict_row

    with factory() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, params)
            return cur.fetchall()


def _fetchone(factory: ConnectionFactory, sql: str, params: tuple | dict) -> dict | None:
    rows = _fetchall(factory, sql, params)
    return rows[0] if rows else None


def _execute(factory: ConnectionFactory, sql: str, params: tuple | dict) -> None:
    with factory() as conn:
        conn.execute(sql, params)
        conn.commit()


# ---------------------------------------------------------------------------
# Signing key registry
# ---------------------------------------------------------------------------


class PostgresSigningKeyStore:
    """signing_key_registry (migration 0070)."""

    def __init__(self, connection_factory: ConnectionFactory) -> None:
        self._connect = connection_factory

    @staticmethod
    def _to_record(row: dict) -> SigningKeyRecord:
        return SigningKeyRecord(
            key_id=row["key_id"],
            signer_id=row["signer_id"],
            algorithm=row["algorithm"],
            scope=row["scope"],
            status=row["status"],
            public_key_b64=row["public_key_b64"],
            registry_version=row["registry_version"],
            created_at=row["created_at"],
            record_hash=row["record_hash"],
            revoked_at=row["revoked_at"],
            revocation_reason=row["revocation_reason"],
            schema_version=row["schema_version"],
        )

    def insert(self, record: SigningKeyRecord) -> None:
        _execute(
            self._connect,
            "INSERT INTO signing_key_registry (key_id, signer_id, algorithm, scope, status, "
            "public_key_b64, registry_version, created_at, revoked_at, revocation_reason, "
            "record_hash, schema_version) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                record.key_id,
                record.signer_id,
                record.algorithm,
                record.scope,
                record.status,
                record.public_key_b64,
                record.registry_version,
                record.created_at,
                record.revoked_at,
                record.revocation_reason,
                record.record_hash,
                record.schema_version,
            ),
        )

    def save(self, record: SigningKeyRecord) -> None:
        _execute(
            self._connect,
            "UPDATE signing_key_registry SET status=%s, revoked_at=%s, "
            "revocation_reason=%s, record_hash=%s WHERE key_id=%s",
            (
                record.status,
                record.revoked_at,
                record.revocation_reason,
                record.record_hash,
                record.key_id,
            ),
        )

    def by_signer(self, signer_id: str) -> list[SigningKeyRecord]:
        rows = _fetchall(
            self._connect,
            "SELECT * FROM signing_key_registry WHERE signer_id=%s ORDER BY registry_version",
            (signer_id,),
        )
        return [self._to_record(r) for r in rows]

    def active_for_signer(self, signer_id: str) -> SigningKeyRecord | None:
        row = _fetchone(
            self._connect,
            "SELECT * FROM signing_key_registry WHERE signer_id=%s AND status='active'",
            (signer_id,),
        )
        return self._to_record(row) if row else None


# ---------------------------------------------------------------------------
# Rule packs
# ---------------------------------------------------------------------------


class PostgresRulePackStore:
    """rule_packs (migration 0070)."""

    def __init__(self, connection_factory: ConnectionFactory) -> None:
        self._connect = connection_factory

    @staticmethod
    def _to_pack(row: dict) -> RulePack:
        return RulePack(
            pack_id=row["pack_id"],
            pack_version=row["pack_version"],
            domain=row["domain"],
            jurisdiction_scope=row["jurisdiction_scope"].strip(),
            status=row["status"],
            rule_count=row["rule_count"],
            signer_id=row["signer_id"],
            signed_pack_content_hash=row["signed_pack_content_hash"],
            signature_hex=row["signature_hex"],
            pack_manifest_hash=row["pack_manifest_hash"],
            rules_json=row["rules_json"],
            activated_at=row["activated_at"],
            superseded_at=row["superseded_at"],
            activation_notes=row["activation_notes"],
            activated_by=row["activated_by"],
            schema_version=row["schema_version"],
        )

    def insert(self, pack: RulePack) -> None:
        _execute(
            self._connect,
            "INSERT INTO rule_packs (pack_id, pack_version, domain, jurisdiction_scope, "
            "status, rule_count, signer_id, signed_pack_content_hash, signature_hex, "
            "pack_manifest_hash, rules_json, activated_at, superseded_at, activation_notes, "
            "activated_by, schema_version) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                pack.pack_id,
                pack.pack_version,
                pack.domain,
                pack.jurisdiction_scope,
                pack.status,
                pack.rule_count,
                pack.signer_id,
                pack.signed_pack_content_hash,
                pack.signature_hex,
                pack.pack_manifest_hash,
                _jsonb(pack.rules_json),
                pack.activated_at,
                pack.superseded_at,
                pack.activation_notes,
                pack.activated_by,
                pack.schema_version,
            ),
        )

    def save(self, pack: RulePack) -> None:
        _execute(
            self._connect,
            "UPDATE rule_packs SET status=%s, superseded_at=%s WHERE pack_id=%s",
            (pack.status, pack.superseded_at, pack.pack_id),
        )

    def get(self, pack_id: str) -> RulePack | None:
        row = _fetchone(
            self._connect, "SELECT * FROM rule_packs WHERE pack_id=%s", (pack_id,)
        )
        return self._to_pack(row) if row else None

    def active(self) -> RulePack | None:
        row = _fetchone(
            self._connect, "SELECT * FROM rule_packs WHERE status='ACTIVE'", ()
        )
        return self._to_pack(row) if row else None

    def list_all(self) -> list[RulePack]:
        rows = _fetchall(
            self._connect, "SELECT * FROM rule_packs ORDER BY activated_at", ()
        )
        return [self._to_pack(r) for r in rows]


# ---------------------------------------------------------------------------
# AML alerts
# ---------------------------------------------------------------------------


class PostgresAlertStore:
    """aml_alerts (migration 0071)."""

    def __init__(self, connection_factory: ConnectionFactory) -> None:
        self._connect = connection_factory

    @staticmethod
    def _to_alert(row: dict) -> AmlAlert:
        return AmlAlert(
            alert_id=row["alert_id"],
            subject_type=row["subject_type"],
            subject_id=row["subject_id"],
            rule_id=row["rule_id"],
            rule_pack_id=row["rule_pack_id"],
            status=row["status"],
            risk_tier=row["risk_tier"],
            raised_at=row["raised_at"],
            idempotency_key=row["idempotency_key"],
            triggered_amount_minor=row["triggered_amount_minor"],
            currency=row["currency"].strip(),
            contributing_payment_ids=tuple(row["contributing_payment_ids"]),
            notes=row["notes"],
            assigned_to=row["assigned_to"],
            triaged_at=row["triaged_at"],
            case_opened_at=row["case_opened_at"],
            str_report_id=row["str_report_id"],
            closed_at=row["closed_at"],
            close_reason=row["close_reason"],
            freeze_applied=row["freeze_applied"],
            escalation_count=row["escalation_count"],
            schema_version=row["schema_version"],
        )

    def insert(self, alert: AmlAlert) -> None:
        import psycopg

        try:
            _execute(
                self._connect,
                "INSERT INTO aml_alerts (alert_id, subject_type, subject_id, rule_id, "
                "rule_pack_id, status, risk_tier, triggered_amount_minor, currency, "
                "contributing_payment_ids, notes, assigned_to, raised_at, triaged_at, "
                "case_opened_at, str_report_id, closed_at, close_reason, freeze_applied, "
                "escalation_count, idempotency_key, schema_version) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    alert.alert_id,
                    alert.subject_type,
                    alert.subject_id,
                    alert.rule_id,
                    alert.rule_pack_id,
                    alert.status,
                    alert.risk_tier,
                    alert.triggered_amount_minor,
                    alert.currency,
                    list(alert.contributing_payment_ids),
                    alert.notes,
                    alert.assigned_to,
                    alert.raised_at,
                    alert.triaged_at,
                    alert.case_opened_at,
                    alert.str_report_id,
                    alert.closed_at,
                    alert.close_reason,
                    alert.freeze_applied,
                    alert.escalation_count,
                    alert.idempotency_key,
                    alert.schema_version,
                ),
            )
        except psycopg.errors.UniqueViolation as exc:
            raise ConflictError(
                f"alert insert violated uniqueness: {alert.alert_id}", code="duplicate_alert"
            ) from exc

    def save(self, alert: AmlAlert) -> None:
        _execute(
            self._connect,
            "UPDATE aml_alerts SET status=%s, notes=%s, assigned_to=%s, triaged_at=%s, "
            "case_opened_at=%s, str_report_id=%s, closed_at=%s, close_reason=%s, "
            "freeze_applied=%s, escalation_count=%s WHERE alert_id=%s",
            (
                alert.status,
                alert.notes,
                alert.assigned_to,
                alert.triaged_at,
                alert.case_opened_at,
                alert.str_report_id,
                alert.closed_at,
                alert.close_reason,
                alert.freeze_applied,
                alert.escalation_count,
                alert.alert_id,
            ),
        )

    def get(self, alert_id: str) -> AmlAlert | None:
        row = _fetchone(
            self._connect, "SELECT * FROM aml_alerts WHERE alert_id=%s", (alert_id,)
        )
        return self._to_alert(row) if row else None

    def by_idempotency_key(self, idempotency_key: str) -> AmlAlert | None:
        row = _fetchone(
            self._connect,
            "SELECT * FROM aml_alerts WHERE idempotency_key=%s",
            (idempotency_key,),
        )
        return self._to_alert(row) if row else None

    def by_status(self, status: str) -> list[AmlAlert]:
        rows = _fetchall(
            self._connect,
            "SELECT * FROM aml_alerts WHERE status=%s ORDER BY raised_at",
            (status,),
        )
        return [self._to_alert(r) for r in rows]

    def by_subject(self, subject_type: str, subject_id: str) -> list[AmlAlert]:
        rows = _fetchall(
            self._connect,
            "SELECT * FROM aml_alerts WHERE subject_type=%s AND subject_id=%s "
            "ORDER BY raised_at",
            (subject_type, subject_id),
        )
        return [self._to_alert(r) for r in rows]


# ---------------------------------------------------------------------------
# STR reports
# ---------------------------------------------------------------------------


class PostgresStrStore:
    """str_reports (migration 0072)."""

    def __init__(self, connection_factory: ConnectionFactory) -> None:
        self._connect = connection_factory

    @staticmethod
    def _to_report(row: dict) -> StrReport:
        return StrReport(
            str_id=row["str_id"],
            alert_id=row["alert_id"],
            subject_type=row["subject_type"],
            subject_id=row["subject_id"],
            status=row["status"],
            camlco_id=row["camlco_id"],
            suspicion_narrative_hash=row["suspicion_narrative_hash"],
            suspicion_narrative_pointer=row["suspicion_narrative_pointer"],
            created_at=row["created_at"],
            camlco_approved_at=row["camlco_approved_at"],
            filing_due_at=row["filing_due_at"],
            requires_bfiu_escalation=row["requires_bfiu_escalation"],
            bfiu_manual_escalation_required=row["bfiu_manual_escalation_required"],
            goaml_payload_hash=row["goaml_payload_hash"],
            goaml_payload_pointer=row["goaml_payload_pointer"],
            goaml_ref=row["goaml_ref"],
            filing_submitted_at=row["filing_submitted_at"],
            filing_confirmed_at=row["filing_confirmed_at"],
            filing_error_detail_hash=row["filing_error_detail_hash"],
            retry_count=row["retry_count"],
            retry_scheduled_at=row["retry_scheduled_at"],
            withdrawn_at=row["withdrawn_at"],
            withdrawal_reason=row["withdrawal_reason"],
            contributing_payment_ids=tuple(row["contributing_payment_ids"]),
            schema_version=row["schema_version"],
        )

    def insert(self, report: StrReport) -> None:
        _execute(
            self._connect,
            "INSERT INTO str_reports (str_id, alert_id, subject_type, subject_id, status, "
            "camlco_id, camlco_approved_at, filing_due_at, suspicion_narrative_hash, "
            "suspicion_narrative_pointer, requires_bfiu_escalation, "
            "bfiu_manual_escalation_required, goaml_payload_hash, goaml_payload_pointer, "
            "goaml_ref, filing_submitted_at, filing_confirmed_at, filing_error_detail_hash, "
            "retry_count, retry_scheduled_at, withdrawn_at, withdrawal_reason, "
            "contributing_payment_ids, created_at, schema_version) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                report.str_id,
                report.alert_id,
                report.subject_type,
                report.subject_id,
                report.status,
                report.camlco_id,
                report.camlco_approved_at,
                report.filing_due_at,
                report.suspicion_narrative_hash,
                report.suspicion_narrative_pointer,
                report.requires_bfiu_escalation,
                report.bfiu_manual_escalation_required,
                report.goaml_payload_hash,
                report.goaml_payload_pointer,
                report.goaml_ref,
                report.filing_submitted_at,
                report.filing_confirmed_at,
                report.filing_error_detail_hash,
                report.retry_count,
                report.retry_scheduled_at,
                report.withdrawn_at,
                report.withdrawal_reason,
                list(report.contributing_payment_ids),
                report.created_at,
                report.schema_version,
            ),
        )

    def save(self, report: StrReport) -> None:
        _execute(
            self._connect,
            "UPDATE str_reports SET status=%s, camlco_approved_at=%s, filing_due_at=%s, "
            "requires_bfiu_escalation=%s, bfiu_manual_escalation_required=%s, "
            "goaml_payload_hash=%s, goaml_payload_pointer=%s, goaml_ref=%s, "
            "filing_submitted_at=%s, filing_confirmed_at=%s, filing_error_detail_hash=%s, "
            "retry_count=%s, retry_scheduled_at=%s, withdrawn_at=%s, withdrawal_reason=%s "
            "WHERE str_id=%s",
            (
                report.status,
                report.camlco_approved_at,
                report.filing_due_at,
                report.requires_bfiu_escalation,
                report.bfiu_manual_escalation_required,
                report.goaml_payload_hash,
                report.goaml_payload_pointer,
                report.goaml_ref,
                report.filing_submitted_at,
                report.filing_confirmed_at,
                report.filing_error_detail_hash,
                report.retry_count,
                report.retry_scheduled_at,
                report.withdrawn_at,
                report.withdrawal_reason,
                report.str_id,
            ),
        )

    def get(self, str_id: str) -> StrReport | None:
        row = _fetchone(self._connect, "SELECT * FROM str_reports WHERE str_id=%s", (str_id,))
        return self._to_report(row) if row else None

    def by_status(self, status: str) -> list[StrReport]:
        rows = _fetchall(
            self._connect,
            "SELECT * FROM str_reports WHERE status=%s ORDER BY created_at",
            (status,),
        )
        return [self._to_report(r) for r in rows]

    def by_alert(self, alert_id: str) -> list[StrReport]:
        rows = _fetchall(
            self._connect,
            "SELECT * FROM str_reports WHERE alert_id=%s ORDER BY created_at",
            (alert_id,),
        )
        return [self._to_report(r) for r in rows]


# ---------------------------------------------------------------------------
# CTR aggregations
# ---------------------------------------------------------------------------


class PostgresCtrStore:
    """ctr_aggregations (migration 0073). threshold_crossed is generated."""

    def __init__(self, connection_factory: ConnectionFactory) -> None:
        self._connect = connection_factory

    @staticmethod
    def _to_row(row: dict) -> CtrAggregation:
        return CtrAggregation(
            ctr_id=row["ctr_id"],
            account_id=row["account_id"],
            aggregation_date=row["aggregation_date"],
            total_cash_in_minor=row["total_cash_in_minor"],
            total_cash_out_minor=row["total_cash_out_minor"],
            threshold_minor=row["threshold_minor"],
            contributing_txn_count=row["contributing_txn_count"],
            computed_at=row["computed_at"],
            currency=row["currency"].strip(),
            ctr_filed=row["ctr_filed"],
            ctr_filed_at=row["ctr_filed_at"],
            goaml_ref=row["goaml_ref"],
            annual_batch_year=row["annual_batch_year"],
            schema_version=row["schema_version"],
        )

    def upsert(self, row: CtrAggregation) -> None:
        _execute(
            self._connect,
            "INSERT INTO ctr_aggregations (ctr_id, account_id, aggregation_date, "
            "total_cash_in_minor, total_cash_out_minor, currency, threshold_minor, "
            "contributing_txn_count, ctr_filed, ctr_filed_at, goaml_ref, annual_batch_year, "
            "computed_at, schema_version) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (account_id, aggregation_date) DO UPDATE SET "
            "total_cash_in_minor=EXCLUDED.total_cash_in_minor, "
            "total_cash_out_minor=EXCLUDED.total_cash_out_minor, "
            "contributing_txn_count=EXCLUDED.contributing_txn_count, "
            "ctr_filed=EXCLUDED.ctr_filed, ctr_filed_at=EXCLUDED.ctr_filed_at, "
            "goaml_ref=EXCLUDED.goaml_ref, annual_batch_year=EXCLUDED.annual_batch_year, "
            "computed_at=EXCLUDED.computed_at",
            (
                row.ctr_id,
                row.account_id,
                row.aggregation_date,
                row.total_cash_in_minor,
                row.total_cash_out_minor,
                row.currency,
                row.threshold_minor,
                row.contributing_txn_count,
                row.ctr_filed,
                row.ctr_filed_at,
                row.goaml_ref,
                row.annual_batch_year,
                row.computed_at,
                row.schema_version,
            ),
        )

    def get(self, account_id: str, aggregation_date: date) -> CtrAggregation | None:
        row = _fetchone(
            self._connect,
            "SELECT * FROM ctr_aggregations WHERE account_id=%s AND aggregation_date=%s",
            (account_id, aggregation_date),
        )
        return self._to_row(row) if row else None

    def get_by_id(self, ctr_id: str) -> CtrAggregation | None:
        row = _fetchone(
            self._connect, "SELECT * FROM ctr_aggregations WHERE ctr_id=%s", (ctr_id,)
        )
        return self._to_row(row) if row else None

    def crossed_unfiled(self) -> list[CtrAggregation]:
        rows = _fetchall(
            self._connect,
            "SELECT * FROM ctr_aggregations WHERE threshold_crossed AND NOT ctr_filed "
            "ORDER BY aggregation_date, account_id",
            (),
        )
        return [self._to_row(r) for r in rows]

    def filed_in_year(self, year: int) -> list[CtrAggregation]:
        rows = _fetchall(
            self._connect,
            "SELECT * FROM ctr_aggregations WHERE ctr_filed "
            "AND date_part('year', aggregation_date) = %s "
            "ORDER BY aggregation_date, account_id",
            (year,),
        )
        return [self._to_row(r) for r in rows]


# ---------------------------------------------------------------------------
# Sanctions store
# ---------------------------------------------------------------------------


class PostgresSanctionsStore:
    """sanctions_* tables (migrations 0074-0076)."""

    def __init__(self, connection_factory: ConnectionFactory) -> None:
        self._connect = connection_factory

    # -- versions -----------------------------------------------------------

    @staticmethod
    def _to_version(row: dict) -> SanctionsListVersion:
        return SanctionsListVersion(
            list_version_id=row["list_version_id"],
            list_code=row["list_code"],
            source_artifact_sha256=row["source_artifact_sha256"],
            source_artifact_pointer=row["source_artifact_pointer"],
            entry_count=row["entry_count"],
            added_count=row["added_count"],
            changed_count=row["changed_count"],
            removed_count=row["removed_count"],
            status=row["status"],
            ingested_by=row["ingested_by"],
            connector_result_id=row["connector_result_id"],
            ingested_at=row["ingested_at"],
            superseded_at=row["superseded_at"],
            schema_version=row["schema_version"],
        )

    def insert_version(self, version: SanctionsListVersion) -> None:
        _execute(
            self._connect,
            "INSERT INTO sanctions_list_versions (list_version_id, list_code, "
            "source_artifact_sha256, source_artifact_pointer, entry_count, added_count, "
            "changed_count, removed_count, status, ingested_by, connector_result_id, "
            "ingested_at, superseded_at, schema_version) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                version.list_version_id,
                version.list_code,
                version.source_artifact_sha256,
                version.source_artifact_pointer,
                version.entry_count,
                version.added_count,
                version.changed_count,
                version.removed_count,
                version.status,
                version.ingested_by,
                version.connector_result_id,
                version.ingested_at,
                version.superseded_at,
                version.schema_version,
            ),
        )

    def save_version(self, version: SanctionsListVersion) -> None:
        _execute(
            self._connect,
            "UPDATE sanctions_list_versions SET status=%s, superseded_at=%s "
            "WHERE list_version_id=%s",
            (version.status, version.superseded_at, version.list_version_id),
        )

    def version_by_artifact(
        self, list_code: str, artifact_sha256: str
    ) -> SanctionsListVersion | None:
        row = _fetchone(
            self._connect,
            "SELECT * FROM sanctions_list_versions WHERE list_code=%s "
            "AND source_artifact_sha256=%s",
            (list_code, artifact_sha256),
        )
        return self._to_version(row) if row else None

    def active_version(self, list_code: str) -> SanctionsListVersion | None:
        row = _fetchone(
            self._connect,
            "SELECT * FROM sanctions_list_versions WHERE list_code=%s AND status='ACTIVE'",
            (list_code,),
        )
        return self._to_version(row) if row else None

    def active_versions(self) -> list[SanctionsListVersion]:
        rows = _fetchall(
            self._connect,
            "SELECT * FROM sanctions_list_versions WHERE status='ACTIVE' "
            "ORDER BY list_code",
            (),
        )
        return [self._to_version(r) for r in rows]

    # -- entries ------------------------------------------------------------

    @staticmethod
    def _to_entry(row: dict) -> SanctionsListEntry:
        identifiers = {
            k: tuple(v) for k, v in (row["identifiers"] or {}).items()
        }
        return SanctionsListEntry(
            entry_id=row["entry_id"],
            list_version_id=row["list_version_id"],
            list_code=row["list_code"],
            entry_reference=row["entry_reference"],
            entry_content_hash=row["entry_content_hash"],
            entity_kind=row["entity_kind"],
            primary_name=row["primary_name"],
            primary_name_norm=row["primary_name_norm"],
            aliases=tuple(row["aliases"]),
            aliases_norm=tuple(row["aliases_norm"]),
            dob=row["dob"],
            nationalities=tuple(row["nationalities"]),
            identifiers=identifiers,
            dossier_pointer=row["dossier_pointer"],
            designated_at=row["designated_at"],
            schema_version=row["schema_version"],
        )

    def insert_entry(self, entry: SanctionsListEntry) -> None:
        _execute(
            self._connect,
            "INSERT INTO sanctions_list_entries (entry_id, list_version_id, list_code, "
            "entry_reference, entry_content_hash, entity_kind, primary_name, "
            "primary_name_norm, aliases, aliases_norm, dob, dob_year_only, nationalities, "
            "identifiers, dossier_pointer, designated_at, schema_version) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                entry.entry_id,
                entry.list_version_id,
                entry.list_code,
                entry.entry_reference,
                entry.entry_content_hash,
                entry.entity_kind,
                entry.primary_name,
                entry.primary_name_norm,
                list(entry.aliases),
                list(entry.aliases_norm),
                entry.dob,
                entry.dob.year if entry.dob else None,
                list(entry.nationalities),
                _jsonb({k: list(v) for k, v in entry.identifiers.items()}),
                entry.dossier_pointer,
                entry.designated_at,
                entry.schema_version,
            ),
        )

    def entries_for_version(self, list_version_id: str) -> list[SanctionsListEntry]:
        rows = _fetchall(
            self._connect,
            "SELECT * FROM sanctions_list_entries WHERE list_version_id=%s "
            "ORDER BY entry_id",
            (list_version_id,),
        )
        return [self._to_entry(r) for r in rows]

    # -- screenings ----------------------------------------------------------

    @staticmethod
    def _to_screening(row: dict) -> ScreeningRecord:
        return ScreeningRecord(
            screening_id=row["screening_id"],
            subject_type=row["subject_type"],
            subject_id=row["subject_id"],
            payment_intent_id=row["payment_intent_id"],
            context=row["context"],
            names_screened=tuple(row["names_screened"]),
            names_norm=tuple(row["names_norm"]),
            identifiers_hash=row["identifiers_hash"],
            list_version_ids=tuple(row["list_version_ids"]),
            algorithm_version=row["algorithm_version"],
            translit_version=row["translit_version"],
            thresholds_version=row["thresholds_version"],
            decision=row["decision"],
            top_score=row["top_score"],
            match_count=row["match_count"],
            result_hash=row["result_hash"],
            result_pointer=row["result_pointer"],
            sanctions_hit_id=row["sanctions_hit_id"],
            rescreen_run_id=row["rescreen_run_id"],
            duration_ms=row["duration_ms"],
            screened_at=row["screened_at"],
            schema_version=row["schema_version"],
        )

    def insert_screening(self, record: ScreeningRecord) -> None:
        _execute(
            self._connect,
            "INSERT INTO sanctions_screenings (screening_id, subject_type, subject_id, "
            "payment_intent_id, context, names_screened, names_norm, identifiers_hash, "
            "list_version_ids, algorithm_version, translit_version, thresholds_version, "
            "decision, top_score, match_count, result_hash, result_pointer, "
            "sanctions_hit_id, rescreen_run_id, duration_ms, screened_at, schema_version) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                record.screening_id,
                record.subject_type,
                record.subject_id,
                record.payment_intent_id,
                record.context,
                list(record.names_screened),
                list(record.names_norm),
                record.identifiers_hash,
                list(record.list_version_ids),
                record.algorithm_version,
                record.translit_version,
                record.thresholds_version,
                record.decision,
                record.top_score,
                record.match_count,
                record.result_hash,
                record.result_pointer,
                record.sanctions_hit_id,
                record.rescreen_run_id,
                record.duration_ms,
                record.screened_at,
                record.schema_version,
            ),
        )

    def get_screening(self, screening_id: str) -> ScreeningRecord | None:
        row = _fetchone(
            self._connect,
            "SELECT * FROM sanctions_screenings WHERE screening_id=%s",
            (screening_id,),
        )
        return self._to_screening(row) if row else None

    # -- hits ---------------------------------------------------------------

    @staticmethod
    def _to_hit(row: dict) -> SanctionsHit:
        return SanctionsHit(
            sanc_id=row["sanc_id"],
            screening_id=row["screening_id"],
            entry_id=row["entry_id"],
            entry_content_hash=row["entry_content_hash"],
            list_code=row["list_code"],
            list_version_id=row["list_version_id"],
            subject_type=row["subject_type"],
            subject_id=row["subject_id"],
            payment_intent_id=row["payment_intent_id"],
            status=row["status"],
            score=row["score"],
            matched_alias=row["matched_alias"],
            freeze_applied=row["freeze_applied"],
            freeze_released_at=row["freeze_released_at"],
            aml_alert_id=row["aml_alert_id"],
            str_report_id=row["str_report_id"],
            approval_request_id=row["approval_request_id"],
            reviewed_by=row["reviewed_by"],
            confirmed_by=row["confirmed_by"],
            rationale_pointer=row["rationale_pointer"],
            detected_at=row["detected_at"],
            review_started_at=row["review_started_at"],
            resolved_at=row["resolved_at"],
            escalation_count=row["escalation_count"],
            schema_version=row["schema_version"],
        )

    def insert_hit(self, hit: SanctionsHit) -> None:
        _execute(
            self._connect,
            "INSERT INTO sanctions_hits (sanc_id, screening_id, entry_id, "
            "entry_content_hash, list_code, list_version_id, subject_type, subject_id, "
            "payment_intent_id, status, score, matched_alias, freeze_applied, "
            "freeze_released_at, aml_alert_id, str_report_id, approval_request_id, "
            "reviewed_by, confirmed_by, rationale_pointer, detected_at, review_started_at, "
            "resolved_at, escalation_count, schema_version) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"
            "%s,%s,%s)",
            (
                hit.sanc_id,
                hit.screening_id,
                hit.entry_id,
                hit.entry_content_hash,
                hit.list_code,
                hit.list_version_id,
                hit.subject_type,
                hit.subject_id,
                hit.payment_intent_id,
                hit.status,
                hit.score,
                hit.matched_alias,
                hit.freeze_applied,
                hit.freeze_released_at,
                hit.aml_alert_id,
                hit.str_report_id,
                hit.approval_request_id,
                hit.reviewed_by,
                hit.confirmed_by,
                hit.rationale_pointer,
                hit.detected_at,
                hit.review_started_at,
                hit.resolved_at,
                hit.escalation_count,
                hit.schema_version,
            ),
        )

    def save_hit(self, hit: SanctionsHit) -> None:
        _execute(
            self._connect,
            "UPDATE sanctions_hits SET status=%s, freeze_applied=%s, freeze_released_at=%s, "
            "aml_alert_id=%s, str_report_id=%s, approval_request_id=%s, reviewed_by=%s, "
            "confirmed_by=%s, rationale_pointer=%s, review_started_at=%s, resolved_at=%s, "
            "escalation_count=%s WHERE sanc_id=%s",
            (
                hit.status,
                hit.freeze_applied,
                hit.freeze_released_at,
                hit.aml_alert_id,
                hit.str_report_id,
                hit.approval_request_id,
                hit.reviewed_by,
                hit.confirmed_by,
                hit.rationale_pointer,
                hit.review_started_at,
                hit.resolved_at,
                hit.escalation_count,
                hit.sanc_id,
            ),
        )

    def get_hit(self, sanc_id: str) -> SanctionsHit | None:
        row = _fetchone(
            self._connect, "SELECT * FROM sanctions_hits WHERE sanc_id=%s", (sanc_id,)
        )
        return self._to_hit(row) if row else None

    def hits_by_subject(self, subject_type: str, subject_id: str) -> list[SanctionsHit]:
        rows = _fetchall(
            self._connect,
            "SELECT * FROM sanctions_hits WHERE subject_type=%s AND subject_id=%s "
            "ORDER BY detected_at",
            (subject_type, subject_id),
        )
        return [self._to_hit(r) for r in rows]

    def hits_by_status(self, status: str) -> list[SanctionsHit]:
        rows = _fetchall(
            self._connect,
            "SELECT * FROM sanctions_hits WHERE status=%s ORDER BY detected_at",
            (status,),
        )
        return [self._to_hit(r) for r in rows]

    # -- whitelist ------------------------------------------------------------

    @staticmethod
    def _to_wl(row: dict) -> WhitelistRow:
        return WhitelistRow(
            wl_id=row["wl_id"],
            subject_type=row["subject_type"],
            subject_id=row["subject_id"],
            entry_content_hash=row["entry_content_hash"],
            list_code=row["list_code"],
            source_sanc_id=row["source_sanc_id"],
            approval_request_id=row["approval_request_id"],
            approved_by=row["approved_by"],
            rationale_pointer=row["rationale_pointer"],
            created_at=row["created_at"],
            expires_at=row["expires_at"],
            revoked_at=row["revoked_at"],
            schema_version=row["schema_version"],
        )

    def insert_whitelist(self, row: WhitelistRow) -> None:
        _execute(
            self._connect,
            "INSERT INTO screening_whitelist (wl_id, subject_type, subject_id, "
            "entry_content_hash, list_code, source_sanc_id, approval_request_id, "
            "approved_by, rationale_pointer, created_at, expires_at, revoked_at, "
            "schema_version) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                row.wl_id,
                row.subject_type,
                row.subject_id,
                row.entry_content_hash,
                row.list_code,
                row.source_sanc_id,
                row.approval_request_id,
                row.approved_by,
                row.rationale_pointer,
                row.created_at,
                row.expires_at,
                row.revoked_at,
                row.schema_version,
            ),
        )

    def whitelist_for_subject(
        self, subject_type: str, subject_id: str
    ) -> list[WhitelistRow]:
        rows = _fetchall(
            self._connect,
            "SELECT * FROM screening_whitelist WHERE subject_type=%s AND subject_id=%s",
            (subject_type, subject_id),
        )
        return [self._to_wl(r) for r in rows]


# ---------------------------------------------------------------------------
# PEP records
# ---------------------------------------------------------------------------


class PostgresPepStore:
    """pep_records (migration 0077)."""

    def __init__(self, connection_factory: ConnectionFactory) -> None:
        self._connect = connection_factory

    @staticmethod
    def _to_record(row: dict) -> PepRecord:
        return PepRecord(
            pep_id=row["pep_id"],
            subject_type=row["subject_type"],
            subject_id=row["subject_id"],
            pep_category=row["pep_category"],
            position_held=row["position_held"],
            source=row["source"],
            status=row["status"],
            next_review_at=row["next_review_at"],
            created_at=row["created_at"],
            edd_completed_at=row["edd_completed_at"],
            camlco_approved_by=row["camlco_approved_by"],
            camlco_approved_at=row["camlco_approved_at"],
            closed_at=row["closed_at"],
            close_approval_request_id=row["close_approval_request_id"],
            schema_version=row["schema_version"],
        )

    def insert(self, record: PepRecord) -> None:
        _execute(
            self._connect,
            "INSERT INTO pep_records (pep_id, subject_type, subject_id, pep_category, "
            "position_held, source, status, edd_completed_at, camlco_approved_by, "
            "camlco_approved_at, next_review_at, closed_at, close_approval_request_id, "
            "created_at, schema_version) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                record.pep_id,
                record.subject_type,
                record.subject_id,
                record.pep_category,
                record.position_held,
                record.source,
                record.status,
                record.edd_completed_at,
                record.camlco_approved_by,
                record.camlco_approved_at,
                record.next_review_at,
                record.closed_at,
                record.close_approval_request_id,
                record.created_at,
                record.schema_version,
            ),
        )

    def save(self, record: PepRecord) -> None:
        _execute(
            self._connect,
            "UPDATE pep_records SET status=%s, edd_completed_at=%s, camlco_approved_by=%s, "
            "camlco_approved_at=%s, next_review_at=%s, closed_at=%s, "
            "close_approval_request_id=%s WHERE pep_id=%s",
            (
                record.status,
                record.edd_completed_at,
                record.camlco_approved_by,
                record.camlco_approved_at,
                record.next_review_at,
                record.closed_at,
                record.close_approval_request_id,
                record.pep_id,
            ),
        )

    def get(self, pep_id: str) -> PepRecord | None:
        row = _fetchone(self._connect, "SELECT * FROM pep_records WHERE pep_id=%s", (pep_id,))
        return self._to_record(row) if row else None

    def active_for_subject(self, subject_type: str, subject_id: str) -> list[PepRecord]:
        rows = _fetchall(
            self._connect,
            "SELECT * FROM pep_records WHERE subject_type=%s AND subject_id=%s "
            "AND status='ACTIVE' ORDER BY created_at",
            (subject_type, subject_id),
        )
        return [self._to_record(r) for r in rows]


# ---------------------------------------------------------------------------
# Monitoring rule events
# ---------------------------------------------------------------------------


class PostgresMonitoringEventStore:
    """monitoring_rule_events (migration 0078)."""

    def __init__(self, connection_factory: ConnectionFactory) -> None:
        self._connect = connection_factory

    def insert(self, row: MonitoringRuleEvent) -> None:
        _execute(
            self._connect,
            "INSERT INTO monitoring_rule_events (event_id, payment_intent_id, rule_id, "
            "rule_pack_id, subject_type, subject_id, evaluation_result, score_delta, "
            "alert_id, evaluated_at, schema_version) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (event_id) DO NOTHING",
            (
                row.event_id,
                row.payment_intent_id,
                row.rule_id,
                row.rule_pack_id,
                row.subject_type,
                row.subject_id,
                row.evaluation_result,
                row.score_delta,
                row.alert_id,
                row.evaluated_at,
                row.schema_version,
            ),
        )

    def by_subject(self, subject_type: str, subject_id: str) -> list[MonitoringRuleEvent]:
        rows = _fetchall(
            self._connect,
            "SELECT * FROM monitoring_rule_events WHERE subject_type=%s AND subject_id=%s "
            "ORDER BY evaluated_at",
            (subject_type, subject_id),
        )
        return [
            MonitoringRuleEvent(
                event_id=r["event_id"],
                payment_intent_id=r["payment_intent_id"],
                rule_id=r["rule_id"],
                rule_pack_id=r["rule_pack_id"],
                subject_type=r["subject_type"],
                subject_id=r["subject_id"],
                evaluation_result=r["evaluation_result"],
                score_delta=r["score_delta"],
                alert_id=r["alert_id"],
                evaluated_at=r["evaluated_at"],
                schema_version=r["schema_version"],
            )
            for r in rows
        ]

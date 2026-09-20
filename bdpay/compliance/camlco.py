"""CAMLCO review queue + evidence-pack ZIP export (spec/06 + spec/07).

The queue aggregates everything awaiting CAMLCO attention: escalated/unworked
AML alerts, STR reports pending review or overdue against the 24h filing
clock, and sanctions hits awaiting review or stuck in review.

The evidence-pack export follows the dataroom-bd BFIU export pattern
(``dataroom-bd/src/lib/admin/bfiu-export.ts``, PORT PATTERN per
PORTING-MAP.md): a ZIP containing a ``manifest.json`` with record counts and
a sha256 over the combined content, one JSON file per section, and an
``audit_events.csv`` flat rendering — and the export itself writes an audit
event, so exports are part of the trail they export.
"""

from __future__ import annotations

import csv
import io
import json
import zipfile
from dataclasses import asdict, dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from bdpay.compliance.alerts import AlertStore
from bdpay.compliance.sanctions.screening import SanctionsStore
from bdpay.compliance.str_workflow import StrStore
from bdpay.platform.canonical import sha256_canonical
from bdpay.platform.clock import Clock
from bdpay.platform.interfaces import AuditEventSpec, AuditPort

__all__ = ["CamlcoQueue", "EvidencePack", "EvidencePackExporter"]

PRODUCER = "regulatory-reporting@1.0.0"


class CamlcoQueue:
    """The CAMLCO work queue across alerts, STRs, and sanctions hits."""

    def __init__(
        self,
        *,
        alerts: AlertStore,
        strs: StrStore,
        sanctions: SanctionsStore,
        clock: Clock,
    ) -> None:
        self._alerts = alerts
        self._strs = strs
        self._sanctions = sanctions
        self._clock = clock

    def pending(self) -> dict[str, list[str]]:
        """IDs awaiting CAMLCO action, grouped by lane (deterministic order)."""
        now = self._clock.now()
        alerts_open = [
            a.alert_id
            for status in ("RAISED", "TRIAGING", "IN_CASE")
            for a in self._alerts.by_status(status)
        ]
        strs_review = [r.str_id for r in self._strs.by_status("PENDING_CAMLCO_REVIEW")]
        strs_failed = [r.str_id for r in self._strs.by_status("FILING_FAILED")]
        strs_overdue = [
            r.str_id
            for status in (
                "CAMLCO_APPROVED",
                "FILING_PENDING",
                "FILING_FAILED",
                "FILING_SUBMITTED",
            )
            for r in self._strs.by_status(status)
            if r.filing_overdue(now)
        ]
        hits_frozen = [h.sanc_id for h in self._sanctions.hits_by_status("DETECTED_FROZEN")]
        hits_review = [h.sanc_id for h in self._sanctions.hits_by_status("UNDER_REVIEW")]
        return {
            "alerts_open": alerts_open,
            "str_pending_review": strs_review,
            "str_filing_failed": strs_failed,
            "str_filing_clock_overdue": strs_overdue,
            "sanctions_hits_frozen_unreviewed": hits_frozen,
            "sanctions_hits_under_review": hits_review,
        }

    def case_decision_trail(self, alert_id: str) -> dict[str, object]:
        """The full decision trail for one case (alert + linked STRs)."""
        alert = self._alerts.get(alert_id)
        if alert is None:
            return {"alert": None, "str_reports": []}
        return {
            "alert": _plain(asdict(alert)),
            "str_reports": [_plain(asdict(r)) for r in self._strs.by_alert(alert_id)],
        }


def _plain(obj: Any) -> Any:
    """JSON-safe rendering: datetimes/dates ISO, Decimals as strings, tuples
    as lists — deterministic and float-free."""
    if isinstance(obj, dict):
        return {k: _plain(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [_plain(v) for v in obj]
    if isinstance(obj, datetime | date):
        return obj.isoformat()
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, float):
        raise ValueError("floats must never appear in evidence content")
    return obj


@dataclass(frozen=True)
class EvidencePack:
    """One built evidence pack: the ZIP bytes + its manifest."""

    zip_bytes: bytes
    manifest: dict
    audit_event_id: str


class EvidencePackExporter:
    """Builds the BFIU evidence-pack ZIP (dataroom-bd export pattern)."""

    def __init__(
        self,
        *,
        alerts: AlertStore,
        strs: StrStore,
        sanctions: SanctionsStore,
        clock: Clock,
        audit: AuditPort,
    ) -> None:
        self._alerts = alerts
        self._strs = strs
        self._sanctions = sanctions
        self._clock = clock
        self._audit = audit

    def export(
        self,
        *,
        actor_id: str,
        audit_events: list[dict] | None = None,
        conn: Any | None = None,
    ) -> EvidencePack:
        """Build the ZIP: manifest.json + section JSONs + audit_events.csv.

        ``audit_events`` rows come from the audit journal read surface
        (spec/03 owns the store); the exporter renders whatever trail the
        caller is authorized to extract.
        """
        now = self._clock.now()
        audit_rows = [_plain(row) for row in (audit_events or [])]

        alert_rows = [
            _plain(asdict(a))
            for status in ("RAISED", "TRIAGING", "IN_CASE", "STR_FILED", "CLOSED")
            for a in self._alerts.by_status(status)
        ]
        str_rows = [
            _plain(asdict(r))
            for status in (
                "PENDING_CAMLCO_REVIEW",
                "CAMLCO_APPROVED",
                "FILING_PENDING",
                "FILING_SUBMITTED",
                "FILING_CONFIRMED",
                "FILING_FAILED",
                "WITHDRAWN",
            )
            for r in self._strs.by_status(status)
        ]
        hit_rows = [
            _plain(asdict(h))
            for status in (
                "DETECTED_FROZEN",
                "UNDER_REVIEW",
                "CONFIRMED",
                "CLEARED_FALSE_POSITIVE",
            )
            for h in self._sanctions.hits_by_status(status)
        ]
        screening_rows = [
            _plain(asdict(s))
            for s in (
                self._sanctions.get_screening(h["screening_id"])
                for h in hit_rows
            )
            if s is not None
        ]

        sections: dict[str, list[dict]] = {
            "audit_events": audit_rows,
            "aml_alerts": alert_rows,
            "str_reports": str_rows,
            "sanctions_hits": hit_rows,
            "screenings": screening_rows,
        }

        # Manifest BEFORE zipping so the sha256 covers the combined content
        # (the dataroom-bd pattern: hash the content JSON, then archive).
        content_hash = sha256_canonical({name: rows for name, rows in sections.items()})
        manifest = {
            "export_kind": "BFIU_EVIDENCE_PACK",
            "exported_at": now.isoformat(),
            "exported_by": actor_id,
            "audit_event_count": len(audit_rows),
            "alert_count": len(alert_rows),
            "str_report_count": len(str_rows),
            "sanctions_hit_count": len(hit_rows),
            "screening_count": len(screening_rows),
            "sha256": content_hash,
        }

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", json.dumps(manifest, indent=2, sort_keys=True))
            for name, rows in sections.items():
                archive.writestr(f"{name}.json", json.dumps(rows, indent=2, sort_keys=True))
            archive.writestr("audit_events.csv", self._audit_csv(audit_rows))

        audit_event_id = self._audit.append(
            AuditEventSpec(
                event_type="BFIU_EVIDENCE_PACK_EXPORTED",
                actor_id=actor_id,
                subject_type="EvidencePack",
                subject_id=content_hash[:24],
                payload={
                    "manifest_sha256": content_hash,
                    "alert_count": len(alert_rows),
                    "str_report_count": len(str_rows),
                    "sanctions_hit_count": len(hit_rows),
                },
            ),
            clock=self._clock,
            conn=conn,
        )
        return EvidencePack(
            zip_bytes=buffer.getvalue(), manifest=manifest, audit_event_id=audit_event_id
        )

    @staticmethod
    def _audit_csv(audit_rows: list[dict]) -> str:
        """Flat CSV rendering of the audit trail (dataroom-bd export shape)."""
        out = io.StringIO()
        writer = csv.writer(out, lineterminator="\n")
        header = (
            "event_id",
            "event_type",
            "actor_id",
            "subject_type",
            "subject_id",
            "from_state",
            "to_state",
            "occurred_at",
        )
        writer.writerow(header)
        for row in audit_rows:
            writer.writerow([str(row.get(col, "") or "") for col in header])
        return out.getvalue()

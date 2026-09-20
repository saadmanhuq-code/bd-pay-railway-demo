"""FactCorrection entity, FSM, and in-memory store (spec/16 LR-3).

States: ``UNVERIFIED -> VERIFIED (terminal)``
        ``UNVERIFIED -> RETRACTED (terminal)``

Refusal-first: any transition not listed is DENIED.  Every transition writes
one ``audit_events`` row via the injected :class:`AuditPort`.  ``VERIFIED``
rows are immutable; a superseding correction records ``supersedes_correction_id``
and retags external docs — the register is append-only history.

ID scheme: ``fcor_<sha256_canonical({claim_source, claim_text})[:24]>``
per spec/16 Entities. The ``fcor`` prefix is registered in
``bdpay.platform.ids.PREFIXES`` (E18 fold-in) and IDs derive through the
canonical :func:`bdpay.platform.ids.make_id` — one namespace everywhere
(application, migrations 0090/0091, CI fact gate, sponsor-bank pack).

Maker-checker rule (binding):
  ``verified_by`` MUST differ from ``created_by``; attempting self-verify
  raises :class:`~bdpay.platform.errors.AuthorizationError` code
  ``verifier_must_differ``.

Seed rows (FC-01 … FC-06) are created by migration ``0090_fact_corrections.sql``
with ``status=UNVERIFIED, external_blocking=TRUE``.
The seed-key constants here let application code and the tiered-MDR migration
look up the canonical ``correction_id`` values without re-deriving them.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from bdpay.platform.clock import Clock
from bdpay.platform.errors import (
    AuthorizationError,
    ConflictError,
    InvalidRequestError,
    NotFoundError,
)
from bdpay.platform.ids import make_id
from bdpay.platform.interfaces import AuditEventSpec, AuditPort, OutboxPort
from bdpay.platform.outbox import build_event

__all__ = [
    "FACT_CORRECTION_SEED_KEYS",
    "FC_02_RETRACTED_ID",
    "FC_02_SUPERSEDING_ID",
    "FactCorrection",
    "FactCorrectionService",
    "FactCorrectionStore",
    "InMemoryFactCorrectionStore",
    "PostgresFactCorrectionStore",
    "make_fcor_id",
]

PRODUCER = "compliance-fact-corrections@1.0.0"

_TERMINAL = frozenset({"VERIFIED", "RETRACTED"})


# ---------------------------------------------------------------------------
# ID helper
# ---------------------------------------------------------------------------

def make_fcor_id(claim_source: str, claim_text: str) -> str:
    """Derive the deterministic ``fcor_<hash>`` for a correction row.

    Identical inputs always yield the same ID (content-addressed, E12 bytes).
    Delegates to the canonical :func:`make_id` — single ``fcor`` namespace.
    """
    return make_id("fcor", {"claim_source": claim_source, "claim_text": claim_text})


# ---------------------------------------------------------------------------
# Binding seed-key constants (spec/16 §B seed rows, verbatim claim texts)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# RC-1 supersede constants (pre-verification correction per FACT-VERIFICATION-PACK
# 2026-06-13 §FC-02 CONTRADICTS / §REQUIRED-CORRECTIONS RC-1).
#
# The Jan-2024 tiered MDR claim (50 bps micro/debit-prepaid, 80 bps
# micro/credit+MFS-PSP, 70 bps personal-retail) is STALE: PSD Circular
# No. 02/2025 (06 Feb 2025) supersedes with a FLAT 1.15% MDR for ALL
# merchants and withdraws the 0.15% NPSB platform fee.
#
# Operational flow (requires live DB + two operators):
#   Operator A: POST /v1/fact-corrections  (corrected claim,
#               supersedes_correction_id=FC_02_RETRACTED_ID)
#   Operator A: POST /v1/fact-corrections/{FC_02_RETRACTED_ID}/retract
#   Operator B: POST /v1/fact-corrections/{FC_02_SUPERSEDING_ID}/verify
#               (with Circular 02/2025 PDF as evidence)
# ---------------------------------------------------------------------------

#: fcor ID of the OLD (retracted) tiered-MDR claim — Jan-2024 regime.
#: Migration 0091's gate checks this ID; since it is RETRACTED (not VERIFIED)
#: the gate permanently holds, which is correct: the Jan-2024 tiered seed rows
#: must NOT be applied to the database.
FC_02_RETRACTED_ID: str = make_fcor_id(
    "research/05 §4.1 + spec/04 MDR table",
    "Bangla QR MDR is tiered (micro-merchant debit/prepaid cap, "
    "micro-merchant credit/MFS/PSP cap, personal-retail rate) under the "
    "1.15% ceiling — exact rates and the BB circular reference",
)

#: fcor ID of the NEW superseding claim — PSD Circular No. 02/2025 flat 1.15%.
#: Created by Operator A via POST /v1/fact-corrections with
#: supersedes_correction_id=FC_02_RETRACTED_ID; verified by Operator B with
#: the Circular 02/2025 PDF as evidence.
FC_02_SUPERSEDING_ID: str = make_fcor_id(
    "research/05 §4.1 + spec/04 MDR table (RC-1 correction)",
    "Bangla QR MDR is FLAT 1.15% for ALL merchants; 0.15% NPSB platform fee "
    "WITHDRAWN — BB PSD Circular No. 02/2025 (06 Feb 2025). The Jan-2024 "
    "tiered 0.50/0.80 micro caps and 0.70% personal-retail are historical "
    "(superseded). MOAT-AND-GAPS line 27 citation 'BB PSD circular 18 Jan 2024' "
    "is stale for the fee engine.",
)


#: Maps seed key (FC-01 … FC-06) -> correction_id.
#: FC-02 maps to FC_02_RETRACTED_ID (the seeded OLD row from migration 0090).
#: The RETRACTED status keeps migration 0091's gate permanently blocked, which is
#: correct: the Jan-2024 tiered values must NOT be applied to the database.
#: The superseding corrected row is tracked by FC_02_SUPERSEDING_ID above.
FACT_CORRECTION_SEED_KEYS: dict[str, str] = {
    "FC-01": make_fcor_id(
        "research/05 §2.2",
        "Binimoy suspended by BB; MFS/bank interoperability live 1 Nov 2025 over NPSB",
    ),
    "FC-02": FC_02_RETRACTED_ID,
    "FC-03": make_fcor_id(
        "research/02 §1 / interop fee model",
        "NPSB per-leg interop fees (bank→MFS, MFS→MFS/bank max, bank→PSP, "
        "sender-pays) — exact schedule",
    ),
    "FC-04": make_fcor_id(
        "research/05 §1/§10, CANONICAL §1",
        "License path + capital table vs PSO Regulation 2025 and draft DEMI "
        "rules (incl. reapplication window)",
    ),
    "FC-05": make_fcor_id(
        "research/07 item 9, spec/12 §H",
        "Veridyn P2 corpus coverage of PSS Act 2024 / BPSSR 2014 (gates any "
        "external use of the 78,411-rule claim)",
    ),
    "FC-06": make_fcor_id(
        "spec/08 / BB micro-merchant circular",
        "The BB micro-merchant definition used to set "
        "merchants.merchant_class = 'MICRO'",
    ),
}


# ---------------------------------------------------------------------------
# Entity
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class FactCorrection:
    """Immutable snapshot of one fact_corrections row."""

    correction_id: str
    claim_source: str
    claim_text: str
    corrected_text: str
    status: str
    external_blocking: bool
    evidence_pointer: str | None
    evidence_sha256: str | None
    supersedes_correction_id: str | None
    created_by: str
    verified_by: str | None
    created_at: datetime
    verified_at: datetime | None
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.status not in ("UNVERIFIED", "VERIFIED", "RETRACTED"):
            raise ValueError(f"invalid FactCorrection status {self.status!r}")
        if self.status == "VERIFIED":
            if not (self.evidence_pointer and self.evidence_sha256
                    and self.verified_by and self.verified_at):
                raise ValueError(
                    "VERIFIED correction requires evidence_pointer, evidence_sha256, "
                    "verified_by, and verified_at"
                )
            if self.verified_by == self.created_by:
                raise ValueError("verified_by must differ from created_by (maker-checker)")

    @property
    def is_terminal(self) -> bool:
        return self.status in _TERMINAL


# ---------------------------------------------------------------------------
# Store protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class FactCorrectionStore(Protocol):
    """Persistence port for :class:`FactCorrection` rows."""

    def save(self, correction: FactCorrection) -> None: ...

    def get(self, correction_id: str) -> FactCorrection | None: ...

    def list_all(self, status: str | None = None) -> Sequence[FactCorrection]: ...


class InMemoryFactCorrectionStore:
    """In-memory implementation for tests."""

    def __init__(self) -> None:
        self._rows: dict[str, FactCorrection] = {}

    def save(self, correction: FactCorrection) -> None:
        self._rows[correction.correction_id] = correction

    def get(self, correction_id: str) -> FactCorrection | None:
        return self._rows.get(correction_id)

    def list_all(self, status: str | None = None) -> list[FactCorrection]:
        rows = list(self._rows.values())
        if status is not None:
            rows = [r for r in rows if r.status == status]
        return rows


ConnectionFactory = Callable[[], Any]

_FCOR_COLUMNS = """
    correction_id, claim_source, claim_text, corrected_text, status,
    external_blocking, evidence_pointer, evidence_sha256,
    supersedes_correction_id, created_by, verified_by, created_at,
    verified_at, schema_version
"""


def _fact_correction_from_row(row: dict[str, Any]) -> FactCorrection:
    return FactCorrection(
        correction_id=row["correction_id"],
        claim_source=row["claim_source"],
        claim_text=row["claim_text"],
        corrected_text=row["corrected_text"],
        status=row["status"],
        external_blocking=row["external_blocking"],
        evidence_pointer=row["evidence_pointer"],
        evidence_sha256=row["evidence_sha256"],
        supersedes_correction_id=row["supersedes_correction_id"],
        created_by=row["created_by"],
        verified_by=row["verified_by"],
        created_at=row["created_at"],
        verified_at=row["verified_at"],
        schema_version=row["schema_version"],
    )


class PostgresFactCorrectionStore:
    """Postgres store for migration ``0090_fact_corrections.sql``."""

    def __init__(
        self, connection_factory: ConnectionFactory, *, schema: str = "compliance"
    ) -> None:
        self._connect = connection_factory
        self._schema = schema

    def _table(self) -> Any:
        from psycopg import sql

        return sql.SQL("{}.fact_corrections").format(sql.Identifier(self._schema))

    def save(self, correction: FactCorrection) -> None:
        from psycopg import sql

        query = sql.SQL(
            """
            INSERT INTO {table} (
                correction_id, claim_source, claim_text, corrected_text, status,
                external_blocking, evidence_pointer, evidence_sha256,
                supersedes_correction_id, created_by, verified_by, created_at,
                verified_at, schema_version
            ) VALUES (
                %(correction_id)s, %(claim_source)s, %(claim_text)s, %(corrected_text)s,
                %(status)s, %(external_blocking)s, %(evidence_pointer)s,
                %(evidence_sha256)s, %(supersedes_correction_id)s, %(created_by)s,
                %(verified_by)s, %(created_at)s, %(verified_at)s, %(schema_version)s
            )
            ON CONFLICT (correction_id) DO UPDATE SET
                claim_source = EXCLUDED.claim_source,
                claim_text = EXCLUDED.claim_text,
                corrected_text = EXCLUDED.corrected_text,
                status = EXCLUDED.status,
                external_blocking = EXCLUDED.external_blocking,
                evidence_pointer = EXCLUDED.evidence_pointer,
                evidence_sha256 = EXCLUDED.evidence_sha256,
                supersedes_correction_id = EXCLUDED.supersedes_correction_id,
                created_by = EXCLUDED.created_by,
                verified_by = EXCLUDED.verified_by,
                created_at = EXCLUDED.created_at,
                verified_at = EXCLUDED.verified_at,
                schema_version = EXCLUDED.schema_version
            """
        ).format(table=self._table())
        params = {
            "correction_id": correction.correction_id,
            "claim_source": correction.claim_source,
            "claim_text": correction.claim_text,
            "corrected_text": correction.corrected_text,
            "status": correction.status,
            "external_blocking": correction.external_blocking,
            "evidence_pointer": correction.evidence_pointer,
            "evidence_sha256": correction.evidence_sha256,
            "supersedes_correction_id": correction.supersedes_correction_id,
            "created_by": correction.created_by,
            "verified_by": correction.verified_by,
            "created_at": correction.created_at,
            "verified_at": correction.verified_at,
            "schema_version": correction.schema_version,
        }
        with self._connect() as conn:
            conn.execute(query, params)
            conn.commit()

    def get(self, correction_id: str) -> FactCorrection | None:
        from psycopg import sql
        from psycopg.rows import dict_row

        query = sql.SQL(
            f"SELECT {_FCOR_COLUMNS} FROM {{table}} WHERE correction_id = %s"
        ).format(table=self._table())
        with self._connect() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(query, (correction_id,))
                row = cur.fetchone()
        return None if row is None else _fact_correction_from_row(row)

    def list_all(self, status: str | None = None) -> list[FactCorrection]:
        from psycopg import sql
        from psycopg.rows import dict_row

        where = sql.SQL("") if status is None else sql.SQL("WHERE status = %s")
        query = (
            sql.SQL(f"SELECT {_FCOR_COLUMNS} FROM {{table}} ").format(table=self._table())
            + where
            + sql.SQL(" ORDER BY created_at, correction_id")
        )
        params: tuple[str, ...] = () if status is None else (status,)
        with self._connect() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(query, params)
                rows = cur.fetchall()
        return [_fact_correction_from_row(row) for row in rows]


# ---------------------------------------------------------------------------
# Service (FSM)
# ---------------------------------------------------------------------------

class FactCorrectionService:
    """Application service wrapping the FactCorrection FSM.

    All FSM transitions are refusal-first: unlisted transitions raise
    :class:`~bdpay.platform.errors.ConflictError`.
    """

    def __init__(
        self,
        store: FactCorrectionStore,
        audit: AuditPort,
        outbox: OutboxPort,
        clock: Clock,
    ) -> None:
        self._store = store
        self._audit = audit
        self._outbox = outbox
        self._clock = clock

    # ------------------------------------------------------------------
    # record (create)
    # ------------------------------------------------------------------

    def record(
        self,
        *,
        claim_source: str,
        claim_text: str,
        corrected_text: str,
        external_blocking: bool,
        created_by: str,
        supersedes_correction_id: str | None = None,
        conn: Any | None = None,
    ) -> FactCorrection:
        """Create a new UNVERIFIED FactCorrection row.

        Idempotent: identical (claim_source, claim_text) returns the existing row.
        """
        if not claim_source or not claim_text or not corrected_text or not created_by:
            raise InvalidRequestError(
                "claim_source, claim_text, corrected_text, and created_by are required",
                code="invalid_request",
            )
        correction_id = make_fcor_id(claim_source, claim_text)
        existing = self._store.get(correction_id)
        if existing is not None:
            return existing

        now = self._clock.now()
        correction = FactCorrection(
            correction_id=correction_id,
            claim_source=claim_source,
            claim_text=claim_text,
            corrected_text=corrected_text,
            status="UNVERIFIED",
            external_blocking=external_blocking,
            evidence_pointer=None,
            evidence_sha256=None,
            supersedes_correction_id=supersedes_correction_id,
            created_by=created_by,
            verified_by=None,
            created_at=now,
            verified_at=None,
        )
        self._store.save(correction)
        self._audit.append(
            AuditEventSpec(
                event_type="FACT_CORRECTION_RECORDED",
                actor_id=created_by,
                subject_type="fact_correction",
                subject_id=correction_id,
                to_state="UNVERIFIED",
                payload={"claim_source": claim_source, "external_blocking": external_blocking},
            ),
            clock=self._clock,
            conn=conn,
        )
        return correction

    # ------------------------------------------------------------------
    # verify  UNVERIFIED -> VERIFIED
    # ------------------------------------------------------------------

    def verify(
        self,
        correction_id: str,
        *,
        evidence_pointer: str,
        evidence_sha256: str,
        verified_by: str,
        conn: Any | None = None,
    ) -> FactCorrection:
        """Attach evidence and advance to VERIFIED.

        Maker-checker: ``verified_by`` must differ from ``created_by``.
        """
        correction = self._require(correction_id)
        if correction.is_terminal:
            raise ConflictError(
                f"fact_correction {correction_id} is already {correction.status}",
                code="terminal_state",
            )
        if correction.status != "UNVERIFIED":
            raise ConflictError(
                f"verify is only valid from UNVERIFIED, current={correction.status}",
                code="invalid_transition",
            )
        if not evidence_pointer or not evidence_sha256 or not verified_by:
            raise InvalidRequestError(
                "evidence_pointer, evidence_sha256, and verified_by are required",
                code="invalid_request",
            )
        if verified_by == correction.created_by:
            raise AuthorizationError(
                "the verifying operator must differ from the recording operator",
                code="verifier_must_differ",
            )
        now = self._clock.now()
        verified = replace(
            correction,
            status="VERIFIED",
            evidence_pointer=evidence_pointer,
            evidence_sha256=evidence_sha256,
            verified_by=verified_by,
            verified_at=now,
        )
        self._store.save(verified)
        self._audit.append(
            AuditEventSpec(
                event_type="FACT_CORRECTION_VERIFIED",
                actor_id=verified_by,
                subject_type="fact_correction",
                subject_id=correction_id,
                from_state="UNVERIFIED",
                to_state="VERIFIED",
                payload={"evidence_sha256": evidence_sha256},
            ),
            clock=self._clock,
            conn=conn,
        )
        self._outbox.enqueue(
            build_event(
                event_type="fact_correction.verified",
                topic="aml.events",
                subject_type="fact_correction",
                subject_id=correction_id,
                producer=PRODUCER,
                occurred_at=now,
                payload={"correction_id": correction_id, "claim_source": correction.claim_source},
            ),
            conn=conn,
        )
        return verified

    # ------------------------------------------------------------------
    # retract  UNVERIFIED -> RETRACTED
    # ------------------------------------------------------------------

    def retract(
        self,
        correction_id: str,
        *,
        retracted_by: str,
        conn: Any | None = None,
    ) -> FactCorrection:
        """Mark the correction RETRACTED (claim withdrawn entirely)."""
        correction = self._require(correction_id)
        if correction.is_terminal:
            raise ConflictError(
                f"fact_correction {correction_id} is already {correction.status}",
                code="terminal_state",
            )
        if correction.status != "UNVERIFIED":
            raise ConflictError(
                f"retract is only valid from UNVERIFIED, current={correction.status}",
                code="invalid_transition",
            )
        retracted = replace(correction, status="RETRACTED")
        self._store.save(retracted)
        self._audit.append(
            AuditEventSpec(
                event_type="FACT_CORRECTION_RETRACTED",
                actor_id=retracted_by,
                subject_type="fact_correction",
                subject_id=correction_id,
                from_state="UNVERIFIED",
                to_state="RETRACTED",
                payload={},
            ),
            clock=self._clock,
            conn=conn,
        )
        return retracted

    # ------------------------------------------------------------------
    # read helpers
    # ------------------------------------------------------------------

    def get(self, correction_id: str) -> FactCorrection:
        return self._require(correction_id)

    def list_all(self, status: str | None = None) -> Sequence[FactCorrection]:
        return self._store.list_all(status=status)

    # ------------------------------------------------------------------

    def _require(self, correction_id: str) -> FactCorrection:
        row = self._store.get(correction_id)
        if row is None:
            raise NotFoundError(f"fact_correction {correction_id} not found", code="not_found")
        return row

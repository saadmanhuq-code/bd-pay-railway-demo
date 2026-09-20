"""DossierExport assembler — hash-manifested evidence ZIPs (spec/16 LR-1/LR-2).

The ZIP+manifest export shape is a port of the dataroom-bd BFIU export
pattern (``dataroom-bd/src/lib/admin/bfiu-export.ts``, PORT PATTERN per
PORTING-MAP.md): a deterministic archive whose first member is a
``manifest.json`` listing every other member with its SHA-256, plus a
``dossier_hash`` over the canonical members list — extended here with the
spec/16 binding determinism rules (members sorted by path, all timestamps
pinned to 1980-01-01, deflate level 6, ``canonical_json`` member bytes) so
re-assembly from an identical ``input_snapshot`` is byte-identical.

FSM (spec/16 State Machines §1, refusal-first):

    QUEUED     --[worker_pickup]--> ASSEMBLING   aud:DOSSIER_ASSEMBLY_STARTED
    ASSEMBLING --[assembled]------> COMPLETED    aud + event:dossier_export.completed
    ASSEMBLING --[assembly_error]-> FAILED       aud + event:dossier_export.failed

Anything else is DENIED; terminal rows are immutable. In Postgres the pickup
serializes via ``pg_advisory_xact_lock`` over the dxp id hash (SPEC_ERRATA E2
pattern, as in ``bdpay/ledger/pg_store.py::lock_chain_tip``); in memory a
claim flag stands in. The 30-minute assembly timeout
(``DOSSIER_ASSEMBLY_TIMEOUT_MINUTES``, injectable) is enforced by
``sweep_timeouts``; the DDL carries no assembly-start column, so staleness is
measured from ``created_at`` (documented; see the workstream errata note).

spec/03's ReplayService was never built. :class:`ChainReplayAdapter` is the
real default :class:`ReplayPort` here: it produces deterministic transcripts
for ``ENTRY_SEQUENCE`` and ``DISPUTE_TRAIL`` demos over a small ledger read
surface, and the assembler runs every demo TWICE, requiring byte-identical
``canonical_json`` output (the spec's determinism gate).
"""

from __future__ import annotations

import hashlib
import io
import zipfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from bdpay.compliance.ids_ext import make_compliance_id
from bdpay.platform.canonical import CanonicalError, canonical_json, sha256_canonical
from bdpay.platform.clock import Clock
from bdpay.platform.errors import ConflictError, InvalidRequestError, NotFoundError
from bdpay.platform.interfaces import AuditEventSpec, AuditPort, OutboxPort
from bdpay.platform.outbox import build_event

__all__ = [
    "CaseEvidencePort",
    "CertificationReadPort",
    "ChainReplayAdapter",
    "ChainVerifyPort",
    "DossierExportRecord",
    "DossierExportService",
    "DossierExportStore",
    "FactRegisterPort",
    "InMemoryDossierExportStore",
    "InMemoryObjectStore",
    "LedgerReadPort",
    "ObjectStorePort",
    "PostgresDossierExportStore",
    "ReplayPort",
    "SponsorPackPort",
    "TrialBalancePort",
]

PRODUCER = "regulatory-reporting@1.0.0"

DOSSIER_TYPES = ("BB_PHASE2", "SPONSOR_BANK_PACK", "BFIU_EVIDENCE")
_STATES = ("QUEUED", "ASSEMBLING", "COMPLETED", "FAILED")
_TERMINAL = ("COMPLETED", "FAILED")

#: CERT coverage required by the BB_PHASE2 gate (spec/10 check catalogue).
CERT_CHECK_IDS = tuple(f"CERT-{n:02d}" for n in range(1, 11))

#: spec/16 config key DOSSIER_ASSEMBLY_TIMEOUT_MINUTES default.
DEFAULT_ASSEMBLY_TIMEOUT_MINUTES = 30

#: Retention config dump defaults (spec/16 Regulatory mapping rows).
DEFAULT_RETENTION_CONFIG: Mapping[str, int] = {
    "retention_years": 12,
    "sandbox_retention_days": 90,
}

_DEFAULT_MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "db" / "migrations"

_SNAPSHOT_KEYS = (
    "as_of",
    "chain_ranges",
    "certification_run_ids",
    "replay_demos",
    "policy_doc_refs",
    "fact_register_as_of",
)


# ---------------------------------------------------------------------------
# Record
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DossierExportRecord:
    """One dossier_exports row (migration 0093)."""

    dossier_export_id: str
    dossier_type: str
    input_snapshot: Mapping[str, object]
    state: str
    requested_by: str
    created_at: datetime
    manifest: Mapping[str, object] | None = None
    dossier_hash: str | None = None
    zip_pointer: str | None = None
    fail_reason: str | None = None
    completed_at: datetime | None = None
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.dossier_type not in DOSSIER_TYPES:
            raise ValueError(
                f"dossier_type must be one of {DOSSIER_TYPES}, got {self.dossier_type!r}"
            )
        if self.state not in _STATES:
            raise ValueError(f"state must be one of {_STATES}, got {self.state!r}")

    @property
    def is_terminal(self) -> bool:
        return self.state in _TERMINAL


# ---------------------------------------------------------------------------
# Store protocol + in-memory implementation
# ---------------------------------------------------------------------------


@runtime_checkable
class DossierExportStore(Protocol):
    """Storage contract for dossier_exports (migration 0093)."""

    def insert(self, record: DossierExportRecord) -> None: ...

    def save(self, record: DossierExportRecord) -> None: ...

    def get(self, dossier_export_id: str) -> DossierExportRecord | None: ...

    def list(
        self, *, limit: int, cursor: str | None = None
    ) -> tuple[list[DossierExportRecord], str | None]: ...

    def by_state(self, state: str) -> list[DossierExportRecord]: ...

    def begin_assembly(self, dossier_export_id: str) -> DossierExportRecord | None: ...

    def oldest_queued(self) -> DossierExportRecord | None: ...


def _list_sort_key(record: DossierExportRecord) -> tuple:
    # idx_dxp_state ordering: created_at DESC, id DESC tie-break.
    return (record.created_at, record.dossier_export_id)


class InMemoryDossierExportStore:
    """Deterministic in-memory store for unit tests."""

    def __init__(self) -> None:
        self._rows: dict[str, DossierExportRecord] = {}
        self._order: list[str] = []
        self._claimed: set[str] = set()

    def insert(self, record: DossierExportRecord) -> None:
        if record.dossier_export_id in self._rows:
            raise ConflictError(f"dossier export {record.dossier_export_id} already exists")
        self._rows[record.dossier_export_id] = record
        self._order.append(record.dossier_export_id)

    def save(self, record: DossierExportRecord) -> None:
        existing = self._rows.get(record.dossier_export_id)
        if existing is None:
            raise ConflictError(f"unknown dossier export {record.dossier_export_id}")
        if existing.is_terminal:
            raise ConflictError(
                f"dossier export {record.dossier_export_id} is terminal "
                f"({existing.state}); terminal rows are immutable",
                code="fsm_transition_denied",
            )
        self._rows[record.dossier_export_id] = record

    def get(self, dossier_export_id: str) -> DossierExportRecord | None:
        return self._rows.get(dossier_export_id)

    def list(
        self, *, limit: int, cursor: str | None = None
    ) -> tuple[list[DossierExportRecord], str | None]:
        rows = sorted(self._rows.values(), key=_list_sort_key, reverse=True)
        if cursor is not None:
            anchor = self._rows.get(cursor)
            if anchor is None:
                raise InvalidRequestError(f"unknown cursor {cursor!r}", code="invalid_cursor")
            rows = [r for r in rows if _list_sort_key(r) < _list_sort_key(anchor)]
        page = rows[:limit]
        next_cursor = page[-1].dossier_export_id if len(rows) > limit else None
        return page, next_cursor

    def by_state(self, state: str) -> list[DossierExportRecord]:
        return [self._rows[i] for i in self._order if self._rows[i].state == state]

    def begin_assembly(self, dossier_export_id: str) -> DossierExportRecord | None:
        record = self._rows.get(dossier_export_id)
        if record is None or record.state != "QUEUED":
            return None
        if dossier_export_id in self._claimed:
            return None
        self._claimed.add(dossier_export_id)
        updated = replace(record, state="ASSEMBLING")
        self._rows[dossier_export_id] = updated
        return updated

    def oldest_queued(self) -> DossierExportRecord | None:
        queued = self.by_state("QUEUED")
        return queued[0] if queued else None


class PostgresDossierExportStore:
    """dossier_exports (migration 0093) via psycopg3.

    Pickup claims serialize through ``pg_advisory_xact_lock`` over the dxp id
    hash (SPEC_ERRATA E2 pattern, mirroring
    ``bdpay/ledger/pg_store.py::lock_chain_tip``); the lock is released at
    transaction end, and the QUEUED->ASSEMBLING update under it makes a
    second worker's claim return ``None``.
    """

    def __init__(self, connection_factory: Callable[[], Any]) -> None:
        self._connect = connection_factory

    @staticmethod
    def _to_record(row: dict) -> DossierExportRecord:
        return DossierExportRecord(
            dossier_export_id=row["dossier_export_id"],
            dossier_type=row["dossier_type"],
            input_snapshot=row["input_snapshot"],
            state=row["state"],
            manifest=row["manifest"],
            dossier_hash=row["dossier_hash"],
            zip_pointer=row["zip_pointer"],
            fail_reason=row["fail_reason"],
            requested_by=row["requested_by"],
            created_at=row["created_at"],
            completed_at=row["completed_at"],
            schema_version=row["schema_version"],
        )

    @staticmethod
    def _jsonb(payload: Mapping[str, object] | None) -> Any:
        if payload is None:
            return None
        from psycopg.types.json import Jsonb

        return Jsonb(dict(payload), dumps=lambda o: canonical_json(o).decode("utf-8"))

    def _fetchall(self, sql: str, params: tuple) -> list[dict]:
        from psycopg.rows import dict_row

        with self._connect() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(sql, params)
                return cur.fetchall()

    def insert(self, record: DossierExportRecord) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO dossier_exports (dossier_export_id, dossier_type, input_snapshot,"
                " state, manifest, dossier_hash, zip_pointer, fail_reason, requested_by,"
                " created_at, completed_at, schema_version)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    record.dossier_export_id,
                    record.dossier_type,
                    self._jsonb(record.input_snapshot),
                    record.state,
                    self._jsonb(record.manifest),
                    record.dossier_hash,
                    record.zip_pointer,
                    record.fail_reason,
                    record.requested_by,
                    record.created_at,
                    record.completed_at,
                    record.schema_version,
                ),
            )
            conn.commit()

    def save(self, record: DossierExportRecord) -> None:
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE dossier_exports SET state=%s, manifest=%s, dossier_hash=%s,"
                " zip_pointer=%s, fail_reason=%s, completed_at=%s"
                " WHERE dossier_export_id=%s AND state NOT IN ('COMPLETED','FAILED')",
                (
                    record.state,
                    self._jsonb(record.manifest),
                    record.dossier_hash,
                    record.zip_pointer,
                    record.fail_reason,
                    record.completed_at,
                    record.dossier_export_id,
                ),
            )
            updated = cur.rowcount
            conn.commit()
        if updated == 0:
            existing = self.get(record.dossier_export_id)
            if existing is None:
                raise ConflictError(f"unknown dossier export {record.dossier_export_id}")
            raise ConflictError(
                f"dossier export {record.dossier_export_id} is terminal "
                f"({existing.state}); terminal rows are immutable",
                code="fsm_transition_denied",
            )

    def get(self, dossier_export_id: str) -> DossierExportRecord | None:
        rows = self._fetchall(
            "SELECT * FROM dossier_exports WHERE dossier_export_id = %s",
            (dossier_export_id,),
        )
        return self._to_record(rows[0]) if rows else None

    def list(
        self, *, limit: int, cursor: str | None = None
    ) -> tuple[list[DossierExportRecord], str | None]:
        if cursor is None:
            rows = self._fetchall(
                "SELECT * FROM dossier_exports"
                " ORDER BY created_at DESC, dossier_export_id DESC LIMIT %s",
                (limit + 1,),
            )
        else:
            anchor = self.get(cursor)
            if anchor is None:
                raise InvalidRequestError(f"unknown cursor {cursor!r}", code="invalid_cursor")
            rows = self._fetchall(
                "SELECT * FROM dossier_exports"
                " WHERE (created_at, dossier_export_id) < (%s, %s)"
                " ORDER BY created_at DESC, dossier_export_id DESC LIMIT %s",
                (anchor.created_at, anchor.dossier_export_id, limit + 1),
            )
        page = [self._to_record(r) for r in rows[:limit]]
        next_cursor = page[-1].dossier_export_id if len(rows) > limit else None
        return page, next_cursor

    def by_state(self, state: str) -> list[DossierExportRecord]:
        rows = self._fetchall(
            "SELECT * FROM dossier_exports WHERE state = %s"
            " ORDER BY created_at ASC, dossier_export_id ASC",
            (state,),
        )
        return [self._to_record(r) for r in rows]

    def begin_assembly(self, dossier_export_id: str) -> DossierExportRecord | None:
        with self._connect() as conn:
            with conn.transaction():
                # E2 pattern: serialize claimants on the dxp id hash for the
                # duration of this transaction (no UPDATE-privilege row locks).
                conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (f"bdpay.dossier_exports.{dossier_export_id}",),
                )
                cur = conn.execute(
                    "SELECT state FROM dossier_exports WHERE dossier_export_id = %s",
                    (dossier_export_id,),
                )
                row = cur.fetchone()
                if row is None or row[0] != "QUEUED":
                    return None
                conn.execute(
                    "UPDATE dossier_exports SET state = 'ASSEMBLING'"
                    " WHERE dossier_export_id = %s AND state = 'QUEUED'",
                    (dossier_export_id,),
                )
        return self.get(dossier_export_id)

    def oldest_queued(self) -> DossierExportRecord | None:
        queued = self.by_state("QUEUED")
        return queued[0] if queued else None


# ---------------------------------------------------------------------------
# Member-source ports (Protocols; in-memory fakes live in the tests)
# ---------------------------------------------------------------------------


@runtime_checkable
class CertificationReadPort(Protocol):
    """spec/10 certification_runs read surface for evidence class 1."""

    def get_run(self, run_id: str) -> Mapping[str, object] | None: ...

    def registered_connector_ids(self) -> tuple[str, ...]: ...


@runtime_checkable
class ChainVerifyPort(Protocol):
    """spec/03 verify_chain(deep=True) wrapper — deep mode is the gate mode."""

    def verify(
        self, chain_domain: str, from_index: int, to_index: int
    ) -> Mapping[str, object]: ...


@runtime_checkable
class TrialBalancePort(Protocol):
    """spec/03 trial_balance_assertion + balances snapshot hash."""

    def snapshot(self) -> Mapping[str, object]: ...


@runtime_checkable
class ReplayPort(Protocol):
    """Deterministic replay transcript per demo (spec/03 ReplayService seam)."""

    def run(self, demo: Mapping[str, object]) -> Mapping[str, object]: ...


@runtime_checkable
class FactRegisterPort(Protocol):
    """LR-3 fact_corrections read surface (the table is another workstream's)."""

    def rows_as_of(self, as_of: str) -> Sequence[Mapping[str, object]]: ...


@runtime_checkable
class CaseEvidencePort(Protocol):
    """spec/15 case evidence pointers + hashes (BFIU evidence class 8)."""

    def evidence_for(self, payment_intent_id: str) -> Sequence[Mapping[str, object]]: ...


@runtime_checkable
class SponsorPackPort(Protocol):
    """LR-1 generated members (workstream-1 seam; optional dependency)."""

    def term_sheet_checklist_md(self) -> str: ...

    def claims_rows(self) -> Sequence[Mapping[str, object]]: ...


@runtime_checkable
class ObjectStorePort(Protocol):
    """Where sealed ZIPs land (12-yr retention per spec/16)."""

    def put(self, key: str, data: bytes) -> None: ...

    def get(self, key: str) -> bytes | None: ...

    def exists(self, key: str) -> bool: ...


class InMemoryObjectStore:
    """Deterministic in-memory object store."""

    def __init__(self) -> None:
        self._objects: dict[str, bytes] = {}

    def put(self, key: str, data: bytes) -> None:
        self._objects[key] = bytes(data)

    def get(self, key: str) -> bytes | None:
        return self._objects.get(key)

    def exists(self, key: str) -> bool:
        return key in self._objects


# ---------------------------------------------------------------------------
# ChainReplayAdapter — the real default ReplayPort (spec/03 ReplayService gap)
# ---------------------------------------------------------------------------


@runtime_checkable
class LedgerReadPort(Protocol):
    """The small ledger read surface the replay adapter consumes."""

    def list_entries(
        self, chain_domain: str, from_index: int, to_index: int
    ) -> Sequence[Mapping[str, object]]: ...

    def verify(
        self, chain_domain: str, from_index: int, to_index: int
    ) -> Mapping[str, object]: ...

    def dispute_trail(self, payment_intent_id: str) -> Sequence[Mapping[str, object]]: ...


class ChainReplayAdapter:
    """Deterministic replay transcripts over the ledger read surface.

    spec/03's ReplayService was specified but never built (verified absent at
    this build); the dossier cannot ship without class-4 evidence, so this
    adapter IS the replay implementation for the two demo modes the snapshot
    schema admits. Transcript content is pure function of the ledger rows:
    {mode, parameters, step_count, steps_digest, transcript_sha256,
    rerun_command} (+ verify status for ENTRY_SEQUENCE).
    """

    def __init__(self, ledger: LedgerReadPort) -> None:
        self._ledger = ledger

    def run(self, demo: Mapping[str, object]) -> Mapping[str, object]:
        mode = demo.get("mode")
        if mode == "ENTRY_SEQUENCE":
            chain_domain = str(demo["chain_domain"])
            from_index = int(demo["from_index"])  # type: ignore[arg-type]
            to_index = int(demo["to_index"])  # type: ignore[arg-type]
            steps = [dict(e) for e in self._ledger.list_entries(chain_domain, from_index, to_index)]
            verify = dict(self._ledger.verify(chain_domain, from_index, to_index))
            parameters: dict[str, object] = {
                "chain_domain": chain_domain,
                "from_index": from_index,
                "to_index": to_index,
            }
            body: dict[str, object] = {
                "mode": "ENTRY_SEQUENCE",
                "parameters": parameters,
                "step_count": len(steps),
                "steps_digest": sha256_canonical(steps),
                "verify_status": verify.get("status"),
                "entries_checked": verify.get("entries_checked"),
            }
            rerun = (
                "bdpay-replay --mode ENTRY_SEQUENCE"
                f" --chain-domain {chain_domain}"
                f" --from-index {from_index} --to-index {to_index}"
            )
        elif mode == "DISPUTE_TRAIL":
            payment_intent_id = str(demo["payment_intent_id"])
            steps = [dict(r) for r in self._ledger.dispute_trail(payment_intent_id)]
            body = {
                "mode": "DISPUTE_TRAIL",
                "parameters": {"payment_intent_id": payment_intent_id},
                "step_count": len(steps),
                "steps_digest": sha256_canonical(steps),
            }
            rerun = f"bdpay-replay --mode DISPUTE_TRAIL --payment-intent-id {payment_intent_id}"
        else:
            raise InvalidRequestError(
                f"unknown replay demo mode {mode!r}", code="invalid_replay_mode"
            )
        return {**body, "transcript_sha256": sha256_canonical(body), "rerun_command": rerun}


# ---------------------------------------------------------------------------
# input_snapshot validation (spec/16 §API A schema — all fields required)
# ---------------------------------------------------------------------------


def _invalid(message: str) -> InvalidRequestError:
    return InvalidRequestError(message, code="invalid_input_snapshot")


def _require_str(mapping: Mapping[str, object], key: str, where: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise _invalid(f"{where}.{key} must be a non-empty string")
    return value


def _require_int(mapping: Mapping[str, object], key: str, where: str) -> int:
    value = mapping.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise _invalid(f"{where}.{key} must be an integer")
    return value


def _require_exact_keys(mapping: Mapping[str, object], keys: tuple[str, ...], where: str) -> None:
    actual = set(mapping.keys())
    expected = set(keys)
    if actual != expected:
        raise _invalid(
            f"{where} must have exactly the keys {sorted(expected)}, got {sorted(actual)}"
        )


def validate_input_snapshot(snapshot: Mapping[str, object]) -> dict[str, object]:
    """Validate the binding §API A snapshot schema; returns a plain dict."""
    if not isinstance(snapshot, Mapping):
        raise _invalid("input_snapshot must be an object")
    _require_exact_keys(snapshot, _SNAPSHOT_KEYS, "input_snapshot")
    _require_str(snapshot, "as_of", "input_snapshot")
    _require_str(snapshot, "fact_register_as_of", "input_snapshot")

    chain_ranges = snapshot["chain_ranges"]
    if not isinstance(chain_ranges, Sequence) or isinstance(chain_ranges, str | bytes):
        raise _invalid("input_snapshot.chain_ranges must be a list")
    for i, item in enumerate(chain_ranges):
        where = f"input_snapshot.chain_ranges[{i}]"
        if not isinstance(item, Mapping):
            raise _invalid(f"{where} must be an object")
        _require_exact_keys(item, ("chain_domain", "from_index", "to_index"), where)
        _require_str(item, "chain_domain", where)
        from_index = _require_int(item, "from_index", where)
        to_index = _require_int(item, "to_index", where)
        if from_index < 0 or to_index < from_index:
            raise _invalid(f"{where} indexes must satisfy 0 <= from_index <= to_index")

    run_ids = snapshot["certification_run_ids"]
    if not isinstance(run_ids, Sequence) or isinstance(run_ids, str | bytes):
        raise _invalid("input_snapshot.certification_run_ids must be a list")
    for i, run_id in enumerate(run_ids):
        if not isinstance(run_id, str) or not run_id:
            raise _invalid(f"input_snapshot.certification_run_ids[{i}] must be a string")

    demos = snapshot["replay_demos"]
    if not isinstance(demos, Sequence) or isinstance(demos, str | bytes):
        raise _invalid("input_snapshot.replay_demos must be a list")
    for i, demo in enumerate(demos):
        where = f"input_snapshot.replay_demos[{i}]"
        if not isinstance(demo, Mapping):
            raise _invalid(f"{where} must be an object")
        mode = demo.get("mode")
        if mode == "ENTRY_SEQUENCE":
            _require_exact_keys(demo, ("mode", "chain_domain", "from_index", "to_index"), where)
            _require_str(demo, "chain_domain", where)
            from_index = _require_int(demo, "from_index", where)
            to_index = _require_int(demo, "to_index", where)
            if from_index < 0 or to_index < from_index:
                raise _invalid(f"{where} indexes must satisfy 0 <= from_index <= to_index")
        elif mode == "DISPUTE_TRAIL":
            _require_exact_keys(demo, ("mode", "payment_intent_id"), where)
            _require_str(demo, "payment_intent_id", where)
        else:
            raise _invalid(f"{where}.mode must be 'ENTRY_SEQUENCE' or 'DISPUTE_TRAIL'")

    refs = snapshot["policy_doc_refs"]
    if not isinstance(refs, Sequence) or isinstance(refs, str | bytes):
        raise _invalid("input_snapshot.policy_doc_refs must be a list")
    for i, ref in enumerate(refs):
        where = f"input_snapshot.policy_doc_refs[{i}]"
        if not isinstance(ref, Mapping):
            raise _invalid(f"{where} must be an object")
        _require_exact_keys(ref, ("name", "version", "sha256"), where)
        for key in ("name", "version", "sha256"):
            _require_str(ref, key, where)

    plain = {key: snapshot[key] for key in _SNAPSHOT_KEYS}
    try:
        canonical_json(plain)  # rejects floats/sets/naive datetimes by construction
    except CanonicalError as exc:
        raise _invalid(f"input_snapshot is not canonically serializable: {exc}") from exc
    return plain


# ---------------------------------------------------------------------------
# Assembly internals
# ---------------------------------------------------------------------------


class _AssemblyError(Exception):
    """Internal: aborts the assembly with the spec fail_reason."""

    def __init__(self, fail_reason: str) -> None:
        super().__init__(fail_reason)
        self.fail_reason = fail_reason


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _seal_zip(manifest_bytes: bytes, members: Mapping[str, bytes]) -> bytes:
    """ZIP per the binding determinism rules (spec/16 §API A)."""
    buffer = io.BytesIO()
    ordered = [("manifest.json", manifest_bytes)]
    ordered.extend(sorted(members.items()))
    with zipfile.ZipFile(buffer, "w") as archive:
        for path, data in ordered:
            info = zipfile.ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (0o100644 << 16)  # plain file, rw-r--r--, consistent
            info.create_system = 3  # unix, pinned (host-OS independence)
            archive.writestr(info, data, compresslevel=6)
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class DossierExportService:
    """Queue + worker + read surface for DossierExport (spec/16 §API A).

    Refusal-first orchestration: every gate failure turns the export FAILED
    with a named reason — never a partial ZIP (spec/16 Failure Modes).
    """

    def __init__(
        self,
        store: DossierExportStore,
        *,
        clock: Clock,
        audit: AuditPort,
        outbox: OutboxPort,
        object_store: ObjectStorePort,
        certification: CertificationReadPort,
        chain_verify: ChainVerifyPort,
        trial_balance: TrialBalancePort,
        replay: ReplayPort,
        fact_register: FactRegisterPort,
        case_evidence: CaseEvidencePort,
        sponsor_pack: SponsorPackPort | None = None,
        migrations_dir: Path | None = None,
        retention_config: Mapping[str, int] = DEFAULT_RETENTION_CONFIG,
        assembly_timeout_minutes: int = DEFAULT_ASSEMBLY_TIMEOUT_MINUTES,
        object_store_prefix: str = "objstore://dossiers/",
    ) -> None:
        self._store = store
        self._clock = clock
        self._audit = audit
        self._outbox = outbox
        self._objects = object_store
        self._certification = certification
        self._chain_verify = chain_verify
        self._trial_balance = trial_balance
        self._replay = replay
        self._fact_register = fact_register
        self._case_evidence = case_evidence
        self._sponsor_pack = sponsor_pack
        self._migrations_dir = migrations_dir or _DEFAULT_MIGRATIONS_DIR
        self._retention_config = dict(retention_config)
        self._timeout = timedelta(minutes=assembly_timeout_minutes)
        self._objstore_prefix = object_store_prefix

    # -- queue (202 semantics; idempotent by content-addressed id) ------------

    def queue(
        self,
        *,
        dossier_type: str,
        input_snapshot: Mapping[str, object],
        requested_by: str,
        conn: Any | None = None,
    ) -> DossierExportRecord:
        """Queue an assembly; identical type+snapshot returns the existing row."""
        if dossier_type not in DOSSIER_TYPES:
            raise InvalidRequestError(
                f"dossier_type must be one of {DOSSIER_TYPES}, got {dossier_type!r}",
                code="invalid_dossier_type",
            )
        if not requested_by:
            raise InvalidRequestError("requested_by is required", code="requested_by_required")
        snapshot = validate_input_snapshot(input_snapshot)
        dossier_export_id = make_compliance_id(
            "dxp", {"dossier_type": dossier_type, "input_snapshot": snapshot}
        )
        existing = self._store.get(dossier_export_id)
        if existing is not None:
            # Content-addressed idempotency: a FAILED row is returned as-is so
            # the operator sees the reason (spec/16 FSM retry note).
            return existing
        record = DossierExportRecord(
            dossier_export_id=dossier_export_id,
            dossier_type=dossier_type,
            input_snapshot=snapshot,
            state="QUEUED",
            requested_by=requested_by,
            created_at=self._clock.now(),
        )
        self._store.insert(record)
        return record

    # -- worker entry ----------------------------------------------------------

    def assemble_next(self, *, conn: Any | None = None) -> DossierExportRecord | None:
        """Pick up and assemble the oldest QUEUED export, if any."""
        queued = self._store.oldest_queued()
        if queued is None:
            return None
        return self.assemble(queued.dossier_export_id, conn=conn)

    def assemble(self, dossier_export_id: str, *, conn: Any | None = None) -> DossierExportRecord:
        """worker_pickup + assembled/assembly_error in one worker pass."""
        record = self._store.get(dossier_export_id)
        if record is None:
            raise NotFoundError(f"unknown dossier export {dossier_export_id}")
        claimed = self._store.begin_assembly(dossier_export_id)
        if claimed is None:
            raise ConflictError(
                f"transition 'worker_pickup' from state {record.state!r} is DENIED "
                "(refusal-first)",
                code="fsm_transition_denied",
            )
        self._audit.append(
            AuditEventSpec(
                event_type="DOSSIER_ASSEMBLY_STARTED",
                actor_id=PRODUCER,
                subject_type="DossierExport",
                subject_id=dossier_export_id,
                from_state="QUEUED",
                to_state="ASSEMBLING",
                payload={"dossier_type": claimed.dossier_type},
            ),
            clock=self._clock,
            conn=conn,
        )
        try:
            members = self._build_members(claimed)
            manifest, manifest_bytes, dossier_hash = self._build_manifest(
                dossier_export_id, claimed.input_snapshot, members
            )
            zip_bytes = _seal_zip(manifest_bytes, members)
            self._verify_sealed_zip(zip_bytes, manifest)
            zip_pointer = f"{self._objstore_prefix}{dossier_export_id}.zip"
            self._objects.put(zip_pointer, zip_bytes)
        except _AssemblyError as exc:
            return self._fail(claimed, fail_reason=exc.fail_reason, conn=conn)
        except Exception as exc:  # refusal-first: a member-source error is a FAILED export
            # spec/16 FSM trigger assembly_error covers "any member fetch/verify
            # failure"; a port raising anything else must not strand the row in
            # ASSEMBLING until the timeout sweep when the worker is still alive.
            return self._fail(
                claimed,
                fail_reason=f"member_failure: {type(exc).__name__}: {exc}",
                conn=conn,
            )
        completed = replace(
            claimed,
            state="COMPLETED",
            manifest=manifest,
            dossier_hash=dossier_hash,
            zip_pointer=zip_pointer,
            completed_at=self._clock.now(),
        )
        self._store.save(completed)
        self._audit.append(
            AuditEventSpec(
                event_type="DOSSIER_ASSEMBLY_COMPLETED",
                actor_id=PRODUCER,
                subject_type="DossierExport",
                subject_id=dossier_export_id,
                from_state="ASSEMBLING",
                to_state="COMPLETED",
                payload={"dossier_hash": dossier_hash, "member_count": len(members)},
            ),
            clock=self._clock,
            conn=conn,
        )
        self._outbox.enqueue(
            build_event(
                event_type="dossier_export.completed",
                subject_type="DossierExport",
                subject_id=dossier_export_id,
                producer=PRODUCER,
                topic="aml.events",
                payload={
                    "dossier_export_id": dossier_export_id,
                    "dossier_type": completed.dossier_type,
                    "dossier_hash": dossier_hash,
                },
                occurred_at=self._clock.now(),
            ),
            conn=conn,
        )
        return completed

    def _fail(
        self, record: DossierExportRecord, *, fail_reason: str, conn: Any | None
    ) -> DossierExportRecord:
        failed = replace(
            record,
            state="FAILED",
            fail_reason=fail_reason,
            completed_at=self._clock.now(),
        )
        self._store.save(failed)
        self._audit.append(
            AuditEventSpec(
                event_type="DOSSIER_ASSEMBLY_FAILED",
                actor_id=PRODUCER,
                subject_type="DossierExport",
                subject_id=record.dossier_export_id,
                from_state="ASSEMBLING",
                to_state="FAILED",
                payload={"fail_reason": fail_reason},
            ),
            clock=self._clock,
            conn=conn,
        )
        self._outbox.enqueue(
            build_event(
                event_type="dossier_export.failed",
                subject_type="DossierExport",
                subject_id=record.dossier_export_id,
                producer=PRODUCER,
                topic="aml.events",
                payload={
                    "dossier_export_id": record.dossier_export_id,
                    "dossier_type": record.dossier_type,
                    "fail_reason": fail_reason,
                },
                occurred_at=self._clock.now(),
            ),
            conn=conn,
        )
        return failed

    # -- timeout sweep -----------------------------------------------------------

    def sweep_timeouts(self, *, conn: Any | None = None) -> list[DossierExportRecord]:
        """Mark stale ASSEMBLING rows FAILED (DOSSIER_ASSEMBLY_TIMEOUT_MINUTES).

        The DDL has no assembly-start column, so staleness is measured from
        ``created_at`` (conservative: a row can only be older than its pickup).
        """
        now = self._clock.now()
        failed: list[DossierExportRecord] = []
        for record in self._store.by_state("ASSEMBLING"):
            if now - record.created_at >= self._timeout:
                failed.append(self._fail(record, fail_reason="timeout", conn=conn))
        return failed

    # -- read surface --------------------------------------------------------------

    def get(self, dossier_export_id: str) -> DossierExportRecord | None:
        return self._store.get(dossier_export_id)

    def list(
        self, *, limit: int = 50, cursor: str | None = None
    ) -> tuple[list[DossierExportRecord], str | None]:
        if limit < 1:
            raise InvalidRequestError("limit must be >= 1", code="invalid_limit")
        return self._store.list(limit=limit, cursor=cursor)

    def download(self, dossier_export_id: str) -> tuple[bytes, str]:
        """ZIP bytes + sha256 hex (spec/15 deterministic file+hash contract).

        Refused (NotFoundError, the 404 surface) until COMPLETED.
        """
        record = self._store.get(dossier_export_id)
        if record is None or record.state != "COMPLETED" or record.zip_pointer is None:
            raise NotFoundError(
                f"dossier export {dossier_export_id} has no downloadable ZIP",
                code="dossier_not_downloadable",
            )
        data = self._objects.get(record.zip_pointer)
        if data is None:
            raise NotFoundError(
                f"dossier export {dossier_export_id} object {record.zip_pointer} is missing",
                code="dossier_object_missing",
            )
        return data, _sha256_bytes(data)

    # -- member assembly (the eight evidence classes) ------------------------------

    def _build_members(self, record: DossierExportRecord) -> dict[str, bytes]:
        snapshot = record.input_snapshot
        dossier_type = record.dossier_type
        members: dict[str, bytes] = {}

        def add(path: str, content: object) -> None:
            data = (
                content
                if isinstance(content, bytes)
                else canonical_json(content)
            )
            if path in members and members[path] != data:
                raise _AssemblyError(f"member_failure: {path}: duplicate member path")
            members[path] = data

        # Class 1 — certification reports (+ BB_PHASE2 coverage gate)
        if dossier_type in ("BB_PHASE2", "SPONSOR_BANK_PACK"):
            self._add_certification_members(snapshot, dossier_type, add)
        # Class 2 — deep chain verification per snapshot range (all types)
        self._add_chain_members(snapshot, add)
        # Class 3 — trial-balance proof
        if dossier_type in ("BB_PHASE2", "SPONSOR_BANK_PACK"):
            self._add_trial_balance_member(add)
        # Class 4 — replay demonstrations (BFIU: DISPUTE_TRAIL only)
        self._add_replay_members(snapshot, dossier_type, add)
        # Class 5 — migration & retention evidence (BB_PHASE2 only)
        if dossier_type == "BB_PHASE2":
            self._add_migration_members(add)
        # Class 6 — AML policy references (BB_PHASE2 + BFIU)
        if dossier_type in ("BB_PHASE2", "BFIU_EVIDENCE"):
            add(
                "aml-policy/policy-doc-refs.json",
                {"policy_doc_refs": list(snapshot["policy_doc_refs"])},
            )
        # Class 7 — fact-correction register snapshot + unverified-claim gate
        fact_rows: list[dict] = []
        if dossier_type in ("BB_PHASE2", "SPONSOR_BANK_PACK"):
            fact_rows = self._add_fact_register_member(snapshot, add)
        # Class 8 — case evidence (BFIU only, keyed off DISPUTE_TRAIL demos)
        if dossier_type == "BFIU_EVIDENCE":
            self._add_case_evidence_members(snapshot, add)
        # LR-1 members (SPONSOR_BANK_PACK only)
        if dossier_type == "SPONSOR_BANK_PACK":
            self._add_sponsor_members(members, fact_rows, add)
        return members

    def _add_certification_members(
        self,
        snapshot: Mapping[str, object],
        dossier_type: str,
        add: Callable[[str, object], None],
    ) -> None:
        run_ids: Sequence[str] = snapshot["certification_run_ids"]  # type: ignore[assignment]
        runs: list[Mapping[str, object]] = []
        for run_id in run_ids:
            run = self._certification.get_run(run_id)
            if run is None:
                raise _AssemblyError(f"member_failure: certification/{run_id}.json: run missing")
            runs.append(run)
            add(f"certification/{run_id}.json", dict(run))
        if dossier_type == "BB_PHASE2":
            self._gate_cert_coverage(runs)

    def _gate_cert_coverage(self, runs: Sequence[Mapping[str, object]]) -> None:
        """BB_PHASE2 gate: CERT-01..10 x every registered connector, all PASSED."""
        passed: set[tuple[str, str]] = set()
        for run in runs:
            connector_id = str(run.get("connector_id", ""))
            for check in run.get("checks", ()):  # type: ignore[union-attr]
                if check.get("verdict") == "PASSED":
                    passed.add((connector_id, str(check.get("check_id"))))
        missing: list[str] = []
        for connector_id in self._certification.registered_connector_ids():
            for check_id in CERT_CHECK_IDS:
                if (connector_id, check_id) not in passed:
                    missing.append(f"{connector_id}:{check_id}")
        if missing:
            raise _AssemblyError(
                "cert_coverage_incomplete: missing PASSED verdicts for "
                + ", ".join(sorted(missing))
            )

    def _add_chain_members(
        self, snapshot: Mapping[str, object], add: Callable[[str, object], None]
    ) -> None:
        for chain_range in snapshot["chain_ranges"]:  # type: ignore[union-attr]
            chain_domain = str(chain_range["chain_domain"])
            result = dict(
                self._chain_verify.verify(
                    chain_domain,
                    int(chain_range["from_index"]),
                    int(chain_range["to_index"]),
                )
            )
            path = f"chain-verification/{chain_domain}.json"
            if result.get("status") != "COMPLETED":
                raise _AssemblyError(
                    f"chain_verification_failed: {path}: "
                    f"domain={chain_domain} status={result.get('status')}"
                )
            add(path, {"chain_domain": chain_domain, "parameters": dict(chain_range), **result})

    def _add_trial_balance_member(self, add: Callable[[str, object], None]) -> None:
        snapshot = dict(self._trial_balance.snapshot())
        if snapshot.get("balanced") is not True:
            raise _AssemblyError(
                "trial_balance_unbalanced: "
                f"debit={snapshot.get('total_debit_minor')} "
                f"credit={snapshot.get('total_credit_minor')}"
            )
        add("trial-balance/assertion.json", snapshot)

    def _add_replay_members(
        self,
        snapshot: Mapping[str, object],
        dossier_type: str,
        add: Callable[[str, object], None],
    ) -> None:
        for index, demo in enumerate(snapshot["replay_demos"]):  # type: ignore[union-attr]
            mode = str(demo["mode"])
            if dossier_type == "BFIU_EVIDENCE" and mode != "DISPUTE_TRAIL":
                continue  # BFIU evidence class 4 is DISPUTE_TRAIL only
            path = f"replay/demo-{index:03d}-{mode.lower()}.json"
            first = self._replay.run(demo)
            second = self._replay.run(demo)
            first_bytes = canonical_json(dict(first))
            if first_bytes != canonical_json(dict(second)):
                raise _AssemblyError(
                    f"replay_nondeterministic: {path}: two runs were not byte-identical"
                )
            add(path, first_bytes)

    def _add_migration_members(self, add: Callable[[str, object], None]) -> None:
        directory = self._migrations_dir
        if not directory.is_dir():
            raise _AssemblyError(
                f"member_failure: migrations/listing.json: directory {directory} missing"
            )
        files = [
            {"name": p.name, "sha256": _sha256_bytes(p.read_bytes())}
            for p in sorted(directory.glob("*.sql"), key=lambda p: p.name)
        ]
        if not files:
            raise _AssemblyError(
                "member_failure: migrations/listing.json: no migration files found"
            )
        add("migrations/listing.json", {"directory": "db/migrations", "files": files})
        add("migrations/retention-config.json", dict(self._retention_config))

    def _add_fact_register_member(
        self, snapshot: Mapping[str, object], add: Callable[[str, object], None]
    ) -> list[dict]:
        as_of = str(snapshot["fact_register_as_of"])
        rows = [dict(r) for r in self._fact_register.rows_as_of(as_of)]
        for row in rows:
            if row.get("external_blocking") is True and row.get("status") != "VERIFIED":
                raise _AssemblyError(f"unverified_claim: {row.get('correction_id')}")
        add("fact-register/snapshot.json", {"as_of": as_of, "rows": rows})
        return rows

    def _add_case_evidence_members(
        self, snapshot: Mapping[str, object], add: Callable[[str, object], None]
    ) -> None:
        seen: set[str] = set()
        for demo in snapshot["replay_demos"]:  # type: ignore[union-attr]
            if demo["mode"] != "DISPUTE_TRAIL":
                continue
            payment_intent_id = str(demo["payment_intent_id"])
            if payment_intent_id in seen:
                continue
            seen.add(payment_intent_id)
            evidence = [dict(e) for e in self._case_evidence.evidence_for(payment_intent_id)]
            add(
                f"case-evidence/{payment_intent_id}.json",
                {"payment_intent_id": payment_intent_id, "evidence": evidence},
            )

    def _add_sponsor_members(
        self,
        members: Mapping[str, bytes],
        fact_rows: Sequence[Mapping[str, object]],
        add: Callable[[str, object], None],
    ) -> None:
        if self._sponsor_pack is None:
            # Workstream-1 seam: the pack generator is another lane's deliverable.
            raise _AssemblyError("sponsor_pack_generator_unavailable")
        verified = {
            str(row.get("correction_id"))
            for row in fact_rows
            if row.get("status") == "VERIFIED"
        }
        add(
            "term-sheet-checklist.md",
            self._sponsor_pack.term_sheet_checklist_md().encode("utf-8"),
        )
        claims: list[dict] = []
        for raw in self._sponsor_pack.claims_rows():
            row = dict(raw)
            fcor_id = row.get("fact_correction_id")
            if fcor_id is not None and str(fcor_id) not in verified:
                raise _AssemblyError(f"unverified_claim: {fcor_id}")
            artifact_path = str(row.get("artifact_path_in_zip", ""))
            artifact = members.get(artifact_path)
            if artifact is None and artifact_path != "term-sheet-checklist.md":
                raise _AssemblyError(
                    "member_failure: claims-evidence-map.json: "
                    f"artifact {artifact_path!r} is not a manifest member"
                )
            computed = _sha256_bytes(
                artifact
                if artifact is not None
                else self._sponsor_pack.term_sheet_checklist_md().encode("utf-8")
            )
            declared = row.get("artifact_sha256")
            if declared not in (None, "", computed):
                raise _AssemblyError(
                    "member_failure: claims-evidence-map.json: "
                    f"artifact_sha256 mismatch for {artifact_path!r}"
                )
            row["artifact_sha256"] = computed
            claims.append(row)
        add("claims-evidence-map.json", {"claims": claims})

    # -- manifest + per-member recompute gate --------------------------------------

    @staticmethod
    def _build_manifest(
        dossier_export_id: str,
        input_snapshot: Mapping[str, object],
        members: Mapping[str, bytes],
    ) -> tuple[dict, bytes, str]:
        rows = [
            {"path": path, "sha256": _sha256_bytes(members[path]), "bytes": len(members[path])}
            for path in sorted(members)
        ]
        dossier_hash = sha256_canonical(rows)
        manifest = {
            "dossier_export_id": dossier_export_id,
            "input_snapshot": dict(input_snapshot),
            "members": rows,
            "dossier_hash": dossier_hash,
        }
        return manifest, canonical_json(manifest), dossier_hash

    @staticmethod
    def _verify_sealed_zip(zip_bytes: bytes, manifest: Mapping[str, object]) -> None:
        """The spec's per-member gate: every sealed member's recomputed
        SHA-256 must equal its manifest row — verified against the actual
        archive bytes, never the in-memory build."""
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
            names = archive.namelist()
            if names[0] != "manifest.json":
                raise _AssemblyError("member_failure: manifest.json: not the first member")
            rows: Sequence[Mapping[str, object]] = manifest["members"]  # type: ignore[assignment]
            if sorted(names[1:]) != [row["path"] for row in rows]:
                raise _AssemblyError(
                    "member_failure: manifest.json: member list does not match the archive"
                )
            for row in rows:
                data = archive.read(str(row["path"]))
                if _sha256_bytes(data) != row["sha256"] or len(data) != row["bytes"]:
                    raise _AssemblyError(
                        f"member_failure: {row['path']}: sha256 recompute mismatch"
                    )

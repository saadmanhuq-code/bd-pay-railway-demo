"""PSO_DISPUTE_BUNDLE exporter — hash-manifested dispute evidence ZIP (spec/19 PSO-3).

Reuses the spec/16 dossier-export pattern (``bdpay/compliance/dossier.py``)
verbatim: a deterministic archive whose FIRST member is ``manifest.json``
listing every other member ``{path, sha256, bytes}`` plus a ``dossier_hash``
over the canonical members list; members sorted by path; all ZIP timestamps
pinned to 1980-01-01; deflate level 6; ``canonical_json`` member bytes; the
sealed archive is re-opened and every member's SHA-256 recomputed before the
row completes. Re-export over the same ``input_snapshot`` is byte-identical.

The spec/16 module hard-pins its three dossier types and its own
``input_snapshot`` schema, so this assembler ships here in the replay
package implementing the SAME FSM (QUEUED -> ASSEMBLING -> COMPLETED|FAILED,
refusal-first, terminal rows immutable), the same determinism rules, and the
same audit/outbox side-effects; rows land in the spec/16 ``dossier_exports``
table (migration 0103 widens its ``dossier_type`` CHECK) — errata E-S19R-06.

PLATFORM_MODE gate: the bundle is a PSO operation; ``queue``/``assemble``
raise :class:`bdpay.ledger.errors.PsoModeError` under ``PLATFORM_MODE=PSP``
before anything is read or written (PSO-CERT-07 surface).

Refusal-first gates: a window whose WINDOW_REPLAY verdict is not ``PASSED``
NEVER produces a clean evidence bundle — byte mismatch or a broken chain is
treated as potential tamper (spec/19 Failure modes) and the export FAILS
with the named reason.
"""

from __future__ import annotations

import dataclasses
import hashlib
import io
import zipfile
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from bdpay.ledger.errors import PsoModeError
from bdpay.ledger.replay.errors import (
    BundleAssemblyError,
    ReplayError,
    ReplayInvalidTransitionError,
    ReplayNotFoundError,
)
from bdpay.ledger.replay.facts import ObjectStorePort, WindowFactsPort
from bdpay.ledger.replay.ids import make_replay_id
from bdpay.ledger.replay.types import AttributedEntry
from bdpay.platform.canonical import CanonicalError, canonical_json, sha256_canonical
from bdpay.platform.config import Settings
from bdpay.platform.interfaces import AuditEventSpec, AuditPort, OutboxPort
from bdpay.platform.outbox import build_event

if TYPE_CHECKING:
    from bdpay.ledger.replay.service import ReplayService
    from bdpay.platform.clock import Clock

__all__ = [
    "DOSSIER_TYPE",
    "PSO_DISPUTE_BUNDLE_MEMBERS",
    "DisputeBundleExporter",
    "DisputeBundleRecord",
    "DisputeBundleStore",
    "InMemoryDisputeBundleStore",
]

PRODUCER = "ledger-service@1"
DOSSIER_TYPE = "PSO_DISPUTE_BUNDLE"

_STATES = ("QUEUED", "ASSEMBLING", "COMPLETED", "FAILED")
_TERMINAL = frozenset({"COMPLETED", "FAILED"})

_SNAPSHOT_KEYS = ("settlement_window_id", "payment_intent_ids", "journal_entry_ids")

#: The spec/19 member table (manifest.json is always the first archive member).
PSO_DISPUTE_BUNDLE_MEMBERS: tuple[str, ...] = (
    "window-summary.json",
    "disputed-transactions.json",
    "positions-derivation.json",
    "netting-result.json",
    "netting-replay.json",
    "chain-verification.json",
    "instructions.json",
    "replay-command.txt",
)


@dataclasses.dataclass(frozen=True, slots=True)
class DisputeBundleRecord:
    """One ``dossier_exports`` row with ``dossier_type='PSO_DISPUTE_BUNDLE'``."""

    dossier_export_id: str  # dxp_<sha256_canonical({dossier_type, input_snapshot})[:24]>
    input_snapshot: Mapping[str, object]
    state: str
    requested_by: str
    created_at: datetime
    dossier_type: str = DOSSIER_TYPE
    manifest: Mapping[str, object] | None = None
    dossier_hash: str | None = None
    zip_pointer: str | None = None
    fail_reason: str | None = None
    completed_at: datetime | None = None
    schema_version: int = 1

    @property
    def is_terminal(self) -> bool:
        return self.state in _TERMINAL


@runtime_checkable
class DisputeBundleStore(Protocol):
    """Storage contract (dossier_exports semantics; spec/16 FSM discipline)."""

    def insert(self, record: DisputeBundleRecord) -> None: ...

    def get(self, dossier_export_id: str) -> DisputeBundleRecord | None: ...

    def begin_assembly(self, dossier_export_id: str) -> DisputeBundleRecord | None: ...

    def save(self, record: DisputeBundleRecord) -> None: ...

    def completed_for_participant(self, participant_id: str) -> list[DisputeBundleRecord]: ...


class InMemoryDisputeBundleStore:
    """Deterministic in-memory store mirroring the dossier_exports posture."""

    def __init__(self) -> None:
        self._rows: dict[str, DisputeBundleRecord] = {}
        self._order: list[str] = []
        self._claimed: set[str] = set()

    def insert(self, record: DisputeBundleRecord) -> None:
        if record.dossier_export_id in self._rows:
            raise ReplayInvalidTransitionError(
                f"dispute bundle {record.dossier_export_id} already exists"
            )
        self._rows[record.dossier_export_id] = record
        self._order.append(record.dossier_export_id)

    def get(self, dossier_export_id: str) -> DisputeBundleRecord | None:
        return self._rows.get(dossier_export_id)

    def begin_assembly(self, dossier_export_id: str) -> DisputeBundleRecord | None:
        record = self._rows.get(dossier_export_id)
        if record is None or record.state != "QUEUED" or dossier_export_id in self._claimed:
            return None
        self._claimed.add(dossier_export_id)
        updated = dataclasses.replace(record, state="ASSEMBLING")
        self._rows[dossier_export_id] = updated
        return updated

    def save(self, record: DisputeBundleRecord) -> None:
        existing = self._rows.get(record.dossier_export_id)
        if existing is None:
            raise ReplayNotFoundError(f"unknown dispute bundle {record.dossier_export_id}")
        if existing.is_terminal:
            raise ReplayInvalidTransitionError(
                f"dispute bundle {record.dossier_export_id} is terminal "
                f"({existing.state}); terminal rows are immutable"
            )
        self._rows[record.dossier_export_id] = record

    def completed_for_participant(self, participant_id: str) -> list[DisputeBundleRecord]:
        out: list[DisputeBundleRecord] = []
        for dossier_export_id in self._order:
            record = self._rows[dossier_export_id]
            if record.state != "COMPLETED":
                continue
            named = record.input_snapshot.get("participant_ids", [])
            if isinstance(named, Sequence) and participant_id in named:
                out.append(record)
        return out


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _seal_zip(manifest_bytes: bytes, members: Mapping[str, bytes]) -> bytes:
    """Seal per the binding spec/16 determinism rules (pattern reuse)."""
    buffer = io.BytesIO()
    ordered = [("manifest.json", manifest_bytes)]
    ordered.extend(sorted(members.items()))
    with zipfile.ZipFile(buffer, "w") as archive:
        for path, data in ordered:
            info = zipfile.ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16  # plain file, rw-r--r--, consistent
            info.create_system = 3  # unix, pinned (host-OS independence)
            archive.writestr(info, data, compresslevel=6)
    return buffer.getvalue()


def validate_input_snapshot(snapshot: Mapping[str, object]) -> dict[str, object]:
    """Binding §API C snapshot: names the window + the disputed ids."""
    if not isinstance(snapshot, Mapping):
        raise ReplayError("input_snapshot must be an object")
    if set(snapshot.keys()) - {*_SNAPSHOT_KEYS, "participant_ids"}:
        raise ReplayError(
            f"input_snapshot keys must be {sorted(_SNAPSHOT_KEYS)} (+ optional "
            f"participant_ids), got {sorted(snapshot.keys())}"
        )
    for key in _SNAPSHOT_KEYS:
        if key not in snapshot:
            raise ReplayError(f"input_snapshot.{key} is required")
    window_id = snapshot["settlement_window_id"]
    if not isinstance(window_id, str) or not window_id:
        raise ReplayError("input_snapshot.settlement_window_id must be a non-empty string")
    for key in ("payment_intent_ids", "journal_entry_ids", "participant_ids"):
        values = snapshot.get(key, [])
        if not isinstance(values, Sequence) or isinstance(values, str | bytes):
            raise ReplayError(f"input_snapshot.{key} must be a list of strings")
        for i, value in enumerate(values):
            if not isinstance(value, str) or not value:
                raise ReplayError(f"input_snapshot.{key}[{i}] must be a non-empty string")
    if not snapshot["payment_intent_ids"] and not snapshot["journal_entry_ids"]:
        raise ReplayError(
            "input_snapshot must name at least one disputed payment_intent_id "
            "or journal_entry_id"
        )
    plain: dict[str, object] = {
        "settlement_window_id": window_id,
        "payment_intent_ids": list(snapshot["payment_intent_ids"]),  # type: ignore[call-overload]
        "journal_entry_ids": list(snapshot["journal_entry_ids"]),  # type: ignore[call-overload]
    }
    if "participant_ids" in snapshot:
        plain["participant_ids"] = list(snapshot["participant_ids"])  # type: ignore[call-overload]
    try:
        canonical_json(plain)
    except CanonicalError as exc:
        raise ReplayError(f"input_snapshot is not canonically serializable: {exc}") from exc
    return plain


class DisputeBundleExporter:
    """Queue + worker + read surface for PSO_DISPUTE_BUNDLE exports.

    Refusal-first orchestration: every gate failure turns the export FAILED
    with a named reason — never a partial ZIP (spec/16 Failure Modes,
    reused).
    """

    def __init__(
        self,
        store: DisputeBundleStore,
        *,
        clock: Clock,
        settings: Settings,
        audit: AuditPort,
        outbox: OutboxPort,
        object_store: ObjectStorePort,
        replay: ReplayService,
        facts: WindowFactsPort,
        object_store_prefix: str = "objstore://dossiers/",
        producer: str = PRODUCER,
    ) -> None:
        self._store = store
        self._clock = clock
        self._settings = settings
        self._audit = audit
        self._outbox = outbox
        self._objects = object_store
        self._replay = replay
        self._facts = facts
        self._objstore_prefix = object_store_prefix
        self._producer = producer

    def _require_pso(self) -> None:
        if self._settings.platform_mode != "PSO":
            raise PsoModeError(
                f"PSO_DISPUTE_BUNDLE is a PSO-only operation; refused under "
                f"PLATFORM_MODE={self._settings.platform_mode!r}"
            )

    # -- queue (202 semantics; idempotent by content-addressed id) ----------

    def queue(
        self, *, input_snapshot: Mapping[str, object], requested_by: str
    ) -> DisputeBundleRecord:
        self._require_pso()
        if not requested_by:
            raise ReplayError("requested_by is required")
        snapshot = validate_input_snapshot(input_snapshot)
        dossier_export_id = make_replay_id(
            "dxp", {"dossier_type": DOSSIER_TYPE, "input_snapshot": snapshot}
        )
        existing = self._store.get(dossier_export_id)
        if existing is not None:
            # Content-addressed idempotency: a FAILED row is returned as-is so
            # the operator sees the reason (spec/16 FSM retry note).
            return existing
        record = DisputeBundleRecord(
            dossier_export_id=dossier_export_id,
            input_snapshot=snapshot,
            state="QUEUED",
            requested_by=requested_by,
            created_at=self._clock.now(),
        )
        self._store.insert(record)
        return record

    # -- worker ----------------------------------------------------------------

    def assemble(self, dossier_export_id: str) -> DisputeBundleRecord:
        """worker_pickup + assembled/assembly_error in one worker pass."""
        self._require_pso()
        record = self._store.get(dossier_export_id)
        if record is None:
            raise ReplayNotFoundError(f"unknown dispute bundle {dossier_export_id}")
        claimed = self._store.begin_assembly(dossier_export_id)
        if claimed is None:
            raise ReplayInvalidTransitionError(
                f"transition 'worker_pickup' from state {record.state!r} is DENIED "
                "(refusal-first)"
            )
        self._audit.append(
            AuditEventSpec(
                event_type="DOSSIER_ASSEMBLY_STARTED",
                actor_id=self._producer,
                subject_type="DossierExport",
                subject_id=dossier_export_id,
                from_state="QUEUED",
                to_state="ASSEMBLING",
                payload={"dossier_type": DOSSIER_TYPE},
            ),
            clock=self._clock,
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
        except BundleAssemblyError as exc:
            return self._fail(claimed, fail_reason=exc.fail_reason)
        except Exception as exc:  # refusal-first: any member failure FAILS the export
            return self._fail(
                claimed, fail_reason=f"member_failure: {type(exc).__name__}: {exc}"
            )
        completed = dataclasses.replace(
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
                actor_id=self._producer,
                subject_type="DossierExport",
                subject_id=dossier_export_id,
                from_state="ASSEMBLING",
                to_state="COMPLETED",
                payload={"dossier_hash": dossier_hash, "member_count": len(members)},
            ),
            clock=self._clock,
        )
        self._outbox.enqueue(
            build_event(
                event_type="dossier_export.completed",
                subject_type="DossierExport",
                subject_id=dossier_export_id,
                producer=self._producer,
                topic="aml.events",
                payload={
                    "dossier_export_id": dossier_export_id,
                    "dossier_type": DOSSIER_TYPE,
                    "dossier_hash": dossier_hash,
                },
                occurred_at=self._clock.now(),
            )
        )
        return completed

    def _fail(self, record: DisputeBundleRecord, *, fail_reason: str) -> DisputeBundleRecord:
        failed = dataclasses.replace(
            record, state="FAILED", fail_reason=fail_reason, completed_at=self._clock.now()
        )
        self._store.save(failed)
        self._audit.append(
            AuditEventSpec(
                event_type="DOSSIER_ASSEMBLY_FAILED",
                actor_id=self._producer,
                subject_type="DossierExport",
                subject_id=record.dossier_export_id,
                from_state="ASSEMBLING",
                to_state="FAILED",
                payload={"fail_reason": fail_reason},
            ),
            clock=self._clock,
        )
        self._outbox.enqueue(
            build_event(
                event_type="dossier_export.failed",
                subject_type="DossierExport",
                subject_id=record.dossier_export_id,
                producer=self._producer,
                topic="aml.events",
                payload={
                    "dossier_export_id": record.dossier_export_id,
                    "dossier_type": DOSSIER_TYPE,
                    "fail_reason": fail_reason,
                },
                occurred_at=self._clock.now(),
            )
        )
        return failed

    # -- read surface ------------------------------------------------------------

    def get(self, dossier_export_id: str) -> DisputeBundleRecord | None:
        return self._store.get(dossier_export_id)

    def download(self, dossier_export_id: str) -> tuple[bytes, str]:
        """ZIP bytes + sha256 hex (spec/15 deterministic file+hash contract)."""
        record = self._store.get(dossier_export_id)
        if record is None or record.state != "COMPLETED" or record.zip_pointer is None:
            raise ReplayNotFoundError(
                f"dispute bundle {dossier_export_id} has no downloadable ZIP"
            )
        data = self._objects.get(record.zip_pointer)
        if data is None:
            raise ReplayNotFoundError(
                f"dispute bundle {dossier_export_id} object {record.zip_pointer} is missing"
            )
        return data, _sha256_bytes(data)

    # -- member assembly (the spec/19 PSO_DISPUTE_BUNDLE member table) -----------

    def _build_members(self, record: DisputeBundleRecord) -> dict[str, bytes]:
        snapshot = record.input_snapshot
        window_id = str(snapshot["settlement_window_id"])
        facts = self._facts

        # Gate: the window must replay clean — byte mismatch / broken chain is
        # potential tamper, never a clean evidence bundle (spec/19 Failure modes).
        try:
            report = self._replay.execute_window_replay(window_id)
        except ReplayError as exc:
            raise BundleAssemblyError(f"replay_unavailable: {exc}") from exc
        if not report.chain_deep_verified:
            raise BundleAssemblyError(
                f"chain_verification_failed: window {window_id} deep verify is not green"
            )
        if report.verdict != "PASSED":
            kinds = ", ".join(sorted({str(d.get("kind")) for d in report.discrepancies}))
            raise BundleAssemblyError(
                f"replay_verification_failed: window {window_id} verdict FAILED ({kinds})"
            )

        window = facts.get_window(window_id)
        if window is None:
            raise BundleAssemblyError(f"member_failure: window-summary.json: {window_id} missing")
        stored_bytes = self._stored_result_bytes(window_id)

        members: dict[str, bytes] = {}

        def add(path: str, content: object) -> None:
            data = content if isinstance(content, bytes) else canonical_json(content)
            members[path] = data

        # 2 — window-summary.json
        add(
            "window-summary.json",
            {
                "settlement_window_id": window_id,
                "dns_session": window["dns_session"],
                "window_date": window["window_date"],
                "state_history": facts.window_audit_history(window_id),
                "netting_run_id": report.netting_run_id,
                "participant_count": len(report.positions),
            },
        )
        # 3 — disputed-transactions.json (PII-free by construction)
        add("disputed-transactions.json", self._disputed_transactions(snapshot))
        # 4 — positions-derivation.json (WINDOW_REPLAY step 1 output)
        add(
            "positions-derivation.json",
            {
                "settlement_window_id": window_id,
                "positions": [
                    {
                        "participant_id": p.participant_id,
                        "net_position_minor": p.net_position_minor,
                        "contributing_entry_ids": list(p.contributing_entry_ids),
                    }
                    for p in report.positions
                ],
            },
        )
        # 5 — netting-result.json (stored file verbatim) + netting-replay.json
        add("netting-result.json", stored_bytes)
        add(
            "netting-replay.json",
            {
                "recomputed_document": dict(report.recomputed_document),
                "recomputed_result_hash": report.recomputed_result_hash,
                "stored_result_hash": report.stored_result_hash,
                "result_bytes_identical": report.result_bytes_identical,
                "verdict": report.verdict,
            },
        )
        # 6 — chain-verification.json (E5 deep verify over the window range)
        add(
            "chain-verification.json",
            {
                "chain_domain": "MONEY",
                "from_index": report.chain_from_index,
                "to_index": report.chain_to_index,
                "deep": True,
                "chain_deep_verified": report.chain_deep_verified,
                "entries_in_range": report.chain_to_index - report.chain_from_index + 1,
            },
        )
        # 7 — instructions.json (raw_response_hash pointers only, never raw)
        add(
            "instructions.json",
            {
                "netting_run_id": report.netting_run_id,
                "instructions": [
                    {key: value for key, value in dict(row).items() if key != "raw_response"}
                    for row in facts.netting_instructions(report.netting_run_id)
                ],
            },
        )
        # 8 — replay-command.txt (spec/16 evidence-class-4 pattern)
        add(
            "replay-command.txt",
            (
                "bdpay-replay --mode WINDOW_REPLAY"
                f" --settlement-window-id {window_id}"
                f" --expect-result-hash {report.stored_result_hash}\n"
            ).encode(),
        )
        return members

    def _stored_result_bytes(self, window_id: str) -> bytes:
        run = self._facts.latest_netting_run(window_id)
        if run is None or run.result_pointer is None:
            raise BundleAssemblyError(
                f"member_failure: netting-result.json: no sealed run for {window_id}"
            )
        data = self._facts.result_file(run.result_pointer)
        if data is None:
            raise BundleAssemblyError(
                f"member_failure: netting-result.json: {run.result_pointer} missing"
            )
        return data

    def _disputed_transactions(self, snapshot: Mapping[str, object]) -> dict[str, object]:
        facts = self._facts
        window_id = str(snapshot["settlement_window_id"])
        seen: set[str] = set()
        transactions: list[dict[str, object]] = []

        def add_entry(attributed: AttributedEntry) -> None:
            entry = attributed.entry
            if entry.entry_id in seen:
                return
            seen.add(entry.entry_id)
            transactions.append(
                {
                    "entry_id": entry.entry_id,
                    "entry_index": entry.entry_index,
                    "reference_id": entry.reference_id,
                    "reference_type": entry.reference_type,
                    "entry_type": entry.entry_type,
                    "postings": [
                        {
                            "posting_id": p.posting_id,
                            "account_id": p.account_id,
                            "side": p.side,
                            "amount_minor": p.amount_minor,
                            "currency": p.currency,
                        }
                        for p in attributed.postings
                    ],
                }
            )

        payment_intent_ids: Sequence[str] = snapshot["payment_intent_ids"]  # type: ignore[assignment]
        for payment_intent_id in payment_intent_ids:
            found = facts.entries_for_reference(payment_intent_id)
            if not found:
                raise BundleAssemblyError(
                    "member_failure: disputed-transactions.json: "
                    f"no ledger entries for payment {payment_intent_id}"
                )
            for attributed in found:
                add_entry(attributed)
        journal_entry_ids: Sequence[str] = snapshot["journal_entry_ids"]  # type: ignore[assignment]
        if journal_entry_ids:
            by_entry_id = {a.entry.entry_id: a for a in facts.window_entries(window_id)}
            for entry_id in journal_entry_ids:
                attributed_entry = by_entry_id.get(entry_id)
                if attributed_entry is None:
                    raise BundleAssemblyError(
                        "member_failure: disputed-transactions.json: "
                        f"entry {entry_id} is not in the window"
                    )
                add_entry(attributed_entry)
        transactions.sort(key=lambda t: int(t["entry_index"]))  # type: ignore[arg-type]
        return {"transactions": transactions}

    # -- manifest + per-member recompute gate (spec/16 rules verbatim) -----------

    @staticmethod
    def _build_manifest(
        dossier_export_id: str,
        input_snapshot: Mapping[str, object],
        members: Mapping[str, bytes],
    ) -> tuple[dict[str, object], bytes, str]:
        rows = [
            {"path": path, "sha256": _sha256_bytes(members[path]), "bytes": len(members[path])}
            for path in sorted(members)
        ]
        dossier_hash = sha256_canonical(rows)
        manifest: dict[str, object] = {
            "dossier_export_id": dossier_export_id,
            "dossier_type": DOSSIER_TYPE,
            "input_snapshot": dict(input_snapshot),
            "members": rows,
            "dossier_hash": dossier_hash,
        }
        return manifest, canonical_json(manifest), dossier_hash

    @staticmethod
    def _verify_sealed_zip(zip_bytes: bytes, manifest: Mapping[str, object]) -> None:
        """Per-member gate: every sealed member's recomputed SHA-256 must
        equal its manifest row — verified against the actual archive bytes,
        never the in-memory build."""
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
            names = archive.namelist()
            if names[0] != "manifest.json":
                raise BundleAssemblyError("member_failure: manifest.json: not the first member")
            rows: Sequence[Mapping[str, object]] = manifest["members"]  # type: ignore[assignment]
            if sorted(names[1:]) != [row["path"] for row in rows]:
                raise BundleAssemblyError(
                    "member_failure: manifest.json: member list does not match the archive"
                )
            for row in rows:
                data = archive.read(str(row["path"]))
                if _sha256_bytes(data) != row["sha256"] or len(data) != row["bytes"]:
                    raise BundleAssemblyError(
                        f"member_failure: {row['path']}: sha256 recompute mismatch"
                    )

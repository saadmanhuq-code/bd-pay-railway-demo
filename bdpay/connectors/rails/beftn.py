"""``beftn_batch_v2`` — BEFTN settlement-file connector (spec/11 §B).

Implements the frozen ``sdk.SettlementFileConnector``: renders the
fixed-width or2020 file (write-then-verify parse-back before SEAL), enforces
the 3-sessions/day cutoff awareness (12:30 Asia/Dhaka same-day rule via the
injected calendar — the platform scheduler owns the windows at integration),
uploads file + detached signature over the SFTP transport port, and ingests
confirmation CSVs for reconciliation (return-code table §B.3; Talent_1.0
recon-loop pitfalls ported: statuses normalized, exact integer-paisa amount
matching — a paid-claim with a mismatched amount is a FAILED row, unknown
traces are recon exceptions, never silently dropped).

Connector discipline: idempotent on ``batch_id`` (exactly one SFTP upload per
unique batch; replays return the recorded outcome), echoes ``batch_id`` +
``session``, archives every bank response content-addressed
(``raw_response_hash`` via the E12 canonical hash), never writes business DB
tables. Validation failure of ANY entry refuses the WHOLE batch back to the
settlement-engine with the per-entry code list — no partial silent drop.

Modes: SIMULATOR wires the in-process
:class:`~bdpay.connectors.simulators.beftn_sim.BeftnSimulatorBank`; SANDBOX /
PRODUCTION wire :class:`AsyncsshSftpTransport` (real SFTP wire code, fully
written — activation needs host + key credentials config). File signing is
the :class:`FileSigner` seam: PGP detached signature (``pgpy``) for the real
gateway per spec/11, HMAC detached signature in SIMULATOR mode.
"""

from __future__ import annotations

import csv
import hmac
import io
import json
import threading
from base64 import b64decode, b64encode
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from hashlib import sha256

from bdpay.connectors.ids_ext import make_connector_id
from bdpay.connectors.ports import (
    CredentialResolver,
    EventSink,
    InMemoryEventSink,
    InMemoryObjectStore,
    ObjectStore,
    raw_object_key,
)
from bdpay.connectors.rails.beftn_layout import (
    BeftnEntryRow,
    BeftnFileConfig,
    BeftnParseError,
    beftn_file_name,
    layout_hash,
    parse_file,
    render_file,
    validate_entry,
)
from bdpay.connectors.rails.ports import BEFTN_SESSIONS, BdCalendar, Detokenizer
from bdpay.platform.bangla import normalize_bengali_digits
from bdpay.platform.canonical import canonical_json, sha256_canonical
from bdpay.platform.clock import Clock

__all__ = [
    "AsyncsshSftpTransport",
    "BeftnBatchConnector",
    "BeftnReconReport",
    "CONNECTOR_ID",
    "FileSigner",
    "HmacFileSigner",
    "PgpFileSigner",
    "RETURN_CODE_MAP",
    "SimulatorSftpTransport",
    "return_error_code",
]

CONNECTOR_ID = "beftn_batch_v2"

#: Return/dishonor codes -> canonical error_code (spec/11 §B.3, binding).
RETURN_CODE_MAP: dict[str, str] = {
    "R01": "beftn_return_nsf",
    "R02": "beftn_return_account_closed",
    "R03": "beftn_return_no_account",
    "R04": "beftn_return_invalid_account",
    "R06": "beftn_return_odfi_request",
    "R07": "beftn_return_auth_revoked",
    "R08": "beftn_return_stopped",
    "R09": "beftn_return_uncollected",
    "R10": "beftn_return_unauthorized",
    "R16": "beftn_return_frozen",
    "R20": "beftn_return_nontxn_account",
    "R29": "beftn_return_corp_unauthorized",
}

#: Returns are honoured for 2 processing sessions after settlement (B.3).
RETURN_WINDOW_SESSIONS = 2


def return_error_code(return_code: str) -> str:
    return RETURN_CODE_MAP.get(return_code, f"beftn_return_{return_code}")


# -- transports -----------------------------------------------------------------------


class SimulatorSftpTransport:
    """SFTP port over the in-process simulator bank (SIMULATOR mode)."""

    def __init__(self, bank) -> None:
        self._bank = bank

    async def put(self, remote_path: str, data: bytes) -> None:
        await self._bank.put(remote_path, data)

    async def get(self, remote_path: str) -> bytes:
        return await self._bank.get(remote_path)

    async def listdir(self, remote_dir: str) -> list[str]:
        return await self._bank.listdir(remote_dir)


class AsyncsshSftpTransport:
    """Real sponsor-bank SFTP gateway transport (SANDBOX/PRODUCTION wire code).

    Connects lazily with ``asyncssh`` using host/port/username from config and
    the private key resolved from OpenBao-shaped credential refs; the server
    host key is pinned via ``known_hosts_ref`` (fail-closed: connection is
    refused when the pin is absent). Activation needs only credentials config.
    """

    def __init__(
        self,
        *,
        host: str,
        port: int,
        username: str,
        credential_resolver: CredentialResolver,
        private_key_ref: str,
        known_hosts_ref: str,
    ) -> None:
        self._host = host
        self._port = port
        self._username = username
        self._resolver = credential_resolver
        self._private_key_ref = private_key_ref
        self._known_hosts_ref = known_hosts_ref
        self._conn = None
        self._sftp = None

    async def _client(self):
        if self._sftp is not None:
            return self._sftp
        import asyncssh

        private_key = self._resolver.resolve(self._private_key_ref)
        known_hosts = self._resolver.resolve(self._known_hosts_ref)
        self._conn = await asyncssh.connect(
            self._host,
            port=self._port,
            username=self._username,
            client_keys=[asyncssh.import_private_key(private_key)],
            known_hosts=asyncssh.import_known_hosts(known_hosts),
        )
        self._sftp = await self._conn.start_sftp_client()
        return self._sftp

    async def put(self, remote_path: str, data: bytes) -> None:
        sftp = await self._client()
        async with sftp.open(remote_path, "wb") as handle:
            await handle.write(data)

    async def get(self, remote_path: str) -> bytes:
        sftp = await self._client()
        async with sftp.open(remote_path, "rb") as handle:
            return await handle.read()

    async def listdir(self, remote_dir: str) -> list[str]:
        sftp = await self._client()
        names = await sftp.listdir(remote_dir)
        return sorted(f"{remote_dir.rstrip('/')}/{n}" for n in names if n not in (".", ".."))

    async def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            await self._conn.wait_closed()
            self._conn = None
            self._sftp = None


# -- file signing seam -------------------------------------------------------------------


class FileSigner:
    """Detached-signature seam (PGP per spec/11; key custody is OpenBao)."""

    def sign(self, data: bytes) -> bytes:
        raise LookupError("no signing key configured (fail-closed)")


class HmacFileSigner(FileSigner):
    """Deterministic HMAC-SHA256 detached signature (SIMULATOR mode)."""

    def __init__(self, key: bytes) -> None:
        self._key = key

    def sign(self, data: bytes) -> bytes:
        return hmac.new(self._key, data, sha256).hexdigest().encode("ascii")


class PgpFileSigner(FileSigner):
    """PGP detached signature via ``pgpy`` (SANDBOX/PRODUCTION wire code).

    The ASCII-armored private key comes from the OpenBao-shaped resolver
    (``openbao:secret/connectors/beftn/pgp_signing`` per spec/11 B.1).
    """

    def __init__(self, credential_resolver: CredentialResolver, key_ref: str) -> None:
        self._resolver = credential_resolver
        self._key_ref = key_ref

    def sign(self, data: bytes) -> bytes:
        import pgpy

        key, _ = pgpy.PGPKey.from_blob(self._resolver.resolve(self._key_ref))
        signature = key.sign(pgpy.PGPMessage.new(data), detached=True)
        return str(signature).encode("ascii")


# -- recon report -------------------------------------------------------------------------


@dataclass(frozen=True)
class BeftnReturnRow:
    """One parsed return row + its disposition."""

    return_id: str
    original_trace_number: str
    return_code: str
    error_code: str
    amount_minor: int
    received_in_session: int
    entry_ref: str | None  # None => unmatched (recon exception)
    amount_mismatch: bool
    late_return: bool


@dataclass(frozen=True)
class BeftnReconReport:
    """Confirmation-CSV ingestion outcome (input to spec/04 reconciliation)."""

    matched: tuple[BeftnReturnRow, ...]
    unmatched: tuple[BeftnReturnRow, ...]
    late: tuple[BeftnReturnRow, ...]
    raw_response_hash: str


@dataclass
class _BatchRecord:
    batch_id: str
    session: str
    effective_date: str
    file_name: str
    file_content_hash: str
    rendered: bytes
    signature: bytes
    uploaded: bool
    entry_refs_by_trace: dict[str, str]
    amounts_by_trace: dict[str, int]
    settled_in_session: int | None
    outcome: dict


class BeftnBatchConnector:
    """``sdk.SettlementFileConnector`` for the BEFTN rail (all modes)."""

    connector_id = CONNECTOR_ID

    #: Single-flight upload claim lease (review finding
    #: beftn-dispatch-race-double-uploads). Generous vs. a real SFTP put so a
    #: healthy in-flight upload is never pre-empted; long enough past any
    #: sane RPC timeout that a crashed holder's claim still self-heals on the
    #: next attempt instead of wedging the batch forever.
    _UPLOAD_LEASE_SECONDS = 60.0

    def __init__(
        self,
        *,
        transport,
        clock: Clock,
        detokenizer: Detokenizer,
        signer: FileSigner,
        config: BeftnFileConfig,
        calendar: BdCalendar | None = None,
        object_store: ObjectStore | None = None,
        events: EventSink | None = None,
        session_counter=None,
    ) -> None:
        self._transport = transport
        self._clock = clock
        self._detokenizer = detokenizer
        self._signer = signer
        self._config = config
        self._calendar = calendar if calendar is not None else BdCalendar()
        self._objects = object_store if object_store is not None else InMemoryObjectStore()
        self._events = events if events is not None else InMemoryEventSink()
        self._session_counter = session_counter if session_counter is not None else (lambda: 0)
        self._layout_hash = layout_hash()
        self._batches: dict[str, _BatchRecord] = {}
        self._content_hashes: set[str] = set()
        self._uploads: dict[str, int] = {}
        self._files_today: dict[str, int] = {}
        # Guards _try_claim_upload's check-then-write against BOTH concurrent
        # asyncio tasks on one event loop AND real OS-thread concurrency (two
        # dispatchers, each driving its own event loop via asyncio.run() in
        # its own thread — the shape the repo's existing dispatch-race tests
        # use). A plain "no await in between" is only atomic within a single
        # event loop; the lock makes the claim atomic either way.
        self._claim_lock = threading.Lock()

    # -- helpers ------------------------------------------------------------------------

    @staticmethod
    def _record_key(batch_id: str) -> str:
        return f"beftn_batch_v2/batches/{batch_id}.json"

    def _archive(self, raw: dict) -> str:
        raw_hash = sha256_canonical(raw)
        self._objects.put(raw_object_key(CONNECTOR_ID, raw_hash), canonical_json(raw))
        return raw_hash

    def _persist_batch_record(self, record: _BatchRecord) -> None:
        self._objects.put(
            self._record_key(record.batch_id),
            canonical_json(
                {
                    "batch_id": record.batch_id,
                    "session": record.session,
                    "effective_date": record.effective_date,
                    "file_name": record.file_name,
                    "file_content_hash": record.file_content_hash,
                    "rendered_b64": b64encode(record.rendered).decode("ascii"),
                    "signature_b64": b64encode(record.signature).decode("ascii"),
                    "uploaded": record.uploaded,
                    "entry_refs_by_trace": record.entry_refs_by_trace,
                    "amounts_by_trace": record.amounts_by_trace,
                    "settled_in_session": record.settled_in_session,
                    "outcome": record.outcome,
                }
            ),
        )

    def _load_batch_record(self, batch_id: str) -> _BatchRecord | None:
        record = self._batches.get(batch_id)
        if record is not None:
            return record
        key = self._record_key(batch_id)
        if not self._objects.exists(key):
            return None
        raw = json.loads(self._objects.get(key))
        record = _BatchRecord(
            batch_id=str(raw["batch_id"]),
            session=str(raw["session"]),
            effective_date=str(raw["effective_date"]),
            file_name=str(raw["file_name"]),
            file_content_hash=str(raw["file_content_hash"]),
            rendered=b64decode(str(raw["rendered_b64"])),
            signature=b64decode(str(raw["signature_b64"])),
            uploaded=bool(raw["uploaded"]),
            entry_refs_by_trace=dict(raw["entry_refs_by_trace"]),
            amounts_by_trace={
                str(trace): int(amount)
                for trace, amount in dict(raw["amounts_by_trace"]).items()
            },
            settled_in_session=raw["settled_in_session"],
            outcome=dict(raw["outcome"]),
        )
        self._batches[batch_id] = record
        if record.file_content_hash:
            self._content_hashes.add(record.file_content_hash)
        return record

    async def _upload_record(self, record: _BatchRecord) -> None:
        await self._transport.put(f"outbound/{record.file_name}.sig", record.signature)
        await self._transport.put(f"outbound/{record.file_name}", record.rendered)
        record.uploaded = True
        self._uploads[record.batch_id] = self._uploads.get(record.batch_id, 0) + 1
        self._content_hashes.add(record.file_content_hash)
        self._persist_batch_record(record)

    # -- single-flight upload claim (review finding
    # beftn-dispatch-race-double-uploads) -----------------------------------
    #
    # ``submit_batch`` used to gate the (re)upload on ``not recorded.uploaded``
    # alone. That flag only flips to True AFTER ``_upload_record`` finishes its
    # network round trip, so a concurrent caller — the CAS loser's recovery
    # path re-submitting after a NOT_RECEIVED query, or a second direct
    # ``submit_batch`` call — reads the same ``uploaded=False`` record while
    # the first upload is still in flight and uploads it again. The claim
    # below is checked and (on success) written with NO ``await`` in between,
    # so on this connector instance the event loop can never interleave a
    # second claimant between the read and the write: two concurrent callers
    # can never both win it. The claim is persisted through the same
    # ``ObjectStore`` the batch record itself uses, so a second connector
    # instance sharing that store also observes a live claim rather than
    # silence — true cross-process compare-and-set would additionally need an
    # atomic primitive at the store layer, which the object store ports do
    # not offer today; the per-instance guarantee above is what closes the
    # actual race this finding reproduced (both callers landing on ONE
    # process's connector/service instance, e.g. two concurrent requests
    # under one uvicorn worker).

    def _claim_key(self, batch_id: str) -> str:
        return f"beftn_batch_v2/upload_claims/{batch_id}.json"

    def _read_claim(self, batch_id: str) -> datetime | None:
        """The claim's ``claimed_at`` if live right now, else ``None``.

        Must be called with ``self._claim_lock`` held.
        """
        key = self._claim_key(batch_id)
        if not self._objects.exists(key):
            return None
        try:
            raw = json.loads(self._objects.get(key))
            claimed_at_raw = raw.get("claimed_at")
        except (ValueError, TypeError):
            return None
        if not claimed_at_raw:
            return None
        claimed_at = datetime.fromisoformat(str(claimed_at_raw))
        if (self._clock.now() - claimed_at) >= timedelta(seconds=self._UPLOAD_LEASE_SECONDS):
            return None  # lease expired — treat as free
        return claimed_at

    def is_upload_in_flight(self, batch_id: str) -> bool:
        """True iff another caller currently holds the upload claim.

        Exposed so a caller ONE LEVEL UP (``DisbursementService``'s CAS-loser
        recovery path) can skip its own query/resubmit entirely while the
        winner's upload is genuinely still running, rather than depending
        solely on the single-flight claim inside ``submit_batch`` to turn a
        would-be second upload into a no-op. Read-only: never claims.
        """
        with self._claim_lock:
            return self._read_claim(batch_id) is not None

    def _try_claim_upload(self, batch_id: str) -> bool:
        key = self._claim_key(batch_id)
        with self._claim_lock:
            if self._read_claim(batch_id) is not None:
                return False  # another caller holds the claim right now
            now = self._clock.now()
            self._objects.put(
                key, canonical_json({"batch_id": batch_id, "claimed_at": now.isoformat()})
            )
            return True

    def _release_upload_claim(self, batch_id: str) -> None:
        # ObjectStore has no delete; a cleared ``claimed_at`` reads as "free"
        # in ``_try_claim_upload`` above so the very next legitimate retry
        # (e.g. after a genuine transport failure) is never blocked by a
        # lease that has already ended.
        with self._claim_lock:
            self._objects.put(
                self._claim_key(batch_id),
                canonical_json({"batch_id": batch_id, "claimed_at": None}),
            )

    def _persisted_uploaded(self, batch_id: str) -> bool:
        """The ``uploaded`` flag as persisted in the object store right now.

        Deliberately bypasses this instance's ``_batches`` cache: the point is
        to see what a previous claim holder (this instance or another one
        sharing the store) has already done.
        """
        key = self._record_key(batch_id)
        if not self._objects.exists(key):
            return False
        try:
            return bool(json.loads(self._objects.get(key)).get("uploaded"))
        except (ValueError, TypeError, AttributeError):
            return False

    async def _claim_and_upload(self, record: _BatchRecord) -> None:
        """Upload ``record`` iff this caller wins the single-flight claim AND
        nobody has uploaded it in the meantime.

        A caller that loses the claim does nothing here: its own
        ``submit_batch``/idempotent-replay caller returns the already-computed
        deterministic ``record.outcome`` (the SUBMITTED outcome was built
        before the first upload attempt started) instead of re-uploading.

        Re-check after winning (2026-09-07, seen as a rare double upload under
        load after lane #339): a caller that read ``uploaded=False`` and then
        waited on the claim while the previous holder finished would otherwise
        win the now-free claim and re-upload its stale snapshot. Both the
        in-memory record (shared object within one instance) and the persisted
        flag (another instance sharing the object store) are consulted.
        """
        if not self._try_claim_upload(record.batch_id):
            return
        try:
            if record.uploaded or self._persisted_uploaded(record.batch_id):
                record.uploaded = True
                return
            await self._upload_record(record)
        finally:
            self._release_upload_claim(record.batch_id)

    def _now_iso(self) -> str:
        return self._clock.now().strftime("%Y-%m-%dT%H:%M:%SZ")

    def upload_count(self, batch_id: str) -> int:
        """Rail-side effects per batch (exactly one upload per unique batch)."""
        return self._uploads.get(batch_id, 0)

    def _outcome(self, batch_id: str, session: str, effective_date: str, status: str,
                 raw: dict, **extra) -> dict:
        outcome = {
            "batch_id": batch_id,
            "session": session,
            "effective_date": effective_date,
            "status": status,
            "responded_at": self._now_iso(),
            "raw_response_hash": self._archive(raw),
        }
        outcome.update(extra)
        return outcome

    def _next_modifier(self, business_date: date) -> str:
        key = business_date.isoformat()
        index = self._files_today.get(key, 0)
        self._files_today[key] = index + 1
        if index > 25:
            raise BeftnParseError("more than 26 BEFTN files in one day (modifier exhausted)")
        return chr(ord("A") + index)

    # -- SettlementFileConnector ------------------------------------------------------------

    async def submit_batch(
        self, batch_id: str, session: str, entries: list[dict], effective_date: str
    ) -> dict:
        recorded = self._load_batch_record(batch_id)
        if recorded is not None:
            if not recorded.uploaded and recorded.rendered:
                await self._claim_and_upload(recorded)
            return recorded.outcome  # idempotent replay; no second upload
        if session not in BEFTN_SESSIONS:
            raw = {"connector_id": CONNECTOR_ID, "batch_id": batch_id,
                   "refusal": "invalid_session", "session": session}
            return self._outcome(batch_id, session, effective_date, "REFUSED", raw,
                                 rejected_entries=[])
        rows: list[BeftnEntryRow] = []
        rejected: list[dict] = []
        for entry in entries:
            row, codes = validate_entry(entry, detokenizer=self._detokenizer)
            if codes:
                rejected.append({"entry_ref": str(entry.get("entry_ref", "")),
                                 "validation_codes": codes})
            elif row is not None:
                rows.append(row)
        if rejected or not rows:
            # Whole-batch refusal with the per-entry code list (B.2).
            raw = {"connector_id": CONNECTOR_ID, "batch_id": batch_id,
                   "refusal": "entry_validation", "rejected": rejected}
            return self._record_terminal(
                batch_id, session, effective_date,
                self._outcome(batch_id, session, effective_date, "REFUSED", raw,
                              rejected_entries=rejected),
            )
        now = self._clock.now()
        effective = date.fromisoformat(effective_date)
        same_day = effective <= self._calendar.dhaka_local(now).date()
        if same_day and not self._calendar.before_cutoff(now):
            # Refusal, not silent next-day rollover (FSM §3): queued for the
            # next session by the scheduler; NOT recorded as terminal.
            self._events.emit(
                "beftn_file.cutoff_deferred",
                {"batch_id": batch_id, "session": session, "effective_date": effective_date},
            )
            raw = {"connector_id": CONNECTOR_ID, "batch_id": batch_id,
                   "refusal": "after_cutoff", "session": session}
            return self._outcome(batch_id, session, effective_date, "DEFERRED", raw,
                                 rejected_entries=[])
        business_date = self._calendar.dhaka_local(now).date()
        modifier = self._next_modifier(business_date)
        rendered = render_file(
            rows,
            config=self._config,
            effective_date=effective,
            created_at=now,
            file_id_modifier=modifier,
        )
        parsed = parse_file(rendered)  # write-then-verify before SEAL
        if parsed.entry_count != len(rows):
            raise BeftnParseError("parse-back entry count diverged from input rows")
        re_rendered = render_file(
            rows,
            config=self._config,
            effective_date=effective,
            created_at=now,
            file_id_modifier=modifier,
        )
        if re_rendered != rendered:
            raise BeftnParseError("re-render is not byte-identical (SEAL refused)")
        if parsed.content_hash in self._content_hashes:
            # Duplicate-file guard (UNIQUE(direction, file_content_hash)).
            raw = {"connector_id": CONNECTOR_ID, "batch_id": batch_id,
                   "refusal": "duplicate_file", "file_content_hash": parsed.content_hash}
            self._events.emit("beftn_file.duplicate_blocked",
                              {"batch_id": batch_id, "file_content_hash": parsed.content_hash})
            return self._record_terminal(
                batch_id, session, effective_date,
                self._outcome(batch_id, session, effective_date, "DUPLICATE_FILE", raw,
                              rejected_entries=[]),
            )
        file_name = beftn_file_name(
            self._config.immediate_origin, business_date, session, modifier
        )
        self._events.emit(
            "beftn_file.sealed",
            {
                "file_id": make_connector_id(
                    "bfile",
                    {"direction": "OUTBOUND", "file_name": file_name,
                     "file_content_hash": parsed.content_hash},
                ),
                "entry_count": parsed.entry_count,
                "total_credit_minor": parsed.total_credit_minor,
                "file_content_hash": parsed.content_hash,
            },
        )
        signature = self._signer.sign(rendered)
        raw = {
            "connector_id": CONNECTOR_ID,
            "batch_id": batch_id,
            "file_name": file_name,
            "file_content_hash": parsed.content_hash,
            "entry_count": parsed.entry_count,
            "total_credit_minor": parsed.total_credit_minor,
            "total_debit_minor": parsed.total_debit_minor,
            "layout_hash": self._layout_hash,
        }
        outcome = self._outcome(
            batch_id, session, effective_date, "SUBMITTED", raw,
            file_name=file_name,
            file_content_hash=parsed.content_hash,
            entry_count=parsed.entry_count,
            total_credit_minor=parsed.total_credit_minor,
            rejected_entries=[],
        )
        record = _BatchRecord(
            batch_id=batch_id,
            session=session,
            effective_date=effective_date,
            file_name=file_name,
            file_content_hash=parsed.content_hash,
            rendered=rendered,
            signature=signature,
            uploaded=False,
            entry_refs_by_trace={
                trace: row.entry_ref
                for trace, row in zip(parsed.trace_numbers, rows, strict=True)
            },
            amounts_by_trace={
                trace: row.amount_minor
                for trace, row in zip(parsed.trace_numbers, rows, strict=True)
            },
            settled_in_session=None,
            outcome=outcome,
        )
        self._batches[batch_id] = record
        self._persist_batch_record(record)
        # Detached signature FIRST: the bank acknowledges on file arrival and
        # treats a data file without its signature as unsigned (fail-closed).
        await self._claim_and_upload(record)
        self._events.emit(
            "beftn_file.submitted",
            {"file_name": file_name, "session_code": session, "effective_date": effective_date},
        )
        return outcome

    def _record_terminal(self, batch_id: str, session: str, effective_date: str,
                         outcome: dict) -> dict:
        self._batches[batch_id] = _BatchRecord(
            batch_id=batch_id,
            session=session,
            effective_date=effective_date,
            file_name="",
            file_content_hash="",
            rendered=b"",
            signature=b"",
            uploaded=True,
            entry_refs_by_trace={},
            amounts_by_trace={},
            settled_in_session=None,
            outcome=outcome,
        )
        return outcome

    async def query_batch_status(self, batch_id: str) -> dict:
        record = self._load_batch_record(batch_id)
        if record is None:
            raw = {"connector_id": CONNECTOR_ID, "batch_id": batch_id,
                   "status": "NOT_RECEIVED"}
            return self._outcome(batch_id, "", "", "NOT_RECEIVED", raw, rejected_entries=[])
        if record.outcome["status"] in ("REFUSED", "DUPLICATE_FILE"):
            return record.outcome
        if not record.uploaded:
            try:
                await self._transport.get(f"outbound/{record.file_name}")
            except FileNotFoundError:
                raw = {
                    "connector_id": CONNECTOR_ID,
                    "batch_id": batch_id,
                    "status": "NOT_RECEIVED",
                    "file_name": record.file_name,
                    "file_content_hash": record.file_content_hash,
                }
                return self._outcome(
                    batch_id,
                    record.session,
                    record.effective_date,
                    "NOT_RECEIVED",
                    raw,
                    rejected_entries=[],
                    file_name=record.file_name,
                    file_content_hash=record.file_content_hash,
                )
            record.uploaded = True
            self._persist_batch_record(record)
        ack_path = f"ack/{record.file_name}.ack.csv"
        try:
            ack_bytes = await self._transport.get(ack_path)
        except FileNotFoundError:
            return record.outcome  # still SUBMITTED; ack pending
        text = normalize_bengali_digits(ack_bytes.decode("utf-8"))
        reader = csv.DictReader(io.StringIO(text))
        row = next(iter(reader), None)
        raw = {"connector_id": CONNECTOR_ID, "batch_id": batch_id,
               "ack_hex": ack_bytes.hex()}
        if row is None:
            return self._outcome(batch_id, record.session, record.effective_date,
                                 "SUBMITTED", raw, rejected_entries=[])
        status = (row.get("status") or "").strip().upper()
        if status == "ACCEPTED":
            if record.settled_in_session is None:
                record.settled_in_session = self._session_counter()
            outcome = self._outcome(
                batch_id, record.session, record.effective_date, "ACCEPTED", raw,
                accepted_count=int(row.get("entry_count") or 0),
                rejected_entries=[],
                file_name=record.file_name,
                file_content_hash=record.file_content_hash,
            )
        else:
            outcome = self._outcome(
                batch_id, record.session, record.effective_date, "REJECTED", raw,
                accepted_count=0,
                rejected_entries=[{"reason": (row.get("reason") or "").strip()}],
                file_name=record.file_name,
                file_content_hash=record.file_content_hash,
            )
        record.outcome = outcome
        self._persist_batch_record(record)
        return outcome

    # -- confirmation-CSV reconciliation input (B.2/B.3) ---------------------------------

    def ingest_confirmation_csv(self, data: bytes) -> BeftnReconReport:
        """Parse a bank return/confirmation CSV into recon dispositions.

        Bengali digits are normalized BEFORE numeric parsing; amounts are
        matched EXACTLY in integer paisa (a near-miss is a mismatch, ported
        from the Talent_1.0 recon-loop float pitfall — no tolerance windows);
        unknown trace numbers surface as unmatched recon exceptions; returns
        arriving more than 2 processing sessions after settlement are flagged
        ``late_return`` and never auto-reverse (ops case path).
        """
        raw_hash = self._archive(
            {"connector_id": CONNECTOR_ID, "confirmation_hex": data.hex()}
        )
        text = normalize_bengali_digits(data.decode("utf-8"))
        reader = csv.DictReader(io.StringIO(text))
        matched: list[BeftnReturnRow] = []
        unmatched: list[BeftnReturnRow] = []
        late: list[BeftnReturnRow] = []
        for row in reader:
            trace = (row.get("original_trace_number") or "").strip()
            return_code = (row.get("return_code") or "").strip().upper()
            amount_text = (row.get("amount_minor") or "").strip()
            session_text = (row.get("received_in_session") or "0").strip()
            amount = int(amount_text) if amount_text.isdigit() else -1
            received_in_session = int(session_text) if session_text.isdigit() else 0
            owner: _BatchRecord | None = None
            entry_ref: str | None = None
            for record in self._batches.values():
                if trace in record.entry_refs_by_trace:
                    owner = record
                    entry_ref = record.entry_refs_by_trace[trace]
                    break
            amount_mismatch = bool(
                owner is not None and owner.amounts_by_trace.get(trace) != amount
            )
            is_late = bool(
                owner is not None
                and owner.settled_in_session is not None
                and received_in_session
                > owner.settled_in_session + RETURN_WINDOW_SESSIONS
            )
            parsed_row = BeftnReturnRow(
                return_id=make_connector_id(
                    "bret",
                    {"trace": trace, "return_code": return_code, "raw": raw_hash},
                ),
                original_trace_number=trace,
                return_code=return_code,
                error_code=return_error_code(return_code),
                amount_minor=amount,
                received_in_session=received_in_session,
                entry_ref=entry_ref,
                amount_mismatch=amount_mismatch,
                late_return=is_late,
            )
            if entry_ref is None:
                unmatched.append(parsed_row)
                self._events.emit(
                    "beftn_return.unmatched",
                    {"original_trace_number": trace, "return_code": return_code},
                )
            elif is_late:
                late.append(parsed_row)
                self._events.emit(
                    "beftn_return.received",
                    {"return_id": parsed_row.return_id, "return_code": return_code,
                     "amount_minor": amount, "late_return": True},
                )
            else:
                matched.append(parsed_row)
                self._events.emit(
                    "beftn_return.received",
                    {"return_id": parsed_row.return_id, "return_code": return_code,
                     "amount_minor": amount, "late_return": False},
                )
        return BeftnReconReport(
            matched=tuple(matched),
            unmatched=tuple(unmatched),
            late=tuple(late),
            raw_response_hash=raw_hash,
        )

    async def list_return_files(self) -> list[str]:
        """List paths under the SFTP ``returns/`` directory."""
        return await self._transport.listdir("returns")

    async def get_return_file(self, remote_path: str) -> bytes:
        """Download a return/confirmation CSV from the SFTP server."""
        return await self._transport.get(remote_path)

    async def health_check(self) -> bool:
        try:
            await self._transport.listdir("ack")
        except FileNotFoundError:
            return True  # reachable, directory empty
        except Exception:
            return False
        return True

    def rendered_file(self, batch_id: str) -> bytes:
        """Immutable SEALED bytes (byte-identical download surface)."""
        record = self._load_batch_record(batch_id)
        if record is None or not record.rendered:
            raise KeyError(f"no sealed file for batch {batch_id}")
        return record.rendered

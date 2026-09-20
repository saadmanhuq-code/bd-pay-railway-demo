"""ReplayService — clean reimplementation (spec/03 modes 1-3 + spec/19 WINDOW_REPLAY).

SUPERSESSION NOTE (binding): **EXPLICITLY NOT LIFTED from**
``veridyn_next/replay_engine.py`` (20x UUIDv4 + wall-clock ``now()`` reads in
replay-critical paths; research 00, arch §(g)). That module stays REJECTED;
spec/19 PSO-3 supersedes it with this clean, purpose-built double-entry
ledger replay. Nothing here lifts, imports, or adapts the rejected module.

Determinism guarantees (spec/03 list, extended by spec/19):

- No UUIDv4, no wall-clock ``now()`` read, no randomness
  anywhere on the replay code paths.
- Replay inputs are ledger facts only: stored ``produced_at``/``occurred_at``
  values, journal/posting/chain/audit rows, the stored netting-run row and
  result file. The injected clock stamps replay-record ROW columns only —
  never any replayed output (D-19-7).
- Ordering is by ``entry_index``/``chain_index`` (monotonic), never by
  timestamp.
- Replay is idempotent: running a mode twice over the same ledger returns
  byte-identical canonical ``result_summary`` output (PSO-CERT-06 class).
- Output is written to ``ledger_replay_records`` (append-only); no ledger
  row is ever modified. Stored rows are NEVER overwritten.
- E5 deep verify (``LedgerService.verify_chain(deep=True)``) is the chain
  foundation — reused, never forked. A FAILED deep verify engages the ledger
  write gate (potential tamper; spec/03 tamper runbook).

PLATFORM_MODE gate (spec/19, binding): ``WINDOW_REPLAY`` is a PSO operation —
it raises :class:`bdpay.ledger.errors.PsoModeError` under ``PLATFORM_MODE=PSP``
at request AND at run, and the error is never caught-and-continued
(PSO-CERT-07 surface). Modes 1-3 are spec/03 operations available in both
modes.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from bdpay.ledger.errors import PsoModeError
from bdpay.ledger.hash_chain import compute_entry_hash, to_canonical_ts
from bdpay.ledger.payloads import journal_entry_payload_hash
from bdpay.ledger.replay import window_math
from bdpay.ledger.replay.errors import (
    ReplayError,
    ReplayInvalidTransitionError,
    ReplayNotFoundError,
)
from bdpay.ledger.replay.facts import WindowFactsPort, enumerate_money_entries
from bdpay.ledger.replay.ids import make_replay_id
from bdpay.ledger.replay.store import ReplayRecordStore
from bdpay.ledger.replay.types import (
    MODE_REFERENCE_TYPES,
    REPLAY_MODES,
    ReplayRecord,
    WindowReplayReport,
)
from bdpay.ledger.types import DEBIT_NORMAL_TYPES
from bdpay.platform.canonical import canonical_json
from bdpay.platform.config import Settings
from bdpay.platform.interfaces import AuditEventSpec, AuditPort
from bdpay.platform.pii import redact

if TYPE_CHECKING:
    from bdpay.ledger.service import LedgerService
    from bdpay.platform.clock import Clock

__all__ = ["ReplayService"]

PRODUCER = "ledger-service@1"
_MONEY = "MONEY"
_BATCH_CLOSE = "settlement_batch_close"


class ReplayService:
    """Deterministic replay over the hash-chained ledger; FSM-guarded records."""

    def __init__(
        self,
        records: ReplayRecordStore,
        *,
        ledger: LedgerService,
        clock: Clock,
        settings: Settings,
        audit: AuditPort,
        window_facts: WindowFactsPort | None = None,
        producer: str = PRODUCER,
    ) -> None:
        self._records = records
        self._ledger = ledger
        self._clock = clock
        self._settings = settings
        self._audit = audit
        self._window_facts = window_facts
        self._producer = producer

    # ------------------------------------------------------------------
    # Guards
    # ------------------------------------------------------------------

    def _require_pso(self) -> None:
        if self._settings.platform_mode != "PSO":
            raise PsoModeError(
                f"WINDOW_REPLAY is a PSO-only operation; refused under "
                f"PLATFORM_MODE={self._settings.platform_mode!r}"
            )

    def _require_facts(self) -> WindowFactsPort:
        if self._window_facts is None:
            raise ReplayError("WINDOW_REPLAY requires a wired WindowFactsPort (fail closed)")
        return self._window_facts

    def _now(self) -> datetime:
        dt = self._clock.now()
        if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
            raise ReplayError("clock returned a naive datetime (spec/00 §7)")
        return dt.astimezone(UTC)

    # ------------------------------------------------------------------
    # ReplayRequest FSM (spec/03; refusal-first)
    # ------------------------------------------------------------------

    def request_replay(
        self,
        *,
        reference_id: str,
        reference_type: str,
        replay_mode: str,
        requested_by: str,
    ) -> ReplayRecord:
        """Queue a replay (spec/03 POST /v1/ledger-replay semantics).

        Idempotent on the content-addressed ``rply_`` id. ``WINDOW_REPLAY``
        refuses under PSP mode before anything is read or written.
        """
        if replay_mode not in REPLAY_MODES:
            raise ReplayError(f"unknown replay_mode {replay_mode!r}; allowed: {REPLAY_MODES}")
        if replay_mode == "WINDOW_REPLAY":
            self._require_pso()
        allowed = MODE_REFERENCE_TYPES[replay_mode]
        if reference_type not in allowed:
            raise ReplayError(
                f"reference_type {reference_type!r} is not valid for {replay_mode} "
                f"(allowed: {sorted(allowed)})"
            )
        if not reference_id:
            raise ReplayError("reference_id is required")
        if not requested_by:
            raise ReplayError("requested_by is required")
        requested_at = self._now()
        replay_id = make_replay_id(
            "rply",
            {
                "reference_id": reference_id,
                "reference_type": reference_type,
                "replay_mode": replay_mode,
                "requested_at_str": to_canonical_ts(requested_at),
            },
        )
        existing = self._records.get(replay_id)
        if existing is not None:
            return existing
        record = ReplayRecord(
            replay_id=replay_id,
            reference_id=reference_id,
            reference_type=reference_type,
            replay_mode=replay_mode,
            status="QUEUED",
            requested_by=redact(requested_by),
            requested_at=requested_at,
        )
        self._records.insert(record)
        return record

    def run(self, replay_id: str) -> ReplayRecord:
        """``runner_pickup`` + ``replay_complete``/``replay_failed`` in one pass."""
        record = self._records.get(replay_id)
        if record is None:
            raise ReplayNotFoundError(f"unknown replay record {replay_id}")
        if record.replay_mode == "WINDOW_REPLAY":
            self._require_pso()
        claimed = self._records.begin_run(replay_id, self._now())
        if claimed is None:
            raise ReplayInvalidTransitionError(
                f"transition 'runner_pickup' from state {record.status!r} is DENIED "
                "(refusal-first)"
            )
        self._write_audit(
            "REPLAY_STARTED",
            claimed,
            from_state="QUEUED",
            to_state="RUNNING",
            payload={"replay_mode": claimed.replay_mode, "reference_id": claimed.reference_id},
        )
        try:
            summary, failed = self._execute(claimed)
        except Exception as exc:  # fail closed: any error is a FAILED replay
            summary = {
                "replay_mode": claimed.replay_mode,
                "reference_id": claimed.reference_id,
                "verdict": "FAILED",
                "fail_reason": redact(f"{type(exc).__name__}: {exc}"),
            }
            failed = True
        status = "FAILED" if failed else "COMPLETED"
        completed = dataclasses.replace(
            claimed, status=status, completed_at=self._now(), result_summary=summary
        )
        self._records.save(completed)
        self._write_audit(
            "REPLAY_FAILED" if failed else "REPLAY_COMPLETED",
            completed,
            from_state="RUNNING",
            to_state=status,
            payload={
                "replay_mode": completed.replay_mode,
                "reference_id": completed.reference_id,
                "verdict": str(summary.get("verdict", status)),
            },
        )
        return completed

    def _write_audit(
        self,
        event_type: str,
        record: ReplayRecord,
        *,
        from_state: str,
        to_state: str,
        payload: dict[str, object],
    ) -> None:
        self._audit.append(
            AuditEventSpec(
                event_type=event_type,
                actor_id=self._producer,
                subject_type="LedgerReplay",
                subject_id=record.replay_id,
                from_state=from_state,
                to_state=to_state,
                payload=payload,
            ),
            clock=self._clock,
        )

    def _execute(self, record: ReplayRecord) -> tuple[dict[str, object], bool]:
        """Dispatch to the mode implementation; returns (summary, failed)."""
        if record.replay_mode == "ACCOUNT_BALANCE":
            return self.replay_account_balance(record.reference_id), False
        if record.replay_mode == "ENTRY_SEQUENCE":
            return (
                self.replay_entry_sequence(record.reference_id, record.reference_type),
                False,
            )
        if record.replay_mode == "DISPUTE_TRAIL":
            return self.replay_dispute_trail(record.reference_id), False
        report = self.execute_window_replay(record.reference_id)
        return self._window_summary(report), report.verdict != "PASSED"

    # ------------------------------------------------------------------
    # Mode 1: ACCOUNT_BALANCE (spec/03)
    # ------------------------------------------------------------------

    def replay_account_balance(
        self, account_id: str, as_of: datetime | None = None
    ) -> dict[str, object]:
        """Recompute an account balance from postings in chain order.

        Stored ``produced_at`` values only; ordering by ``entry_index`` via
        the chain walk (timestamps can collide within a microsecond). With
        ``as_of`` unset the replay runs to the chain tip; the recorded
        request flow always replays the full history (the ``as_of``
        point-in-time variant is a direct read — errata E-S19R-04 note).
        """
        store = self._ledger.store
        account = store.get_account(account_id)
        if account is None:
            raise ReplayError(f"unknown account {account_id}")
        debit_positive = account.account_type in DEBIT_NORMAL_TYPES
        balance = 0
        entries_touched = 0
        postings_replayed = 0
        for _row, _entry, postings in enumerate_money_entries(store):
            touched = False
            for posting in postings:
                if posting.account_id != account_id:
                    continue
                if as_of is not None and posting.produced_at > as_of:
                    continue
                sign = 1 if (posting.side == "DEBIT") == debit_positive else -1
                balance += sign * posting.amount_minor
                postings_replayed += 1
                touched = True
            if touched:
                entries_touched += 1
        discrepancies: list[dict[str, object]] = []
        if as_of is None:
            maintained = store.get_balance(account_id)
            if maintained != balance:
                discrepancies.append(
                    {
                        "kind": "BALANCE_MISMATCH",
                        "account_id": account_id,
                        "derived_minor": balance,
                        "maintained_minor": maintained,
                    }
                )
        verify = self._ledger.verify_chain(chain_domain=_MONEY, deep=True)
        return {
            "replay_mode": "ACCOUNT_BALANCE",
            "account_id": account_id,
            "as_of": to_canonical_ts(as_of) if as_of is not None else None,
            "entries_replayed": entries_touched,
            "postings_replayed": postings_replayed,
            "balance_at_start_minor": 0,
            "balance_at_end_minor": balance,
            "chain_hashes_valid": verify.ok,
            "discrepancies": discrepancies,
            "verdict": "COMPLETED",
        }

    # ------------------------------------------------------------------
    # Mode 2: ENTRY_SEQUENCE (spec/03)
    # ------------------------------------------------------------------

    def replay_entry_sequence(self, reference_id: str, reference_type: str) -> dict[str, object]:
        """Walk all journal entries for a reference in ``entry_index`` order,
        recompute each chain hash AND payload hash from scratch (E5 depth),
        and report discrepancies."""
        store = self._ledger.store
        discrepancies: list[dict[str, object]] = []
        entries_replayed = 0
        for row, entry, postings in enumerate_money_entries(store):
            if entry.reference_id != reference_id or entry.reference_type != reference_type:
                continue
            entries_replayed += 1
            debit = sum(p.amount_minor for p in postings if p.side == "DEBIT")
            credit = sum(p.amount_minor for p in postings if p.side == "CREDIT")
            if debit != credit:
                discrepancies.append(
                    {
                        "kind": "ENTRY_IMBALANCED",
                        "entry_id": entry.entry_id,
                        "expected": debit,
                        "actual": credit,
                    }
                )
            recomputed_chain = compute_entry_hash(
                prev_hash=row.prev_chain_hash,
                chain_index=row.chain_index,
                chain_domain=row.chain_domain,
                entry_type=row.entry_type,
                payload_hash=row.payload_hash,
                payload_pointer=row.payload_pointer,
                amount_minor_sum=row.amount_minor_sum,
                producer=row.producer,
                produced_at=to_canonical_ts(row.produced_at),
            )
            if recomputed_chain != row.chain_hash:
                discrepancies.append(
                    {
                        "kind": "CHAIN_HASH_MISMATCH",
                        "entry_id": entry.entry_id,
                        "chain_index": row.chain_index,
                        "expected": row.chain_hash,
                        "actual": recomputed_chain,
                    }
                )
            recomputed_payload = journal_entry_payload_hash(entry, list(postings))
            if recomputed_payload != row.payload_hash:
                discrepancies.append(
                    {
                        "kind": "PAYLOAD_HASH_MISMATCH",
                        "entry_id": entry.entry_id,
                        "chain_index": row.chain_index,
                        "expected": row.payload_hash,
                        "actual": recomputed_payload,
                    }
                )
        if entries_replayed == 0:
            raise ReplayError(
                f"no journal entries for reference {reference_id!r} ({reference_type})"
            )
        chain_ok = not any(
            d["kind"] in ("CHAIN_HASH_MISMATCH", "PAYLOAD_HASH_MISMATCH") for d in discrepancies
        )
        return {
            "replay_mode": "ENTRY_SEQUENCE",
            "reference_id": reference_id,
            "reference_type": reference_type,
            "entries_replayed": entries_replayed,
            "chain_hashes_valid": chain_ok,
            "discrepancies": discrepancies,
            "verdict": "COMPLETED",
        }

    # ------------------------------------------------------------------
    # Mode 3: DISPUTE_TRAIL (spec/03)
    # ------------------------------------------------------------------

    def replay_dispute_trail(self, payment_intent_id: str) -> dict[str, object]:
        """Chronological narrative of every ledger event touching a payment.

        ``balances`` is a per-account before/after map (the spec's scalar
        ``balance_before/after`` is underdefined for multi-account trails —
        errata E-S19R-04). ``pii_present`` is always False: ledger rows are
        PII-redacted at write, and descriptions are re-redacted defensively.
        """
        store = self._ledger.store
        trail = [
            (row, entry, postings)
            for row, entry, postings in enumerate_money_entries(store)
            if entry.reference_id == payment_intent_id
        ]
        if not trail:
            raise ReplayError(f"no ledger events for payment {payment_intent_id!r}")
        first_index = trail[0][1].entry_index
        last_index = trail[-1][1].entry_index
        touched_accounts = sorted(
            {p.account_id for _row, _entry, postings in trail for p in postings}
        )
        balances = [
            {
                "account_id": account_id,
                "balance_before_minor": self._derived_balance(account_id, first_index - 1),
                "balance_after_minor": self._derived_balance(account_id, last_index),
            }
            for account_id in touched_accounts
        ]
        entries_out = [
            {
                "entry_id": entry.entry_id,
                "entry_index": entry.entry_index,
                "entry_type": entry.entry_type,
                "description": redact(entry.description),
                "produced_at": to_canonical_ts(entry.produced_at),
                "chain_hash": row.chain_hash,
                "postings": [
                    {
                        "posting_id": p.posting_id,
                        "account_id": p.account_id,
                        "side": p.side,
                        "amount_minor": p.amount_minor,
                    }
                    for p in postings
                ],
            }
            for row, entry, postings in trail
        ]
        audit_rows = store.list_audit_events(subject_type=None, subject_id=payment_intent_id)
        audit_out = [
            {
                "event_id": row.event_id,
                "event_type": row.event_type,
                "from_state": row.from_state,
                "to_state": row.to_state,
                "occurred_at": to_canonical_ts(row.occurred_at),
            }
            for row in sorted(audit_rows, key=lambda r: r.entry_index)
        ]
        verify = self._ledger.verify_chain(chain_domain=_MONEY, deep=True)
        return {
            "replay_mode": "DISPUTE_TRAIL",
            "payment_intent_id": payment_intent_id,
            "entries_replayed": len(entries_out),
            "entries": entries_out,
            "audit_events": audit_out,
            "balances": balances,
            "chain_valid": verify.ok,
            "pii_present": False,
            "verdict": "COMPLETED",
        }

    def _derived_balance(self, account_id: str, up_to_entry_index: int) -> int:
        """Balance derived from postings with ``entry_index <= bound``."""
        store = self._ledger.store
        account = store.get_account(account_id)
        if account is None:
            raise ReplayError(f"unknown account {account_id}")
        debit_positive = account.account_type in DEBIT_NORMAL_TYPES
        balance = 0
        for _row, entry, postings in enumerate_money_entries(store):
            if entry.entry_index > up_to_entry_index:
                continue
            for posting in postings:
                if posting.account_id != account_id:
                    continue
                sign = 1 if (posting.side == "DEBIT") == debit_positive else -1
                balance += sign * posting.amount_minor
        return balance

    # ------------------------------------------------------------------
    # Mode 4: WINDOW_REPLAY (spec/19 PSO-3 — binding mechanics)
    # ------------------------------------------------------------------

    def execute_window_replay(self, settlement_window_id: str) -> WindowReplayReport:
        """Deterministic byte-parity replay of a closed/netted window.

        Reads ONLY ledger facts: journal entries + postings selected by the
        D-19-6 attribution, ordered by ``entry_index``; the stored
        ``netting_runs`` row; ``ledger_chain``. Steps per spec/19:

        1. Re-derive per-participant positions (algorithm step 1b verbatim).
        2. Re-run obligations + result-document construction (steps 3-4) as a
           pure function.
        3. Byte-compare the recomputed canonical document against the stored
           result file AND its sha256 against ``netting_runs.result_hash``.
           Any difference => verdict FAILED with the first differing byte
           offset + field path.
        4. ``verify_chain(deep=True)`` (E5 — the gate mode) over the MONEY
           chain range [first window entry .. settlement_batch_close entry],
           plus a range count cross-check.
        5. The caller (``run``) writes the output to ``ledger_replay_records``
           (append-only).

        Anchor rule (errata E-S19R-03): the two chain-attestation fields and
        the rail are read from the stored document and VERIFIED against
        ``ledger_chain`` / plan consistency — everything else is re-derived.
        """
        self._require_pso()
        facts = self._require_facts()
        window = facts.get_window(settlement_window_id)
        if window is None:
            raise ReplayError(f"unknown settlement window {settlement_window_id}")
        run = facts.latest_netting_run(settlement_window_id)
        if run is None:
            raise ReplayError(
                f"window {settlement_window_id} has no netting run; not replayable "
                "(requires NETTING_CALCULATED or later)"
            )
        if run.status != "PASSED":
            raise ReplayError(
                f"netting run {run.netting_run_id} is {run.status}; only PASSED runs replay"
            )
        if run.result_pointer is None or run.result_hash is None:
            raise ReplayError(f"netting run {run.netting_run_id} has no sealed result")
        stored_bytes = facts.result_file(run.result_pointer)
        if stored_bytes is None:
            raise ReplayError(f"stored netting result {run.result_pointer} is missing")

        discrepancies: list[dict[str, object]] = []
        stored_sha = hashlib.sha256(stored_bytes).hexdigest()
        if stored_sha != run.result_hash:
            discrepancies.append(
                {
                    "kind": "RESULT_FILE_HASH_MISMATCH",
                    "expected": run.result_hash,
                    "actual": stored_sha,
                }
            )
        try:
            stored_document = json.loads(stored_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ReplayError(f"stored netting result is not parseable JSON: {exc}") from exc
        if not isinstance(stored_document, dict):
            raise ReplayError("stored netting result is not a JSON object")

        entries = facts.window_entries(settlement_window_id)
        if not entries:
            raise ReplayError(
                f"window {settlement_window_id} has no attributed journal entries (D-19-6)"
            )
        closes = [e for e in entries if e.entry.entry_type == _BATCH_CLOSE]
        if not closes:
            raise ReplayError(
                f"window {settlement_window_id} has no {_BATCH_CLOSE} entry; "
                "not replayable (requires NETTING_CALCULATED or later)"
            )
        close = closes[-1]
        # Compute-time snapshot semantics: the engine read positions BEFORE
        # writing the net-file entry, so derivation excludes the batch-close
        # bound and any earlier (superseded-run) close entries.
        derivation = [
            e
            for e in entries
            if e.entry.entry_index < close.entry.entry_index
            and e.entry.entry_type != _BATCH_CLOSE
        ]
        positions = window_math.derive_positions(
            derivation, participant_for_account=facts.participant_for_account
        )

        # D-19-6 completeness: every position-touching entry inside the window
        # range must carry the attribution — fail closed otherwise.
        first_entry_index = min(e.entry.entry_index for e in entries)
        for entry_id, attributed in facts.position_entries_in_range(
            first_entry_index, close.entry.entry_index
        ):
            if attributed is None:
                discrepancies.append(
                    {"kind": "MISSING_WINDOW_ATTRIBUTION", "entry_id": entry_id}
                )

        delta = window_math.zero_sum_delta(positions)
        if delta != 0:
            discrepancies.append({"kind": "ZERO_SUM_VIOLATION", "zero_sum_delta_minor": delta})

        inputs_hash = window_math.inputs_hash_for(positions)
        if inputs_hash != run.inputs_hash:
            discrepancies.append(
                {
                    "kind": "INPUTS_HASH_MISMATCH",
                    "expected": run.inputs_hash,
                    "actual": inputs_hash,
                }
            )
        expected_run_id = window_math.make_netting_run_id(settlement_window_id, inputs_hash)
        if expected_run_id != run.netting_run_id:
            discrepancies.append(
                {
                    "kind": "NETTING_RUN_ID_MISMATCH",
                    "expected": expected_run_id,
                    "actual": run.netting_run_id,
                }
            )

        # Chain anchors (E-S19R-03): stored values, verified against the chain.
        anchor_index_raw = stored_document.get("chain_index_at_compute")
        anchor_hash_raw = stored_document.get("chain_hash_at_compute")
        anchor_index = (
            anchor_index_raw
            if isinstance(anchor_index_raw, int) and not isinstance(anchor_index_raw, bool)
            else -1
        )
        anchor_hash = anchor_hash_raw if isinstance(anchor_hash_raw, str) else ""
        anchor_row = facts.chain_entry_at(anchor_index) if anchor_index >= 0 else None
        if anchor_row is None or anchor_row.chain_hash != anchor_hash:
            discrepancies.append(
                {
                    "kind": "CHAIN_ANCHOR_MISMATCH",
                    "chain_index_at_compute": anchor_index,
                    "stored_chain_hash": anchor_hash,
                    "ledger_chain_hash": None if anchor_row is None else anchor_row.chain_hash,
                }
            )

        # Rail attestation (D-19-3 config value as recorded in the plan).
        stored_plan = stored_document.get("instruction_plan")
        stored_plan = stored_plan if isinstance(stored_plan, list) else []
        rails = {row.get("rail") for row in stored_plan if isinstance(row, dict)}
        rail = ""
        if len(rails) == 1:
            (only_rail,) = rails
            rail = only_rail if isinstance(only_rail, str) else ""
        elif len(rails) > 1:
            discrepancies.append(
                {"kind": "PLAN_RAIL_INCONSISTENT", "rails": sorted(str(r) for r in rails)}
            )

        obligations = window_math.obligations_from_positions(positions)
        gross = window_math.total_gross_minor(
            derivation, participant_for_account=facts.participant_for_account
        )
        net_pay = window_math.total_net_pay_minor(obligations)
        plan = window_math.build_instruction_plan(
            obligations,
            rail=rail,
            settlement_account_token=facts.settlement_account_token,
        )
        document = window_math.build_result_document(
            settlement_window_id=settlement_window_id,
            dns_session=str(window["dns_session"]),
            window_date=str(window["window_date"]),
            chain_index_at_compute=anchor_index,
            chain_hash_at_compute=anchor_hash,
            positions=positions,
            obligations=obligations,
            gross_minor=gross,
            net_pay_minor=net_pay,
            instruction_plan=plan,
        )
        recomputed_bytes = canonical_json(document)
        recomputed_hash = window_math.result_hash_for(document)
        identical = recomputed_bytes == stored_bytes
        if not identical:
            diff = window_math.first_difference(
                stored_bytes, recomputed_bytes, stored_document, document
            )
            if diff is not None:
                discrepancies.append(diff)

        # Step 4: E5 deep verify over the window's MONEY chain range.
        from_index = facts.chain_index_for_entry(entries[0].entry.entry_id)
        to_index = facts.chain_index_for_entry(close.entry.entry_id)
        if from_index is None or to_index is None:
            raise ReplayError("window entries are missing from the MONEY chain (fail closed)")
        verify = self._ledger.verify_chain(
            chain_domain=_MONEY, from_index=from_index, to_index=to_index, deep=True
        )
        chain_ok = verify.ok
        if chain_ok and verify.entries_checked != (to_index - from_index + 1):
            chain_ok = False
            discrepancies.append(
                {
                    "kind": "CHAIN_RANGE_COUNT_MISMATCH",
                    "expected": to_index - from_index + 1,
                    "actual": verify.entries_checked,
                }
            )
        if not verify.ok:
            discrepancies.append(
                {
                    "kind": "CHAIN_DEEP_VERIFY_FAILED",
                    "status": verify.status,
                    "first_broken_index": verify.first_broken_index,
                    "detail": verify.detail,
                }
            )

        return WindowReplayReport(
            settlement_window_id=settlement_window_id,
            netting_run_id=run.netting_run_id,
            recomputed_document=document,
            recomputed_bytes=recomputed_bytes,
            recomputed_result_hash=recomputed_hash,
            stored_result_hash=run.result_hash,
            result_bytes_identical=identical,
            chain_deep_verified=chain_ok,
            chain_from_index=from_index,
            chain_to_index=to_index,
            entries_replayed=len(entries),
            positions=positions,
            discrepancies=tuple(discrepancies),
        )

    @staticmethod
    def _window_summary(report: WindowReplayReport) -> dict[str, object]:
        """The spec/19 ``result_summary`` shape for WINDOW_REPLAY records."""
        return {
            "replay_mode": "WINDOW_REPLAY",
            "settlement_window_id": report.settlement_window_id,
            "entries_replayed": report.entries_replayed,
            "result_bytes_identical": report.result_bytes_identical,
            "chain_deep_verified": report.chain_deep_verified,
            "netting_run_id": report.netting_run_id,
            "recomputed_result_hash": report.recomputed_result_hash,
            "stored_result_hash": report.stored_result_hash,
            "chain_range": {
                "from_index": report.chain_from_index,
                "to_index": report.chain_to_index,
            },
            "discrepancies": [dict(d) for d in report.discrepancies],
            "verdict": report.verdict,
        }

"""Replay record types and vocabularies (spec/03 ledger_replay_records + spec/19 PSO-3).

The ``ledger_replay_records`` row shape matches migration
``0103_replay_window_mode.sql`` (which CREATES the spec/03 table — it had
never shipped in a migration; errata E-S19R-01) with the spec/19 additive
``WINDOW_REPLAY`` mode and ``SETTLEMENT_WINDOW`` reference type.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime

from bdpay.ledger.types import REFERENCE_TYPES, JournalEntryRow, PostingRow

__all__ = [
    "MODE_REFERENCE_TYPES",
    "REPLAY_MODES",
    "REPLAY_REFERENCE_TYPES",
    "REPLAY_STATES",
    "REPLAY_TERMINAL_STATES",
    "REPLAY_TRANSITIONS",
    "AttributedEntry",
    "NettingRunView",
    "PositionDerivation",
    "ReplayRecord",
    "WindowReplayReport",
]

#: spec/03 modes 1-3 + the spec/19 PSO-3 additive mode 4.
REPLAY_MODES: tuple[str, ...] = (
    "ACCOUNT_BALANCE",
    "ENTRY_SEQUENCE",
    "DISPUTE_TRAIL",
    "WINDOW_REPLAY",
)

REPLAY_STATES: tuple[str, ...] = ("QUEUED", "RUNNING", "COMPLETED", "FAILED")
REPLAY_TERMINAL_STATES: frozenset[str] = frozenset({"COMPLETED", "FAILED"})

#: spec/03 ReplayRequest FSM, refusal-first: any pair not listed is DENIED.
#:   QUEUED  --[runner_pickup]----> RUNNING    aud:REPLAY_STARTED
#:   RUNNING --[replay_complete]--> COMPLETED  aud:REPLAY_COMPLETED; output row
#:   RUNNING --[replay_failed]----> FAILED     aud:REPLAY_FAILED; reason named
REPLAY_TRANSITIONS: dict[tuple[str, str], str] = {
    ("QUEUED", "runner_pickup"): "RUNNING",
    ("RUNNING", "replay_complete"): "COMPLETED",
    ("RUNNING", "replay_failed"): "FAILED",
}

#: App-side closed registry for replay reference types: the journal reference
#: vocabulary plus the replay-specific anchors (spec/03 ``ACCOUNT`` for mode 1;
#: spec/19 additive ``SETTLEMENT_WINDOW`` for mode 4).
REPLAY_REFERENCE_TYPES: frozenset[str] = REFERENCE_TYPES | {"ACCOUNT", "SETTLEMENT_WINDOW"}

#: Which reference types each mode accepts (refusal-first).
MODE_REFERENCE_TYPES: dict[str, frozenset[str]] = {
    "ACCOUNT_BALANCE": frozenset({"ACCOUNT"}),
    "ENTRY_SEQUENCE": frozenset(REFERENCE_TYPES),
    "DISPUTE_TRAIL": frozenset({"PAYMENT"}),
    "WINDOW_REPLAY": frozenset({"SETTLEMENT_WINDOW"}),
}


@dataclass(frozen=True, slots=True)
class ReplayRecord:
    """One ``ledger_replay_records`` row (append-only output; spec/03 DDL)."""

    # rply_<sha256_canonical({reference_id, reference_type, replay_mode,
    #                          requested_at_str})[:24]>
    replay_id: str
    reference_id: str
    reference_type: str
    replay_mode: str
    status: str
    requested_by: str  # PII-redacted actor id
    requested_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    result_summary: Mapping[str, object] | None = None
    result_pointer: str | None = None
    schema_version: int = 1

    @property
    def is_terminal(self) -> bool:
        return self.status in REPLAY_TERMINAL_STATES


@dataclass(frozen=True, slots=True)
class AttributedEntry:
    """A journal entry + its postings + the D-19-6 window attribution.

    ``settlement_window_id`` mirrors the ``journal_entries.metadata`` value
    written by the posting path in the same transaction (D-19-6); the facts
    port supplies it (Postgres: the metadata column verbatim; in-memory: the
    writer-maintained attribution map — errata E-S19R-02).
    """

    entry: JournalEntryRow
    postings: tuple[PostingRow, ...]
    settlement_window_id: str


@dataclass(frozen=True, slots=True)
class NettingRunView:
    """Read model of one spec/19 ``netting_runs`` row (PSO-2 lane owns the
    table; replay only READS it — never writes)."""

    netting_run_id: str
    settlement_window_id: str
    status: str  # COMPUTING | PASSED | FAILED_INTEGRITY | SUPERSEDED
    inputs_hash: str
    result_hash: str | None
    result_pointer: str | None
    participant_count: int
    total_gross_minor: int | None = None
    total_net_pay_minor: int | None = None
    excluded_participant_id: str | None = None
    supersedes_netting_run_id: str | None = None


@dataclass(frozen=True, slots=True)
class PositionDerivation:
    """Step-1b output for one participant: position + contributing entries."""

    participant_id: str
    net_position_minor: int
    contributing_entry_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class WindowReplayReport:
    """The full WINDOW_REPLAY output (pure function of ledger facts)."""

    settlement_window_id: str
    netting_run_id: str
    recomputed_document: Mapping[str, object]
    recomputed_bytes: bytes
    recomputed_result_hash: str
    stored_result_hash: str
    result_bytes_identical: bool
    chain_deep_verified: bool
    chain_from_index: int
    chain_to_index: int
    entries_replayed: int
    positions: tuple[PositionDerivation, ...]
    discrepancies: tuple[Mapping[str, object], ...] = field(default_factory=tuple)

    @property
    def verdict(self) -> str:
        """``PASSED`` iff bytes are identical AND the deep chain verify is
        green AND no integrity discrepancy was found (refusal-first)."""
        ok = (
            self.result_bytes_identical
            and self.chain_deep_verified
            and not self.discrepancies
        )
        return "PASSED" if ok else "FAILED"

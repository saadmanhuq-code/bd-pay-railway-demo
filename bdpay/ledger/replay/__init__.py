"""Clean replay service — deterministic ledger replay + dispute evidence (spec/03 + spec/19 PSO-3).

SUPERSESSION NOTE (binding, arch §(g) + spec/19 Scope): the rejected legacy
module ``veridyn_next/replay_engine.py`` (20x UUIDv4 + wall-clock ``now()``
reads in replay-critical paths) **stays REJECTED**. This package is the clean
reimplementation the architecture decision called for; nothing here lifts,
imports, or adapts the rejected module. spec/19 explicitly supersedes it.

Determinism guarantees (spec/03 + spec/19, test-pinned):

- Replay inputs are LEDGER FACTS ONLY: journal_entries, postings,
  ledger_chain, audit_events, the stored netting-run row and result file.
- No UUIDv4, no wall-clock ``now()`` read, no randomness anywhere
  on the replay path. The injected :class:`bdpay.platform.clock.Clock` is read
  ONLY for replay-record row columns (``requested_at``/``started_at``/
  ``completed_at``) — never inside any replayed or hashed output (D-19-7).
- Ordering is by ``entry_index`` / ``chain_index`` (monotonic), never by
  timestamp.
- Two runs over the same ledger produce byte-identical canonical output.
- E5 deep verify (``LedgerService.verify_chain(deep=True)``) is the chain
  foundation — reused, never forked.
"""

from bdpay.ledger.replay.bundle import (
    PSO_DISPUTE_BUNDLE_MEMBERS,
    DisputeBundleExporter,
    DisputeBundleRecord,
    DisputeBundleStore,
    InMemoryDisputeBundleStore,
)
from bdpay.ledger.replay.errors import (
    BundleAssemblyError,
    ReplayError,
    ReplayInvalidTransitionError,
    ReplayNotFoundError,
)
from bdpay.ledger.replay.facts import InMemoryObjectStore, LedgerWindowFacts, WindowFactsPort
from bdpay.ledger.replay.service import ReplayService
from bdpay.ledger.replay.store import InMemoryReplayRecordStore, ReplayRecordStore
from bdpay.ledger.replay.types import (
    REPLAY_MODES,
    REPLAY_STATES,
    REPLAY_TERMINAL_STATES,
    REPLAY_TRANSITIONS,
    AttributedEntry,
    NettingRunView,
    ReplayRecord,
    WindowReplayReport,
)

__all__ = [
    "PSO_DISPUTE_BUNDLE_MEMBERS",
    "REPLAY_MODES",
    "REPLAY_STATES",
    "REPLAY_TERMINAL_STATES",
    "REPLAY_TRANSITIONS",
    "AttributedEntry",
    "BundleAssemblyError",
    "DisputeBundleExporter",
    "DisputeBundleRecord",
    "DisputeBundleStore",
    "InMemoryDisputeBundleStore",
    "InMemoryObjectStore",
    "InMemoryReplayRecordStore",
    "LedgerWindowFacts",
    "NettingRunView",
    "ReplayError",
    "ReplayInvalidTransitionError",
    "ReplayNotFoundError",
    "ReplayRecord",
    "ReplayRecordStore",
    "ReplayService",
    "WindowFactsPort",
    "WindowReplayReport",
]

"""DNS netting v2 — multilateral netting engine (spec/19 workstream PSO-2).

Public surface:

- :class:`~bdpay.ledger.netting.engine.NettingEngine` — compute / unwind /
  window side-effects / dispatch plans (PSO-mode gated).
- :class:`~bdpay.ledger.netting.facts.StoreLedgerFacts` — D-19-6 per-window
  derivation from ledger facts only (shared with WINDOW_REPLAY, PSO-3).
- :class:`~bdpay.ledger.netting.positions.PositionPostingService` — the
  attributed posting path every PSO position movement goes through.
- :class:`~bdpay.ledger.netting.calendar.WindowSweeper` +
  :func:`~bdpay.ledger.netting.calendar.build_pso_scheduler_tasks` — the
  window calendar on the platform scheduler (absent under PSP).
"""

from bdpay.ledger.netting.config import NettingConfig, parse_session_schedule
from bdpay.ledger.netting.engine import NettingEngine, PsoInstructionDispatch
from bdpay.ledger.netting.errors import (
    NdcParticipantNotActiveError,
    NettingError,
    NettingGuardError,
    NettingIntegrityError,
    NettingInvalidTransitionError,
    UnwindRefusedError,
)
from bdpay.ledger.netting.facts import LedgerFactsPort, StoreLedgerFacts, WindowDerivation
from bdpay.ledger.netting.objectstore import (
    FilesystemObjectStore,
    InMemoryObjectStore,
    ObjectStore,
)
from bdpay.ledger.netting.positions import (
    InMemoryNettingPositionStore,
    NettingPositionStore,
    ParticipantDirectory,
    PositionPostingService,
    PostgresNettingPositionStore,
    StaticParticipantDirectory,
)
from bdpay.ledger.netting.store import InMemoryNettingStore, NettingStore
from bdpay.ledger.netting.types import (
    NettingObligationRow,
    NettingRunRow,
    ParticipantPosition,
    PsoNettingInstructionRow,
    build_result_document,
    compute_obligations,
    netting_inputs_hash,
    netting_run_id_for,
    participant_position_account_id,
)

__all__ = [
    "FilesystemObjectStore",
    "InMemoryNettingPositionStore",
    "InMemoryNettingStore",
    "InMemoryObjectStore",
    "LedgerFactsPort",
    "NdcParticipantNotActiveError",
    "NettingConfig",
    "NettingEngine",
    "NettingError",
    "NettingGuardError",
    "NettingIntegrityError",
    "NettingInvalidTransitionError",
    "NettingObligationRow",
    "NettingPositionStore",
    "NettingRunRow",
    "NettingStore",
    "ObjectStore",
    "ParticipantDirectory",
    "ParticipantPosition",
    "PositionPostingService",
    "PostgresNettingPositionStore",
    "PsoInstructionDispatch",
    "PsoNettingInstructionRow",
    "StaticParticipantDirectory",
    "StoreLedgerFacts",
    "UnwindRefusedError",
    "WindowDerivation",
    "build_result_document",
    "compute_obligations",
    "netting_inputs_hash",
    "netting_run_id_for",
    "parse_session_schedule",
    "participant_position_account_id",
]

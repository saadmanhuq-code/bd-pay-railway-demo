"""Reconciliation engine (spec/04) — three-way recon over the immutable ledger.

Drift-classification report shape ported from
``dse-profit-engine/src/dse_engine/pod/reconciler.py`` (ADAPT, PORTING-MAP:
"rename to settlement domain"): a per-run report dataclass with one counter
per classification outcome plus an errors list, and idempotent re-runs.
"""

from bdpay.ledger.reconciliation.engine import ReconciliationEngine
from bdpay.ledger.reconciliation.store import InMemoryReconStore, ReconStore
from bdpay.ledger.reconciliation.types import (
    EXCEPTION_TRANSITIONS,
    RECORD_TRANSITIONS,
    NormalizedReconLine,
    ReconcileReport,
    ReconciliationExceptionRow,
    ReconciliationRecordRow,
)

__all__ = [
    "EXCEPTION_TRANSITIONS",
    "RECORD_TRANSITIONS",
    "InMemoryReconStore",
    "NormalizedReconLine",
    "ReconcileReport",
    "ReconciliationEngine",
    "ReconciliationExceptionRow",
    "ReconciliationRecordRow",
    "ReconStore",
]

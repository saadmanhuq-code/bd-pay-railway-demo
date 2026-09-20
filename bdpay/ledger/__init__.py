"""bdpay.ledger — double-entry ledger, hash chain, audit, settlement, recon, TCSA.

Ported from the verified clean-room pilot
``bd-pay-fable-pilot/src/bdpay_pilot/ledger`` (specs 03/04/05; SPEC_ERRATA
E1-E11 binding) with all ``bdpay_pilot.platform.*`` imports replaced by the
binding ``bdpay.platform.*`` implementations (canonical/E12, ids, money,
clock). Chain mechanics and Ed25519 checkpoint signing live here
(``hash_chain``, ``checkpoint_signer``) because the pilot platform modules
were not ported.
"""

from bdpay.ledger.errors import (
    ChainIntegrityError,
    LedgerAccountNotFoundError,
    LedgerAmountError,
    LedgerDuplicateError,
    LedgerError,
    LedgerImbalanceError,
    LedgerValidationError,
    LedgerWriteGateEngagedError,
    TrialBalanceViolationError,
)
from bdpay.ledger.memory_store import InMemoryLedgerStore
from bdpay.ledger.service import AUDIT_DOMAIN, MONEY_DOMAIN, LedgerService
from bdpay.ledger.types import (
    AccountSpec,
    AuditEventSpec,
    ChainVerificationResult,
    JournalEntrySpec,
    PostingSpec,
    TrialBalance,
)

__all__ = [
    "AUDIT_DOMAIN",
    "MONEY_DOMAIN",
    "AccountSpec",
    "AuditEventSpec",
    "ChainIntegrityError",
    "ChainVerificationResult",
    "InMemoryLedgerStore",
    "JournalEntrySpec",
    "LedgerAccountNotFoundError",
    "LedgerAmountError",
    "LedgerDuplicateError",
    "LedgerError",
    "LedgerImbalanceError",
    "LedgerService",
    "LedgerValidationError",
    "LedgerWriteGateEngagedError",
    "PostingSpec",
    "TrialBalance",
    "TrialBalanceViolationError",
]

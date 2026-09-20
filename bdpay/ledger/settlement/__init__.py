"""Settlement engine (spec/04) — PSP-mode batches, instructions, fees, BEFTN.

New code for spec/04 on top of the ported pilot ledger core. The engine is a
logical sub-service of the ``ledger`` package (``settlement-engine@1``); it
never touches postings/ledger_chain directly — every money-moving transition
goes through ``LedgerService.post_journal_entry``.
"""

from bdpay.ledger.settlement.engine import SettlementEngine, TcsaGateResult
from bdpay.ledger.settlement.fees import FeeRule, compute_fee, default_fee_rules, resolve_fee_rule
from bdpay.ledger.settlement.store import InMemorySettlementStore, SettlementStore
from bdpay.ledger.settlement.types import (
    BATCH_TERMINAL_STATES,
    BATCH_TRANSITIONS,
    INSTRUCTION_TERMINAL_STATES,
    INSTRUCTION_TRANSITIONS,
    MAX_RETRIES,
    SettlementBatchRow,
    SettlementInstructionRow,
)

__all__ = [
    "BATCH_TERMINAL_STATES",
    "BATCH_TRANSITIONS",
    "INSTRUCTION_TERMINAL_STATES",
    "INSTRUCTION_TRANSITIONS",
    "MAX_RETRIES",
    "FeeRule",
    "InMemorySettlementStore",
    "SettlementBatchRow",
    "SettlementEngine",
    "SettlementInstructionRow",
    "SettlementStore",
    "TcsaGateResult",
    "compute_fee",
    "default_fee_rules",
    "resolve_fee_rule",
]

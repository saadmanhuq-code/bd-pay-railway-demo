"""BEFTN batch handoff (spec/04 Rail 2 + spec/00 §10 SettlementFileConnector).

The settlement engine never imports the connectors package: this module
declares the ``SettlementFileConnector`` PORT (signature-identical to the
binding spec/00 §10 sub-protocol) and builds the pre-formatted entry dicts
the port's ``submit_batch`` consumes. The byte-precise fixed-width file
encoding is the BEFTN connector's job (spec/11); the binding field rules
pinned HERE are the ones spec/04 owns:

- Amount field: 10 digits, BDT with two implied decimals —
  ``str(amount_minor // 100).zfill(8) + str(amount_minor % 100).zfill(2)``.
- Individual ID: ``instruction_id[:15]``.
- Individual Name: 22 chars, space-padded / truncated.
- Transaction codes: '22' checking credit, '27' checking debit.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from bdpay.ledger.errors import BeftnFormatError
from bdpay.ledger.settlement.types import SettlementBatchRow, SettlementInstructionRow

__all__ = [
    "BeftnBatchHandoff",
    "SettlementFileConnector",
    "beftn_amount_field",
    "build_beftn_entries",
    "build_handoff",
]

#: Largest representable amount: 8 BDT digits + 2 paisa digits.
_MAX_AMOUNT_MINOR = 99_999_999 * 100 + 99

_TRANSACTION_CODES = {"CREDIT": "22", "DEBIT": "27"}


@runtime_checkable
class SettlementFileConnector(Protocol):
    """spec/00 §10 SettlementFileConnector — port only, no connector import."""

    connector_id: str

    async def submit_batch(
        self, batch_id: str, session: str, entries: list[dict], effective_date: str
    ) -> dict: ...

    async def query_batch_status(self, batch_id: str) -> dict: ...


def beftn_amount_field(amount_minor: int) -> str:
    """Encode integer paisa as the 10-char BEFTN amount field (8 BDT + 2 paisa)."""
    if isinstance(amount_minor, bool) or not isinstance(amount_minor, int):
        raise BeftnFormatError(
            f"amount_minor must be int paisa, got {type(amount_minor).__name__}"
        )
    if amount_minor <= 0:
        raise BeftnFormatError(f"amount_minor must be > 0, got {amount_minor}")
    if amount_minor > _MAX_AMOUNT_MINOR:
        raise BeftnFormatError(
            f"amount_minor {amount_minor} exceeds the BEFTN 10-digit field capacity"
        )
    return str(amount_minor // 100).zfill(8) + str(amount_minor % 100).zfill(2)


def _individual_name(name: str) -> str:
    """22 chars, left-justified, space-padded; longer names truncate."""
    return name[:22].ljust(22)


def build_beftn_entries(
    instructions: list[SettlementInstructionRow],
    merchant_names: dict[str, str] | None = None,
) -> list[dict]:
    """One entry dict per instruction, pre-formatted to the spec/04 field rules."""
    names = merchant_names or {}
    entries: list[dict] = []
    for sequence, instruction in enumerate(instructions, start=1):
        if instruction.direction not in _TRANSACTION_CODES:
            raise BeftnFormatError(f"unknown direction {instruction.direction!r}")
        entries.append(
            {
                "entry_ref": instruction.instruction_id,
                "record_type": "6",
                "transaction_code": _TRANSACTION_CODES[instruction.direction],
                "account_token": instruction.beneficiary_account_ref,
                "beneficiary_account_ref": instruction.beneficiary_account_ref,
                "amount_minor": instruction.net_payout_minor,
                "amount_field": beftn_amount_field(instruction.net_payout_minor),
                "individual_id": instruction.instruction_id[:15],
                "individual_name": _individual_name(
                    names.get(instruction.merchant_id, instruction.merchant_id)
                ),
                "addenda_record_indicator": "0",
                "trace_sequence": f"{sequence:07d}",
                "instruction_id": instruction.instruction_id,
                "connector_ref": instruction.connector_ref,
            }
        )
    return entries


@dataclass(frozen=True, slots=True)
class BeftnBatchHandoff:
    """Exactly the argument bundle ``SettlementFileConnector.submit_batch`` takes."""

    batch_id: str
    session: str
    entries: tuple[dict, ...]
    effective_date: str  # YYYY-MM-DD


def build_handoff(
    batch: SettlementBatchRow,
    instructions: list[SettlementInstructionRow],
    merchant_names: dict[str, str] | None = None,
) -> BeftnBatchHandoff:
    """Build the submit_batch argument bundle for a dispatched BEFTN batch."""
    if batch.session is None:
        raise BeftnFormatError(f"batch {batch.batch_id} has no BEFTN session")
    return BeftnBatchHandoff(
        batch_id=batch.batch_id,
        session=batch.session,
        entries=tuple(build_beftn_entries(instructions, merchant_names)),
        effective_date=batch.cycle_date.isoformat(),
    )

"""Connector SDK - BINDING contract per spec/00-conventions.md section 10.

Do not alter signatures. Every connector implements PaymentConnector (plus,
where applicable, one capability sub-protocol). Async is mandatory; money is
integer paisa; connector_ref is the idempotency key.

SDK invariants: a connector MUST be idempotent on connector_ref; MUST echo
instruction_id and connector_ref; MUST NOT write any business DB; MUST NOT
call ledger; MUST NOT retain decrypted PAN beyond a single submit; MUST
return raw_response_hash (never raw text to logs); MUST be wrapped by
CircuitBreaker; MUST honour the per-connector hard timeout.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable


class ConnectorStatus(str, Enum):
    SUCCESS = "success"
    PENDING = "pending"      # async; expect callback/poll
    FAILED = "failed"
    REJECTED = "rejected"    # hard decline; do not retry
    TIMED_OUT = "timed_out"  # soft; status-poll then reverse
    REVERSED = "reversed"


@dataclass(frozen=True)
class Money:
    amount_minor: int          # paisa; NEVER float, NEVER negative
    currency: str = "BDT"


@dataclass(frozen=True)
class PaymentInstruction:
    instruction_id: str        # ins_<...> content-addressed
    connector_ref: str         # idempotency key the connector MUST honour
    amount: Money
    method: str                # "NPSB_IBFT"|"BEFTN_CREDIT"|"BKASH"|"NAGAD"|"CARD"|"BANGLA_QR"
    sender_ref: str            # opaque/tokenized; never raw PAN, never raw account here
    beneficiary_ref: str       # opaque/tokenized
    rail: str                  # connector_id of the target rail
    instruction_at: str        # RFC3339 UTC
    metadata: dict[str, str]   # string-only; PII pre-redacted; MUST NOT affect amount


@dataclass(frozen=True)
class ConnectorResult:
    instruction_id: str        # echoed back, unchanged
    connector_ref: str         # echoed back, unchanged
    status: ConnectorStatus
    rail_transaction_id: str | None    # e.g. NPSB STAN / bKash trxID
    responded_at: str | None           # RFC3339 UTC
    error_code: str | None             # canonical connector error taxonomy
    raw_response_hash: str             # sha256(canonical_json(raw wire response))


@runtime_checkable
class PaymentConnector(Protocol):
    connector_id: str                  # e.g. "bkash_pgw_v2"
    supported_methods: tuple[str, ...]

    async def submit(self, instruction: PaymentInstruction) -> ConnectorResult: ...
    async def query_status(self, connector_ref: str,
                           rail_transaction_id: str | None) -> ConnectorResult: ...
    async def reverse(self, connector_ref: str, rail_transaction_id: str | None,
                      reverse_amount: Money, reason: str) -> ConnectorResult: ...
    async def health_check(self) -> bool: ...


# Inbound callbacks (MFS IPN, async rail confirmations)
@runtime_checkable
class ConnectorWebhookHandler(Protocol):
    connector_id: str

    def verify_signature(self, headers: dict, body: bytes) -> bool: ...
    def parse_event(self, headers: dict, body: bytes) -> ConnectorResult: ...


# Capability sub-protocols for non-payment rails (implement instead of/with the above):
@runtime_checkable
class IdentityConnector(Protocol):          # porichoy_ekyc_v1
    connector_id: str

    async def verify_nid(self, nid_number: str, dob: str, selfie_hash: str) -> dict: ...


@runtime_checkable
class SanctionsConnector(Protocol):         # sanctions_feed_v1
    connector_id: str

    async def screen(self, entity_name: str, entity_type: str, identifiers: dict) -> dict: ...


@runtime_checkable
class AmlFilingConnector(Protocol):         # goaml_reporter_v1
    connector_id: str

    async def file_str(self, payload: dict) -> str: ...
    async def file_ctr(self, payload: dict) -> str: ...


@runtime_checkable
class SettlementFileConnector(Protocol):    # beftn_batch_v2
    connector_id: str

    async def submit_batch(self, batch_id: str, session: str,
                           entries: list[dict], effective_date: str) -> dict: ...
    async def query_batch_status(self, batch_id: str) -> dict: ...

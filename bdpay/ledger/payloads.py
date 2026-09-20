"""Canonical journal-entry payload + payload_pointer helpers.

Ported from bd-pay-fable-pilot/src/bdpay_pilot/ledger/payloads.py (verified
pilot); hashing goes through the binding bdpay.platform.canonical (E12).

The chain row's payload_hash is sha256_canonical() over this payload, and the
deep verify pass rebuilds the identical payload from stored rows.  Postings
are ordered by posting_id (stored, deterministic) so write-time and
verify-time serialization always agree.  entry_index is store-assigned and is
therefore NOT part of the hashed payload.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from bdpay.ledger.hash_chain import to_canonical_ts
from bdpay.ledger.types import AuditEventRow, JournalEntryRow, PostingRow
from bdpay.platform.canonical import sha256_canonical

LEDGER_POINTER_PREFIX = "s3://bd-pay-ledger"
AUDIT_POINTER_PREFIX = "s3://bd-pay-audit"
_POINTER_SUFFIX = ".json.enc"


def journal_entry_payload(entry: JournalEntryRow, postings: Sequence[PostingRow]) -> dict[str, Any]:
    """The canonical (hashable) representation of a journal entry + postings."""
    return {
        "entry_id": entry.entry_id,
        "reference_id": entry.reference_id,
        "reference_type": entry.reference_type,
        "entry_type": entry.entry_type,
        "description": entry.description,
        "produced_by": entry.produced_by,
        "produced_at": to_canonical_ts(entry.produced_at),
        "idempotency_key": entry.idempotency_key,
        "schema_version": entry.schema_version,
        "postings": [
            {
                "posting_id": p.posting_id,
                "account_id": p.account_id,
                "side": p.side,
                "amount_minor": p.amount_minor,
                "currency": p.currency,
            }
            for p in sorted(postings, key=lambda p: p.posting_id)
        ],
    }


def journal_entry_payload_hash(entry: JournalEntryRow, postings: Sequence[PostingRow]) -> str:
    return sha256_canonical(journal_entry_payload(entry, postings))


def _pointer_for(prefix: str, entity_id: str, at: datetime) -> str:
    ts = to_canonical_ts(at)  # normalizes to UTC
    yyyy, mm, dd = ts[0:4], ts[5:7], ts[8:10]
    return f"{prefix}/{yyyy}/{mm}/{dd}/{entity_id}{_POINTER_SUFFIX}"


def payload_pointer_for(entry_id: str, produced_at: datetime) -> str:
    """"s3://bd-pay-ledger/{YYYY}/{MM}/{DD}/{entry_id}.json.enc" (spec/03 DDL).

    The pointer format is preserved and is part of the hash preimage; the
    object-store upload itself is the archival pipeline's job (wired later).
    """
    return _pointer_for(LEDGER_POINTER_PREFIX, entry_id, produced_at)


def audit_payload_pointer_for(event_id: str, occurred_at: datetime) -> str:
    """"s3://bd-pay-audit/{YYYY}/{MM}/{DD}/{event_id}.json.enc" (spec/03 DDL)."""
    return _pointer_for(AUDIT_POINTER_PREFIX, event_id, occurred_at)


def entry_id_from_pointer(pointer: str) -> str | None:
    """Parse the entry_id back out of a ledger payload_pointer (deep verify)."""
    if not pointer.startswith(LEDGER_POINTER_PREFIX + "/") or not pointer.endswith(_POINTER_SUFFIX):
        return None
    basename = pointer.rsplit("/", 1)[-1]
    entry_id = basename[: -len(_POINTER_SUFFIX)]
    return entry_id or None


def audit_event_payload(row: AuditEventRow) -> dict[str, Any]:
    """The canonical (hashable) representation of an audit event row.

    ``payload_hash`` itself is over the redacted payload dict; this envelope
    payload is what the AUDIT chain row seals (entry_index excluded —
    store-assigned).
    """
    return {
        "event_id": row.event_id,
        "event_type": row.event_type,
        "actor_id": row.actor_id,
        "actor_type": row.actor_type,
        "subject_type": row.subject_type,
        "subject_id": row.subject_id,
        "from_state": row.from_state,
        "to_state": row.to_state,
        "payload_hash": row.payload_hash,
        "occurred_at": to_canonical_ts(row.occurred_at),
        "schema_version": row.schema_version,
    }

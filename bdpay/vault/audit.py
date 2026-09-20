"""Hash-chained, append-only CDE audit (spec/14 table 9, `vault_audit_chain`).

Separate from the platform ``audit_events`` table — cross-database writes are
prohibited (spec/14 §Entities). Pattern per spec: append with a chained
preimage ``canonical_json({prev_hash, chain_index, entry_type, payload_hash,
actor, produced_at})``; streaming ``verify_chain``; on verify failure the
chain refuses further WRITES until a human acknowledges (failure mode F7) —
reads keep working.

Every payload passes the platform PII redaction before it is hashed or
stored, so no PAN/NID/mobile/email can rest in the audit trail.
"""

from __future__ import annotations

from datetime import datetime

from bdpay.platform.canonical import sha256_canonical
from bdpay.platform.clock import Clock
from bdpay.platform.errors import InternalError
from bdpay.platform.pii import redact
from bdpay.vault.ids import make_vault_id

__all__ = ["AuditChainWriteRefusedError", "VaultAuditChain", "VaultAuditEntry"]

GENESIS_HASH = "0" * 64


class AuditChainWriteRefusedError(InternalError):
    """F7: chain verification failed — writes refused until acknowledged."""

    default_code = "audit_chain_failed"


class VaultAuditEntry:
    """One immutable chain row (vault_audit_chain columns)."""

    __slots__ = (
        "actor",
        "chain_index",
        "entry_hash",
        "entry_type",
        "payload",
        "payload_hash",
        "prev_hash",
        "produced_at",
        "vaud_id",
    )

    def __init__(
        self,
        *,
        vaud_id: str,
        chain_index: int,
        prev_hash: str,
        entry_type: str,
        payload: dict,
        payload_hash: str,
        actor: str,
        produced_at: datetime,
        entry_hash: str,
    ) -> None:
        self.vaud_id = vaud_id
        self.chain_index = chain_index
        self.prev_hash = prev_hash
        self.entry_type = entry_type
        self.payload = payload
        self.payload_hash = payload_hash
        self.actor = actor
        self.produced_at = produced_at
        self.entry_hash = entry_hash


def _redact_payload(value: object) -> object:
    """Recursively apply the platform PII redaction to every string leaf."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {key: _redact_payload(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_redact_payload(item) for item in value]
    return value


def _entry_preimage(
    prev_hash: str,
    chain_index: int,
    entry_type: str,
    payload_hash: str,
    actor: str,
    produced_at: datetime,
) -> dict:
    return {
        "prev_hash": prev_hash,
        "chain_index": chain_index,
        "entry_type": entry_type,
        "payload_hash": payload_hash,
        "actor": actor,
        "produced_at": produced_at,
    }


class VaultAuditChain:
    """In-memory hash chain store (DDL mirror: db/migrations/0060_vault.sql)."""

    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self._entries: list[VaultAuditEntry] = []
        self._write_refused = False
        self.last_verified_at: datetime | None = None

    @property
    def entries(self) -> tuple[VaultAuditEntry, ...]:
        return tuple(self._entries)

    @property
    def write_refused(self) -> bool:
        return self._write_refused

    def append(self, entry_type: str, payload: dict, *, actor: str) -> VaultAuditEntry:
        if self._write_refused:
            raise AuditChainWriteRefusedError(
                "vault audit chain verification failed; writes refused pending acknowledgement"
            )
        clean_payload = _redact_payload(payload)
        prev_hash = self._entries[-1].entry_hash if self._entries else GENESIS_HASH
        chain_index = len(self._entries)
        payload_hash = sha256_canonical(clean_payload)
        produced_at = self._clock.now()
        preimage = _entry_preimage(
            prev_hash, chain_index, entry_type, payload_hash, actor, produced_at
        )
        entry = VaultAuditEntry(
            vaud_id=make_vault_id(
                "vaud",
                {"chain_index": chain_index, "prev_hash": prev_hash, "payload_hash": payload_hash},
            ),
            chain_index=chain_index,
            prev_hash=prev_hash,
            entry_type=entry_type,
            payload=clean_payload,  # type: ignore[arg-type]
            payload_hash=payload_hash,
            actor=actor,
            produced_at=produced_at,
            entry_hash=sha256_canonical(preimage),
        )
        self._entries.append(entry)
        return entry

    def verify_chain(self) -> bool:
        """Streaming re-verification; a mismatch flips the write-refusal latch."""
        prev_hash = GENESIS_HASH
        for index, entry in enumerate(self._entries):
            preimage = _entry_preimage(
                prev_hash,
                entry.chain_index,
                entry.entry_type,
                entry.payload_hash,
                entry.actor,
                entry.produced_at,
            )
            if (
                entry.chain_index != index
                or entry.prev_hash != prev_hash
                or entry.payload_hash != sha256_canonical(entry.payload)
                or entry.entry_hash != sha256_canonical(preimage)
            ):
                self._write_refused = True
                return False
            prev_hash = entry.entry_hash
        self.last_verified_at = self._clock.now()
        return True

    def acknowledge_failure(self, *, operator_id: str) -> None:
        """Human acknowledgement re-arms writes (F7) and records who did it."""
        self._write_refused = False
        self.append(
            "audit_chain_failure_acknowledged", {"operator_id": operator_id}, actor=operator_id
        )

    def of_type(self, entry_type: str) -> list[VaultAuditEntry]:
        return [entry for entry in self._entries if entry.entry_type == entry_type]

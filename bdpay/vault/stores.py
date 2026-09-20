"""Deterministic in-memory stores for the vault entities (spec/14 §Data model).

The unit suite runs with no infrastructure (IMPLEMENTATION.md storage
strategy); the matching Postgres DDL ships in ``db/migrations/0060_vault.sql``
and targets the dedicated ``vault_db`` database. Following the lane-B
connector-core precedent, the runtime repositories here are the in-memory
implementations; the psycopg implementations land with the integration
environment (no platform role can see ``vault_db`` either way).
"""

from __future__ import annotations

from datetime import datetime

from bdpay.vault.records import (
    AllowlistRecord,
    CardTokenRecord,
    CustodianRecord,
    DetokDecision,
    DetokenizationRecord,
    EncryptedPanRecord,
    IdempotencyRecord,
    IntakeSessionRecord,
    KeyCeremonyRecord,
    KeyKind,
    KeyStatus,
    ReencryptionRunRecord,
    TokenAliasRecord,
    VaultKeyRecord,
    VaultOutboxRecord,
)

__all__ = [
    "AllowlistStore",
    "CeremonyStore",
    "CustodianStore",
    "DetokAuditStore",
    "IdempotencyStore",
    "InMemoryPublisher",
    "KeyStore",
    "OutboxStore",
    "PanStore",
    "ReencryptionRunStore",
    "SessionStore",
    "TokenAliasStore",
    "TokenStore",
]


class KeyStore:
    def __init__(self) -> None:
        self._rows: dict[str, VaultKeyRecord] = {}

    def save(self, record: VaultKeyRecord) -> None:
        self._rows[record.vkey_id] = record

    def get(self, vkey_id: str) -> VaultKeyRecord | None:
        return self._rows.get(vkey_id)

    def all(self) -> list[VaultKeyRecord]:
        return list(self._rows.values())

    def active(self, kind: KeyKind, key_name: str | None = None) -> VaultKeyRecord | None:
        """The single ACTIVE key per (kind, logical name) — UNIQUE-index mirror."""
        for record in self._rows.values():
            if (
                record.kind is kind
                and record.status is KeyStatus.ACTIVE
                and (key_name is None or record.openbao_key_name == key_name)
            ):
                return record
        return None

    def generations(self, kind: KeyKind, key_name: str | None = None) -> list[VaultKeyRecord]:
        rows = [
            record
            for record in self._rows.values()
            if record.kind is kind and (key_name is None or record.openbao_key_name == key_name)
        ]
        return sorted(rows, key=lambda record: record.generation)


class CustodianStore:
    def __init__(self) -> None:
        self._rows: dict[str, CustodianRecord] = {}

    def save(self, record: CustodianRecord) -> None:
        self._rows[record.vcus_id] = record

    def get(self, vcus_id: str) -> CustodianRecord | None:
        return self._rows.get(vcus_id)

    def by_operator(self, operator_identity: str) -> CustodianRecord | None:
        for record in self._rows.values():
            if record.operator_identity == operator_identity:
                return record
        return None

    def active(self) -> list[CustodianRecord]:
        rows = [record for record in self._rows.values() if record.status == "ACTIVE"]
        return sorted(rows, key=lambda record: record.vcus_id)


class CeremonyStore:
    def __init__(self) -> None:
        self._rows: dict[str, KeyCeremonyRecord] = {}

    def save(self, record: KeyCeremonyRecord) -> None:
        self._rows[record.vcer_id] = record

    def get(self, vcer_id: str) -> KeyCeremonyRecord | None:
        return self._rows.get(vcer_id)

    def all(self) -> list[KeyCeremonyRecord]:
        return list(self._rows.values())


class TokenStore:
    def __init__(self) -> None:
        self._rows: dict[str, CardTokenRecord] = {}

    def save(self, record: CardTokenRecord) -> None:
        self._rows[record.token] = record

    def get(self, token: str) -> CardTokenRecord | None:
        return self._rows.get(token)

    def by_pan_hmac(self, pan_hmac: str, pepper_kid: str) -> CardTokenRecord | None:
        """UNIQUE (pan_hmac, pepper_kid) — same PAN => same token per pepper."""
        for record in self._rows.values():
            if record.pan_hmac == pan_hmac and record.pepper_kid == pepper_kid:
                return record
        return None

    def all(self) -> list[CardTokenRecord]:
        return list(self._rows.values())


class PanStore:
    def __init__(self) -> None:
        self._rows: dict[str, EncryptedPanRecord] = {}  # keyed by token (UNIQUE)

    def save(self, record: EncryptedPanRecord) -> None:
        self._rows[record.token] = record

    def get_by_token(self, token: str) -> EncryptedPanRecord | None:
        return self._rows.get(token)

    def delete_by_token(self, token: str) -> bool:
        return self._rows.pop(token, None) is not None

    def by_dek(self, dek_key_id: str) -> list[EncryptedPanRecord]:
        rows = [record for record in self._rows.values() if record.dek_key_id == dek_key_id]
        return sorted(rows, key=lambda record: record.epan_id)

    def all(self) -> list[EncryptedPanRecord]:
        return list(self._rows.values())


class SessionStore:
    def __init__(self) -> None:
        self._rows: dict[str, IntakeSessionRecord] = {}

    def save(self, record: IntakeSessionRecord) -> None:
        self._rows[record.vint_id] = record

    def get(self, vint_id: str) -> IntakeSessionRecord | None:
        return self._rows.get(vint_id)

    def all(self) -> list[IntakeSessionRecord]:
        return list(self._rows.values())


class DetokAuditStore:
    """detokenization_audit — append rows; UPDATE only nulls stored_response."""

    def __init__(self) -> None:
        self._rows: list[DetokenizationRecord] = []

    def append(self, record: DetokenizationRecord) -> None:
        self._rows.append(record)

    def allowed_by_connector_ref(self, connector_ref: str) -> DetokenizationRecord | None:
        for record in self._rows:
            if record.connector_ref == connector_ref and record.decision is DetokDecision.ALLOWED:
                return record
        return None

    def all(self) -> list[DetokenizationRecord]:
        return list(self._rows)

    def clear_expired_responses(self, now: datetime) -> int:
        """The single permitted mutation: null stored_response past the
        24 h replay window (DDL trigger mirror)."""
        cleared = 0
        for record in self._rows:
            if (
                record.stored_response is not None
                and record.replay_expires_at is not None
                and now >= record.replay_expires_at
            ):
                record.stored_response = None
                cleared += 1
        return cleared


class AllowlistStore:
    def __init__(self) -> None:
        self._rows: dict[str, AllowlistRecord] = {}

    def save(self, record: AllowlistRecord) -> None:
        self._rows[record.caller_san] = record

    def get(self, caller_san: str) -> AllowlistRecord | None:
        return self._rows.get(caller_san)

    def all(self) -> list[AllowlistRecord]:
        return list(self._rows.values())


class TokenAliasStore:
    def __init__(self) -> None:
        self._rows: dict[str, TokenAliasRecord] = {}  # keyed by old_token (PK)

    def save(self, record: TokenAliasRecord) -> None:
        self._rows[record.old_token] = record

    def resolve(self, old_token: str) -> TokenAliasRecord | None:
        return self._rows.get(old_token)

    def all(self) -> list[TokenAliasRecord]:
        return list(self._rows.values())


class ReencryptionRunStore:
    def __init__(self) -> None:
        self._rows: dict[str, ReencryptionRunRecord] = {}

    def save(self, record: ReencryptionRunRecord) -> None:
        self._rows[record.vrun_id] = record

    def get(self, vrun_id: str) -> ReencryptionRunRecord | None:
        return self._rows.get(vrun_id)

    def all(self) -> list[ReencryptionRunRecord]:
        return list(self._rows.values())


class IdempotencyStore:
    """vault_idempotency_keys — Plane-B mutating endpoints, vault_db only
    (Redis is out-of-CDE and never used by the vault), 24 h sweep."""

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str], IdempotencyRecord] = {}

    def get(self, idempotency_key: str, caller_san: str) -> IdempotencyRecord | None:
        return self._rows.get((idempotency_key, caller_san))

    def save(self, record: IdempotencyRecord) -> None:
        self._rows[(record.idempotency_key, record.caller_san)] = record

    def purge_older_than(self, cutoff: datetime) -> int:
        stale = [key for key, record in self._rows.items() if record.created_at < cutoff]
        for key in stale:
            del self._rows[key]
        return len(stale)


class OutboxStore:
    """vault_outbox — sole event egress (conventions §5; the vault cannot
    write the platform outbox table)."""

    def __init__(self) -> None:
        self._rows: list[VaultOutboxRecord] = []

    def append(self, record: VaultOutboxRecord) -> None:
        self._rows.append(record)

    def all(self) -> list[VaultOutboxRecord]:
        return list(self._rows)

    def pending(self) -> list[VaultOutboxRecord]:
        return [record for record in self._rows if record.published_at is None]

    def of_type(self, event_type: str) -> list[VaultOutboxRecord]:
        return [record for record in self._rows if record.event_type == event_type]

    def relay(self, publisher: InMemoryPublisher, *, now: datetime) -> int:
        """Drain unpublished rows to the (outbound-only) publisher port."""
        published = 0
        for record in self.pending():
            record.attempts += 1
            publisher.publish(record)
            record.published_at = now
            published += 1
        return published


class InMemoryPublisher:
    """Outbound-only publisher port stand-in (Redpanda topic vault.events)."""

    def __init__(self) -> None:
        self.published: list[dict] = []

    def publish(self, record: VaultOutboxRecord) -> None:
        self.published.append(
            {
                "event_id": record.vobx_id,
                "type": record.event_type,
                "subject_type": record.subject_type,
                "subject_id": record.subject_id,
                "payload": record.payload,
            }
        )

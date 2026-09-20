"""Entity records and status enums for the vault package (spec/14 §Entities, §Data model).

Statuses are str Enums whose ``.value`` is the UPPER_SNAKE_CASE state name
from the spec FSM tables. Canonical-JSON payloads always receive ``.value``
explicitly (the E12 serializer lowercases str-subclass Enums, which would
mangle the spec state spelling if an Enum object leaked into a preimage).

All records are plain mutable dataclasses backing the in-memory stores; the
Postgres DDL lives in ``db/migrations/0060_vault.sql`` (same columns).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

__all__ = [
    "AllowlistRecord",
    "CardTokenRecord",
    "CeremonyShareRecord",
    "CeremonyStatus",
    "CustodianRecord",
    "DetokDecision",
    "DetokenizationRecord",
    "EncryptedPanRecord",
    "IdempotencyRecord",
    "IntakeSessionRecord",
    "IntakeSessionStatus",
    "KeyCeremonyRecord",
    "KeyKind",
    "KeyStatus",
    "ReencryptionRunRecord",
    "ReencryptionStatus",
    "TokenAliasRecord",
    "TokenStatus",
    "VaultKeyRecord",
    "VaultOutboxRecord",
]


class TokenStatus(StrEnum):
    PENDING_FIRST_AUTH = "PENDING_FIRST_AUTH"
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"
    RETIRED = "RETIRED"  # terminal
    EXPIRED = "EXPIRED"  # terminal


TOKEN_TERMINAL = frozenset({TokenStatus.RETIRED, TokenStatus.EXPIRED})


class IntakeSessionStatus(StrEnum):
    CREATED = "CREATED"
    CONSUMED = "CONSUMED"  # terminal
    EXPIRED = "EXPIRED"  # terminal
    VOIDED = "VOIDED"  # terminal


class KeyKind(StrEnum):
    LMK = "LMK"
    KEK = "KEK"
    DEK = "DEK"
    PEPPER = "PEPPER"
    PII_KEY = "PII_KEY"
    EGRESS_HMAC = "EGRESS_HMAC"


class KeyStatus(StrEnum):
    PENDING_CEREMONY = "PENDING_CEREMONY"
    ACTIVE = "ACTIVE"
    DEPRECATED = "DEPRECATED"
    COMPROMISED = "COMPROMISED"
    DESTROYED = "DESTROYED"  # terminal


class CeremonyStatus(StrEnum):
    DRAFT = "DRAFT"
    SHARES_PENDING = "SHARES_PENDING"
    QUORUM_REACHED = "QUORUM_REACHED"
    EXECUTED = "EXECUTED"  # terminal
    ABORTED = "ABORTED"  # terminal


class DetokDecision(StrEnum):
    ALLOWED = "ALLOWED"
    DENIED = "DENIED"


class ReencryptionStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


#: Crypto periods (days) per spec/14 §State machines (BB ICT §5.8 alignment).
#: ``None`` = rotate on compromise only (PEPPER).
CRYPTO_PERIOD_DAYS: dict[KeyKind, int | None] = {
    KeyKind.LMK: 730,
    KeyKind.KEK: 365,
    KeyKind.DEK: 90,
    KeyKind.PEPPER: None,
    KeyKind.PII_KEY: 365,
    KeyKind.EGRESS_HMAC: 90,
}


@dataclass
class VaultKeyRecord:
    """vault_keys row — key hierarchy metadata; never plaintext material."""

    vkey_id: str
    kind: KeyKind
    generation: int
    status: KeyStatus
    kcv: str
    wrapped_key_material: str | None  # KEK-wrapped (DEK/PEPPER) else None
    openbao_key_name: str | None  # KEK/PII_KEY/EGRESS_HMAC logical key name
    openbao_key_version: int | None
    ceremony_id: str | None
    created_at: datetime
    activated_at: datetime | None = None
    deprecated_at: datetime | None = None
    destroyed_at: datetime | None = None
    destroy_receipt_hash: str | None = None
    rotation_due_at: datetime | None = None  # additive: rotation schedule record


@dataclass
class CustodianRecord:
    vcus_id: str
    person_name: str
    operator_identity: str
    reporting_line: str
    status: str  # ACTIVE | REVOKED
    appointed_at: datetime
    appointment_approval_id: str
    revoked_at: datetime | None = None


@dataclass
class CeremonyShareRecord:
    ceremony_id: str
    custodian_id: str
    share_kcv: str
    attestation_hash: str
    attested_at: datetime


@dataclass
class KeyCeremonyRecord:
    vcer_id: str
    kind: KeyKind
    target_key_name: str
    reason: str  # initial | scheduled_rotation | compromise | custodian_change
    quorum: int
    custodian_ids: tuple[str, ...]
    status: CeremonyStatus
    approval_request_id: str
    opened_at: datetime
    ttl_expires_at: datetime  # opened_at + 7 days
    quorum_at: datetime | None = None
    executed_at: datetime | None = None
    aborted_at: datetime | None = None
    evidence_pack_ref: str | None = None
    shares: list[CeremonyShareRecord] = field(default_factory=list)


@dataclass
class CardTokenRecord:
    token: str
    pan_hmac: str
    pepper_kid: str
    bin: str
    last4: str
    scheme: str
    expiry_month: str
    expiry_year: str
    status: TokenStatus
    origin: str  # intake | import
    merchant_id: str
    created_at: datetime
    customer_id: str | None = None
    holder_name_ct: bytes | None = None
    holder_name_nonce: bytes | None = None
    holder_name_dek: str | None = None
    first_used_at: datetime | None = None
    last_used_at: datetime | None = None
    last_seen_at: datetime | None = None  # additive column (errata LB8)
    terminal_at: datetime | None = None
    retire_reason: str | None = None


@dataclass
class EncryptedPanRecord:
    epan_id: str
    token: str
    dek_key_id: str
    nonce: bytes
    pan_ciphertext: bytes
    aad: str
    created_at: datetime
    reencrypted_at: datetime | None = None
    destroy_after: datetime | None = None


@dataclass
class IntakeSessionRecord:
    vint_id: str
    merchant_id: str
    purpose: str  # payment | save_card
    payment_intent_id: str | None
    customer_id: str | None
    allowed_origins: tuple[str, ...]
    cvv_required: bool
    status: IntakeSessionStatus
    failed_attempts: int
    created_at: datetime
    expires_at: datetime
    token: str | None = None
    terminal_at: datetime | None = None


@dataclass
class DetokenizationRecord:
    instruction_id: str
    connector_ref: str
    token: str
    caller_san: str
    decision: DetokDecision
    grant_hash: str
    created_at: datetime
    denial_code: str | None = None
    acquirer_host: str | None = None
    sad_consumed: bool = False
    egress_at: datetime | None = None
    responded_at: datetime | None = None
    acquirer_status: int | None = None
    raw_response_hash: str | None = None
    stored_response: dict | None = None  # PAN-stripped, 24h replay only
    replay_expires_at: datetime | None = None


@dataclass
class AllowlistRecord:
    caller_san: str
    operations: tuple[str, ...]
    approval_request_id: str
    created_at: datetime
    revoked_at: datetime | None = None


@dataclass
class TokenAliasRecord:
    old_token: str
    new_token: str
    old_pepper_kid: str
    new_pepper_kid: str
    migrated_at: datetime


@dataclass
class ReencryptionRunRecord:
    vrun_id: str
    scope: str
    old_key_id: str
    new_key_id: str
    ceremony_id: str | None
    status: ReencryptionStatus
    deadline_at: datetime
    total_rows: int | None = None
    done_rows: int = 0
    cursor_ref: str | None = None
    rate_cap_rps: int = 50
    started_at: datetime | None = None
    completed_at: datetime | None = None
    stats: dict = field(default_factory=dict)


@dataclass
class IdempotencyRecord:
    idempotency_key: str
    caller_san: str
    request_hash: str
    response_status: int
    response_body: dict
    created_at: datetime


@dataclass
class VaultOutboxRecord:
    vobx_id: str
    event_type: str
    subject_type: str
    subject_id: str
    payload: dict
    occurred_at: datetime
    published_at: datetime | None = None
    attempts: int = 0

"""Merchant API keys — records, repository protocol, auth service (spec/01).

Lifecycle FSM (spec/01 §State machines 1, refusal-first):
``ACTIVE --[rotate]--> ROTATION_PENDING --[grace elapsed | force]--> REVOKED``;
``ACTIVE --[revoke]--> REVOKED``; ``ACTIVE --[expiry]--> EXPIRED``. Terminal
states deny everything.

Authentication paths:

- **Bearer** — constant-time bcrypt check of the presented secret against
  every candidate hash for the key prefix (spec/01 §B bearer algorithm).
- **HMAC-signed** (system-to-system) — timestamp freshness, HKDF-derived
  per-key HMAC key decrypted from rest, constant-time signature compare,
  replay refusal on (key_id, timestamp, signature).

Secrets at rest: bcrypt only; the HMAC key is AES-256-GCM sealed. The raw
secret exists exactly once, in the creation response.
"""

from __future__ import annotations

import ipaddress
import threading
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any, Protocol, runtime_checkable

from bdpay.gateway import credentials
from bdpay.gateway.config import GatewaySettings
from bdpay.gateway.ids_ext import make_gateway_id
from bdpay.gateway.principals import Principal
from bdpay.platform.clock import Clock
from bdpay.platform.errors import (
    AuthenticationError,
    AuthorizationError,
    ConflictError,
    InvalidRequestError,
    NotFoundError,
)

__all__ = [
    "API_SCOPES",
    "ApiKeyRecord",
    "ApiKeyRepository",
    "ApiKeyService",
    "CreatedApiKey",
    "InMemoryApiKeyRepository",
    "InMemoryReplayGuard",
    "ReplayGuard",
]

#: Closed scope catalogue (spec/01 DDL ``api_scope`` enum).
API_SCOPES: frozenset[str] = frozenset(
    {
        "payment:write",
        "payment:read",
        "refund:write",
        "refund:read",
        "customer:write",
        "customer:read",
        "webhook:write",
        "webhook:read",
        "settlement:read",
        "apikey:write",
        "apikey:read",
        "otp:request",
        # Additive (spec/13 Bangla QR acquirer surface).
        "qr:write",
        "qr:read",
        # Additive (spec/18 merchant offer-management surface).
        "offers:write",
        "offers:read",
        # Additive (spec/17 subscription management surface).
        "subscriptions:write",
        "subscriptions:read",
        # Additive (spec/17 bulk disbursement surface).
        "disbursements:write",
        "disbursements:read",
    }
)

_STATUSES = ("ACTIVE", "ROTATION_PENDING", "REVOKED", "EXPIRED")


@dataclass(frozen=True)
class ApiKeyRecord:
    """One ``api_keys`` row (+ child scopes), migration 0080."""

    key_id: str
    merchant_id: str
    key_name: str
    key_prefix: str
    secret_hash: str
    hmac_key_enc: str
    scopes: tuple[str, ...]
    status: str = "ACTIVE"
    ip_allowlist: tuple[str, ...] = ()
    expires_at: datetime | None = None
    rotation_successor: str | None = None
    rotation_grace_ends_at: datetime | None = None
    last_used_at: datetime | None = None
    created_at: datetime | None = None
    revoked_at: datetime | None = None
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.status not in _STATUSES:
            raise ValueError(f"status must be one of {_STATUSES}, got {self.status!r}")
        unknown = set(self.scopes) - API_SCOPES
        if unknown:
            raise ValueError(f"unknown scopes {sorted(unknown)}; catalogue is closed")


@dataclass(frozen=True)
class CreatedApiKey:
    """Creation/rotation result: the record plus the once-only raw secret."""

    record: ApiKeyRecord
    secret: str


@runtime_checkable
class ApiKeyRepository(Protocol):
    """Storage contract for API keys (in-memory + Postgres, migration 0080)."""

    def insert(self, record: ApiKeyRecord, *, conn: Any | None = None) -> None: ...

    def save(self, record: ApiKeyRecord) -> None: ...

    def rotate_secret_atomic(
        self,
        key_id: str,
        *,
        merchant_id: str,
        new_secret_hash: str,
        new_hmac_key_enc: str,
    ) -> ApiKeyRecord: ...

    def rotate_key_atomic(
        self,
        original: ApiKeyRecord,
        successor: ApiKeyRecord,
        *,
        grace_ends_at: datetime,
    ) -> ApiKeyRecord: ...

    def get(self, key_id: str) -> ApiKeyRecord | None: ...

    def candidates_by_prefix(self, key_prefix: str) -> Sequence[ApiKeyRecord]: ...

    def list_for_merchant(self, merchant_id: str) -> Sequence[ApiKeyRecord]: ...


class InMemoryApiKeyRepository:
    """Deterministic in-memory repository for unit tests."""

    def __init__(self) -> None:
        self._rows: dict[str, ApiKeyRecord] = {}
        self._order: list[str] = []
        self._lock = threading.RLock()

    def insert(self, record: ApiKeyRecord, *, conn: Any | None = None) -> None:
        del conn  # in-memory repo ignores the caller's transaction
        with self._lock:
            self._insert_locked(record)

    def _insert_locked(self, record: ApiKeyRecord) -> None:
        if record.key_id in self._rows:
            raise ValueError(f"api key {record.key_id!r} already exists")
        for existing in self._rows.values():
            if (
                existing.merchant_id == record.merchant_id
                and existing.key_name == record.key_name
            ):
                raise ValueError("duplicate (merchant_id, key_name)")
        self._rows[record.key_id] = record
        self._order.append(record.key_id)

    def save(self, record: ApiKeyRecord) -> None:
        with self._lock:
            current = self._rows.get(record.key_id)
            if current is None:
                raise ValueError(f"unknown api key {record.key_id!r}")
            self._rows[record.key_id] = replace(
                current,
                status=record.status,
                rotation_successor=record.rotation_successor,
                rotation_grace_ends_at=record.rotation_grace_ends_at,
                last_used_at=record.last_used_at,
                revoked_at=record.revoked_at,
            )

    def rotate_secret_atomic(
        self,
        key_id: str,
        *,
        merchant_id: str,
        new_secret_hash: str,
        new_hmac_key_enc: str,
    ) -> ApiKeyRecord:
        with self._lock:
            record = self._rows.get(key_id)
            if record is None or record.merchant_id != merchant_id:
                raise NotFoundError("api key not found", code="api_key_not_found")
            if record.status != "ACTIVE" or record.rotation_successor is not None:
                raise ConflictError(
                    "only ACTIVE keys without a rotation successor can rotate their secret",
                    code="api_key_not_active",
                )
            updated = replace(
                record,
                secret_hash=new_secret_hash,
                hmac_key_enc=new_hmac_key_enc,
                status="ACTIVE",
                rotation_successor=None,
                rotation_grace_ends_at=None,
            )
            self._rows[key_id] = updated
            return updated

    def rotate_key_atomic(
        self,
        original: ApiKeyRecord,
        successor: ApiKeyRecord,
        *,
        grace_ends_at: datetime,
    ) -> ApiKeyRecord:
        with self._lock:
            current = self._rows.get(original.key_id)
            if current is None:
                raise AuthenticationError("api key not found", code="api_key_invalid")
            if current.status != "ACTIVE" or current.rotation_successor is not None:
                raise AuthenticationError(
                    "only ACTIVE keys can rotate (refusal-first FSM)",
                    code="api_key_invalid",
                )
            if (
                current.secret_hash != original.secret_hash
                or current.hmac_key_enc != original.hmac_key_enc
            ):
                raise ConflictError(
                    "api key changed during rotation; retry with the latest key state",
                    code="api_key_rotation_conflict",
                )
            self._insert_locked(successor)
            updated = replace(
                current,
                status="ROTATION_PENDING",
                rotation_successor=successor.key_id,
                rotation_grace_ends_at=grace_ends_at,
            )
            self._rows[original.key_id] = updated
            return updated

    def get(self, key_id: str) -> ApiKeyRecord | None:
        with self._lock:
            return self._rows.get(key_id)

    def candidates_by_prefix(self, key_prefix: str) -> Sequence[ApiKeyRecord]:
        with self._lock:
            return [
                self._rows[key_id]
                for key_id in self._order
                if self._rows[key_id].key_prefix == key_prefix
                and self._rows[key_id].status in ("ACTIVE", "ROTATION_PENDING")
            ]

    def list_for_merchant(self, merchant_id: str) -> Sequence[ApiKeyRecord]:
        with self._lock:
            return [
                self._rows[key_id]
                for key_id in self._order
                if self._rows[key_id].merchant_id == merchant_id
            ]


@runtime_checkable
class ReplayGuard(Protocol):
    """Once-only marker store for HMAC-signed requests (spec/01 §A step 3)."""

    def first_use(self, marker: str, *, now: datetime, ttl_seconds: int) -> bool: ...


class InMemoryReplayGuard:
    """In-memory replay marker store; entries expire after their TTL."""

    def __init__(self) -> None:
        self._seen: dict[str, datetime] = {}

    def first_use(self, marker: str, *, now: datetime, ttl_seconds: int) -> bool:
        expired = [key for key, at in self._seen.items() if at <= now]
        for key in expired:
            del self._seen[key]
        if marker in self._seen:
            return False
        self._seen[marker] = now + timedelta(seconds=ttl_seconds)
        return True


def _ip_allowed(client_ip: str, allowlist: tuple[str, ...]) -> bool:
    if not allowlist:
        return True
    try:
        addr = ipaddress.ip_address(client_ip)
    except ValueError:
        return False
    for entry in allowlist:
        try:
            if addr in ipaddress.ip_network(entry, strict=False):
                return True
        except ValueError:
            continue
    return False


class ApiKeyService:
    """Create / rotate / revoke / authenticate merchant API keys."""

    def __init__(
        self,
        repository: ApiKeyRepository,
        *,
        clock: Clock,
        settings: GatewaySettings,
        replay_guard: ReplayGuard | None = None,
    ) -> None:
        self._repo = repository
        self._clock = clock
        self._settings = settings
        self._replay_guard = replay_guard if replay_guard is not None else InMemoryReplayGuard()

    # -- lifecycle ---------------------------------------------------------

    def create_key(
        self,
        *,
        merchant_id: str,
        key_name: str,
        scopes: Sequence[str],
        ip_allowlist: Sequence[str] = (),
        expires_at: datetime | None = None,
        env: str = "live",
        conn: Any | None = None,
        entitlement: frozenset[str] | None = None,
    ) -> CreatedApiKey:
        if env not in ("live", "test"):
            raise InvalidRequestError("env must be live or test", code="invalid_key_env")
        if not merchant_id or not key_name:
            raise InvalidRequestError(
                "merchant_id and key_name are required", code="invalid_request"
            )
        unknown = set(scopes) - API_SCOPES
        if unknown:
            raise InvalidRequestError(
                f"unknown scopes {sorted(unknown)}", code="unknown_scope"
            )
        # Scope-escalation guard (developer-portal create endpoint): when the
        # caller's entitlement envelope is supplied, the new key may only carry
        # scopes the creating principal itself holds. There is no pre-existing
        # per-merchant entitlement table, so the boundary is the calling key's
        # own scopes (apikey:write is required to reach the endpoint; the new
        # key's scopes must be a subset of the caller's).
        if entitlement is not None:
            excess = set(scopes) - entitlement
            if excess:
                raise AuthorizationError(
                    "cannot grant scopes beyond the caller's entitlement",
                    code="scope_escalation",
                )
        now = self._clock.now()
        prefix = credentials.KEY_PREFIX_LIVE if env == "live" else credentials.KEY_PREFIX_TEST
        key_id = make_gateway_id(
            "akey", {"merchant_id": merchant_id, "key_name": key_name, "created_at": now}
        )
        secret = credentials.generate_api_key_secret(prefix)
        record = ApiKeyRecord(
            key_id=key_id,
            merchant_id=merchant_id,
            key_name=key_name,
            key_prefix=prefix,
            secret_hash=credentials.hash_secret(secret, rounds=self._settings.bcrypt_rounds),
            hmac_key_enc=credentials.encrypt_blob(
                self._settings.hmac_master_key,
                credentials.derive_hmac_key(secret, key_id),
            ),
            scopes=tuple(dict.fromkeys(scopes)),
            ip_allowlist=tuple(ip_allowlist),
            expires_at=expires_at,
            created_at=now,
        )
        self._repo.insert(record, conn=conn)
        return CreatedApiKey(record=record, secret=secret)

    def rotate_key(self, key_id: str, *, grace_hours: int = 24) -> CreatedApiKey:
        """ACTIVE -> ROTATION_PENDING; issues the successor key."""
        if not 1 <= grace_hours <= 72:
            raise InvalidRequestError(
                "grace_period_hours must be between 1 and 72", code="invalid_grace_period"
            )
        record = self._repo.get(key_id)
        if record is None:
            raise AuthenticationError("api key not found", code="api_key_invalid")
        if record.status != "ACTIVE":
            raise AuthenticationError(
                "only ACTIVE keys can rotate (refusal-first FSM)", code="api_key_invalid"
            )
        now = self._clock.now()
        successor_name = f"{record.key_name} (rot {now.strftime('%Y%m%dT%H%M%S')})"
        successor_key_id = make_gateway_id(
            "akey",
            {
                "merchant_id": record.merchant_id,
                "key_name": successor_name,
                "created_at": now,
            },
        )
        successor_secret = credentials.generate_api_key_secret(record.key_prefix)
        successor_record = ApiKeyRecord(
            key_id=successor_key_id,
            merchant_id=record.merchant_id,
            # unique (merchant_id, key_name): suffix the rotation generation
            key_name=successor_name,
            key_prefix=record.key_prefix,
            secret_hash=credentials.hash_secret(
                successor_secret, rounds=self._settings.bcrypt_rounds
            ),
            hmac_key_enc=credentials.encrypt_blob(
                self._settings.hmac_master_key,
                credentials.derive_hmac_key(successor_secret, successor_key_id),
            ),
            scopes=record.scopes,
            ip_allowlist=record.ip_allowlist,
            expires_at=record.expires_at,
            created_at=now,
        )
        self._repo.rotate_key_atomic(
            record,
            successor_record,
            grace_ends_at=now + timedelta(hours=grace_hours),
        )
        return CreatedApiKey(record=successor_record, secret=successor_secret)

    def revoke_key(self, key_id: str) -> None:
        record = self._repo.get(key_id)
        if record is None:
            raise AuthenticationError("api key not found", code="api_key_invalid")
        if record.status in ("REVOKED", "EXPIRED"):
            raise AuthenticationError(
                "terminal api key states deny all transitions", code="api_key_invalid"
            )
        self._repo.save(replace(record, status="REVOKED", revoked_at=self._clock.now()))

    # -- developer-portal management surface (create / rotate / revoke) --------
    #
    # Endpoint-facing lifecycle methods. These reuse the SAME key format,
    # hashing (bcrypt), HMAC-key sealing (AES-256-GCM), record type, and
    # repository as the bootstrap mint path — they do NOT introduce a second
    # key scheme. They differ from the spec/01 FSM ``rotate_key``/``revoke_key``
    # (successor + grace, refusal-first-terminal) because the developer-portal
    # contract is: in-place immediate rotation (old secret stops working at
    # once, mirroring the webhook ``rotate_secret`` shape) and idempotent
    # revoke. Both are merchant-scoped: a foreign/unknown key_id yields an
    # identical 404 (no existence leak), matching the webhook rotate pattern.

    def rotate_secret(self, key_id: str, *, merchant_id: str) -> CreatedApiKey:
        """In-place immediate secret rotation (developer-portal endpoint).

        Same ``key_id``, new raw secret returned once; the old secret stops
        working immediately because ``secret_hash`` / ``hmac_key_enc`` are
        replaced (no grace window — the grace model is the spec/01 FSM
        ``rotate_key`` successor path, NOT this endpoint). Reuses
        ``credentials.generate_api_key_secret`` + ``hash_secret`` +
        ``encrypt_blob`` + ``derive_hmac_key`` so format/hashing are unchanged.
        """
        record = self._repo.get(key_id)
        if record is None or record.merchant_id != merchant_id:
            # unknown OR foreign key: identical refusal (no existence leak)
            raise NotFoundError("api key not found", code="api_key_not_found")
        if record.status != "ACTIVE":
            raise ConflictError(
                "only ACTIVE keys can rotate their secret", code="api_key_not_active"
            )
        new_secret = credentials.generate_api_key_secret(record.key_prefix)
        updated = self._repo.rotate_secret_atomic(
            key_id,
            merchant_id=merchant_id,
            new_secret_hash=credentials.hash_secret(
                new_secret, rounds=self._settings.bcrypt_rounds
            ),
            new_hmac_key_enc=credentials.encrypt_blob(
                self._settings.hmac_master_key,
                credentials.derive_hmac_key(new_secret, record.key_id),
            ),
        )
        return CreatedApiKey(record=updated, secret=new_secret)

    def revoke(self, key_id: str, *, merchant_id: str) -> ApiKeyRecord:
        """Idempotent, merchant-scoped revoke (developer-portal endpoint).

        Revoking an already-REVOKED key returns the record unchanged (200, no
        error). EXPIRED is also terminal and returned unchanged. Any other
        state transitions to REVOKED; ``_usable`` then denies authentication.
        """
        record = self._repo.get(key_id)
        if record is None or record.merchant_id != merchant_id:
            raise NotFoundError("api key not found", code="api_key_not_found")
        if record.status in ("REVOKED", "EXPIRED"):
            return record
        updated = replace(record, status="REVOKED", revoked_at=self._clock.now())
        self._repo.save(updated)
        return updated

    # -- reads -------------------------------------------------------------
    #
    # List a merchant's own keys for the developer-portal API-keys screen.
    # Returns the records (secret_hash/hmac_key_enc are NEVER carried over the
    # read boundary; the route layer maps to a secret-free view).

    def list_for_merchant(self, merchant_id: str) -> Sequence[ApiKeyRecord]:
        if not merchant_id:
            return ()
        return self._repo.list_for_merchant(merchant_id)

    # -- authentication ----------------------------------------------------

    def _usable(self, record: ApiKeyRecord, now: datetime) -> bool:
        if record.status == "ACTIVE":
            pass
        elif record.status == "ROTATION_PENDING":
            if record.rotation_grace_ends_at is None or now >= record.rotation_grace_ends_at:
                return False
        else:
            return False
        return not (record.expires_at is not None and now >= record.expires_at)

    def _to_principal(self, record: ApiKeyRecord) -> Principal:
        env = "live" if record.key_prefix == credentials.KEY_PREFIX_LIVE else "test"
        return Principal(
            kind="merchant_key",
            principal_id=record.key_id,
            merchant_id=record.merchant_id,
            scopes=frozenset(record.scopes),
            key_env=env,
        )

    def _enforce_ip(self, record: ApiKeyRecord, client_ip: str) -> None:
        # spec/01 open question 6: test keys bypass IP allowlists.
        if record.key_prefix == credentials.KEY_PREFIX_TEST:
            return
        if not _ip_allowed(client_ip, record.ip_allowlist):
            raise AuthorizationError(
                "request address is not in the key allowlist", code="ip_not_allowed"
            )

    def authenticate_bearer(self, raw_secret: str, *, client_ip: str) -> Principal:
        """spec/01 §B bearer algorithm; constant-time hash checks."""
        now = self._clock.now()
        prefix = raw_secret[: len(credentials.KEY_PREFIX_LIVE)]
        if prefix not in (credentials.KEY_PREFIX_LIVE, credentials.KEY_PREFIX_TEST):
            raise AuthenticationError("api key not recognized", code="api_key_invalid")
        expired_match = False
        for record in self._repo.candidates_by_prefix(prefix):
            if not credentials.verify_secret(raw_secret, record.secret_hash):
                continue
            if not self._usable(record, now):
                expired_match = True
                continue
            self._enforce_ip(record, client_ip)
            return self._to_principal(record)
        if expired_match:
            raise AuthenticationError("api key has expired", code="api_key_expired")
        raise AuthenticationError("api key not recognized", code="api_key_invalid")

    def authenticate_hmac(
        self,
        *,
        key_id: str,
        signature: str,
        timestamp: str,
        method: str,
        path: str,
        body: bytes,
        client_ip: str,
    ) -> Principal:
        """spec/01 §A HMAC-signed verification (system-to-system)."""
        now = self._clock.now()
        try:
            ts = int(timestamp, 10)
        except (ValueError, TypeError) as exc:
            raise AuthenticationError(
                "signature timestamp is not an epoch integer",
                code="timestamp_out_of_window",
            ) from exc
        if abs(int(now.timestamp()) - ts) > self._settings.hmac_timestamp_tolerance_seconds:
            raise AuthenticationError(
                "signature timestamp outside the acceptance window",
                code="timestamp_out_of_window",
            )
        record = self._repo.get(key_id)
        if record is None or not self._usable(record, now):
            raise AuthenticationError("api key not recognized", code="api_key_invalid")
        hmac_key = credentials.decrypt_blob(self._settings.hmac_master_key, record.hmac_key_enc)
        expected = credentials.compute_request_signature(
            hmac_key, method=method, path=path, timestamp=timestamp, body=body
        )
        if not credentials.constant_time_equals(expected, signature):
            raise AuthenticationError("request signature mismatch", code="invalid_signature")
        marker = credentials.sha256_hex(f"{key_id}:{timestamp}:{signature}")
        if not self._replay_guard.first_use(
            marker, now=now, ttl_seconds=self._settings.hmac_replay_ttl_seconds
        ):
            raise AuthenticationError("signature replay refused", code="replay_detected")
        self._enforce_ip(record, client_ip)
        return self._to_principal(record)

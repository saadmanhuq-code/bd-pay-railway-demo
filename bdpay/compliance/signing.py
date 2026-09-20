"""Ed25519 signing-key registry + keypair ceremony for rule packs (spec/06).

Adapted from the veridyn SigningKeyRegistry pattern (spec/06 Connector
contract, "RulePack signing"): the registry is backed by the
``signing_key_registry`` table (migration 0070) rather than a JSON file; the
private key NEVER enters the registry (OpenBao custody — only the public key
is stored, base64); every record carries a ``record_hash`` integrity check
recomputed on load; the demo signer is blocked unless
``BDPAY_ENABLE_DEMO_SIGNER=true`` (production deploys never set it).

Key revocation blocks NEW pack activations but does not invalidate
already-active packs (spec/06 adaptation rule 5).
"""

from __future__ import annotations

import base64
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Protocol, runtime_checkable

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from bdpay.platform.canonical import sha256_canonical

__all__ = [
    "DEMO_SIGNER_ID",
    "InMemorySigningKeyStore",
    "SigningKeyError",
    "SigningKeyRecord",
    "SigningKeyRegistry",
    "SigningKeyStore",
    "generate_signing_keypair",
    "sign_content_hash",
]

#: The veridyn demo signer, blocked in production (spec/06 adaptation rule 3).
DEMO_SIGNER_ID = "signer_demo_trusted"

_DEMO_FLAG = "BDPAY_ENABLE_DEMO_SIGNER"


class SigningKeyError(ValueError):
    """Raised on any signing-key registry refusal (fail-closed)."""


def _record_preimage(
    *,
    key_id: str,
    signer_id: str,
    algorithm: str,
    scope: str,
    status: str,
    public_key_b64: str | None,
    registry_version: int,
) -> dict:
    """The hashed registry-row content — timestamps excluded (veridyn rule)."""
    return {
        "key_id": key_id,
        "signer_id": signer_id,
        "algorithm": algorithm,
        "scope": scope,
        "status": status,
        "public_key_b64": public_key_b64,
        "registry_version": registry_version,
    }


@dataclass(frozen=True)
class SigningKeyRecord:
    """One signing_key_registry row (migration 0070)."""

    key_id: str  # signer_<name>_v<version>
    signer_id: str
    algorithm: str
    scope: str
    status: str  # active | revoked
    public_key_b64: str | None
    registry_version: int
    created_at: datetime
    record_hash: str
    revoked_at: datetime | None = None
    revocation_reason: str | None = None
    schema_version: int = 1

    def verify_integrity(self) -> None:
        """Recompute and check record_hash — refused on mismatch (tamper)."""
        expected = sha256_canonical(
            _record_preimage(
                key_id=self.key_id,
                signer_id=self.signer_id,
                algorithm=self.algorithm,
                scope=self.scope,
                status=self.status,
                public_key_b64=self.public_key_b64,
                registry_version=self.registry_version,
            )
        )
        if expected != self.record_hash:
            raise SigningKeyError(
                f"signing key record {self.key_id!r} failed its integrity check"
            )


@runtime_checkable
class SigningKeyStore(Protocol):
    """Storage contract for the signing key registry."""

    def insert(self, record: SigningKeyRecord) -> None: ...

    def save(self, record: SigningKeyRecord) -> None: ...

    def by_signer(self, signer_id: str) -> list[SigningKeyRecord]: ...

    def active_for_signer(self, signer_id: str) -> SigningKeyRecord | None: ...


class InMemorySigningKeyStore:
    """Deterministic in-memory registry store for unit tests."""

    def __init__(self) -> None:
        self._rows: dict[str, SigningKeyRecord] = {}

    def insert(self, record: SigningKeyRecord) -> None:
        if record.key_id in self._rows:
            raise SigningKeyError(f"key_id {record.key_id!r} already registered")
        self._rows[record.key_id] = record

    def save(self, record: SigningKeyRecord) -> None:
        if record.key_id not in self._rows:
            raise SigningKeyError(f"unknown key_id {record.key_id!r}")
        self._rows[record.key_id] = record

    def by_signer(self, signer_id: str) -> list[SigningKeyRecord]:
        return sorted(
            (r for r in self._rows.values() if r.signer_id == signer_id),
            key=lambda r: r.registry_version,
        )

    def active_for_signer(self, signer_id: str) -> SigningKeyRecord | None:
        for record in self._rows.values():
            if record.signer_id == signer_id and record.status == "active":
                return record
        return None


def generate_signing_keypair() -> tuple[str, str]:
    """Generate an Ed25519 keypair: ``(private_key_hex, public_key_b64)``.

    Retained verbatim from the veridyn ceremony: library-default entropy.
    The private hex goes to OpenBao custody; only the public half may ever
    reach the registry. This function never persists anything.
    """
    private_key = Ed25519PrivateKey.generate()
    private_hex = private_key.private_bytes_raw().hex()
    public_b64 = base64.b64encode(private_key.public_key().public_bytes_raw()).decode("ascii")
    return private_hex, public_b64


def sign_content_hash(private_key_hex: str, content_hash_hex: str) -> str:
    """Sign a sha256 content hash (its hex string, UTF-8) -> 128-char hex sig."""
    private_key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(private_key_hex))
    return private_key.sign(content_hash_hex.encode("utf-8")).hex()


class SigningKeyRegistry:
    """Registry façade: register, revoke, look up, and verify signatures."""

    def __init__(
        self,
        store: SigningKeyStore,
        *,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self._store = store
        self._env: Mapping[str, str] = env if env is not None else {}

    def _demo_signer_allowed(self) -> bool:
        return self._env.get(_DEMO_FLAG, "").strip().lower() == "true"

    def register_public_key(
        self,
        *,
        signer_id: str,
        public_key_b64: str,
        registry_version: int,
        now: datetime,
    ) -> SigningKeyRecord:
        """Register a public key for a signer; one active key per signer."""
        for name, value in (("signer_id", signer_id), ("public_key_b64", public_key_b64)):
            if not isinstance(value, str) or not value:
                raise SigningKeyError(f"{name} must be a non-empty string")
        try:
            decoded = base64.b64decode(public_key_b64, validate=True)
        except (ValueError, TypeError) as exc:
            raise SigningKeyError("public_key_b64 is not valid base64") from exc
        if len(decoded) != 32:
            raise SigningKeyError("an Ed25519 public key must be exactly 32 bytes")
        if self._store.active_for_signer(signer_id) is not None:
            raise SigningKeyError(
                f"signer {signer_id!r} already has an active key; revoke it first"
            )
        key_id = f"{signer_id}_v{registry_version}"
        record = SigningKeyRecord(
            key_id=key_id,
            signer_id=signer_id,
            algorithm="Ed25519",
            scope="pack_signing",
            status="active",
            public_key_b64=public_key_b64,
            registry_version=registry_version,
            created_at=now,
            record_hash=sha256_canonical(
                _record_preimage(
                    key_id=key_id,
                    signer_id=signer_id,
                    algorithm="Ed25519",
                    scope="pack_signing",
                    status="active",
                    public_key_b64=public_key_b64,
                    registry_version=registry_version,
                )
            ),
        )
        self._store.insert(record)
        return record

    def revoke(self, signer_id: str, *, reason: str, now: datetime) -> SigningKeyRecord:
        """Revoke the signer's active key (blocks new pack activations)."""
        if not reason:
            raise SigningKeyError("a revocation requires a reason")
        record = self._store.active_for_signer(signer_id)
        if record is None:
            raise SigningKeyError(f"signer {signer_id!r} has no active key to revoke")
        revoked = replace(
            record,
            status="revoked",
            revoked_at=now,
            revocation_reason=reason,
            record_hash=sha256_canonical(
                _record_preimage(
                    key_id=record.key_id,
                    signer_id=record.signer_id,
                    algorithm=record.algorithm,
                    scope=record.scope,
                    status="revoked",
                    public_key_b64=record.public_key_b64,
                    registry_version=record.registry_version,
                )
            ),
        )
        self._store.save(revoked)
        return revoked

    def active_public_key(self, signer_id: str) -> Ed25519PublicKey:
        """The signer's active, integrity-checked public key (fail-closed).

        Refuses for: unknown signer, revoked-only signer, missing key
        material, integrity mismatch, and the demo signer without the
        explicit ``BDPAY_ENABLE_DEMO_SIGNER=true`` opt-in.
        """
        if signer_id == DEMO_SIGNER_ID and not self._demo_signer_allowed():
            raise SigningKeyError(
                "the demo signer is blocked in production "
                "(BDPAY_ENABLE_DEMO_SIGNER is not set)"
            )
        record = self._store.active_for_signer(signer_id)
        if record is None:
            raise SigningKeyError(f"signer {signer_id!r} has no active registered key")
        record.verify_integrity()
        if not record.public_key_b64:
            raise SigningKeyError(f"signer {signer_id!r} has no registered public key")
        return Ed25519PublicKey.from_public_bytes(base64.b64decode(record.public_key_b64))

    def verify_signature(
        self, *, signer_id: str, content_hash_hex: str, signature_hex: str
    ) -> None:
        """Verify an Ed25519 signature over a content-hash hex string."""
        public_key = self.active_public_key(signer_id)
        try:
            signature = bytes.fromhex(signature_hex)
        except ValueError as exc:
            raise SigningKeyError("signature_hex is not valid hex") from exc
        try:
            public_key.verify(signature, content_hash_hex.encode("utf-8"))
        except InvalidSignature as exc:
            raise SigningKeyError("Ed25519 signature verification failed") from exc

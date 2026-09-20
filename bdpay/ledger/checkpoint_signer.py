"""Ed25519 checkpoint signing (spec/03 source-adaptation notes).

Ported from bd-pay-fable-pilot/src/bdpay_pilot/platform/checkpoint_signer.py
(verified pilot) into bdpay/ledger per the build plan.

The production design signs via an external transit engine; this in-process
``LocalEd25519Signer`` exposes the same sign/verify contract with keys
supplied externally (raw 32-byte seed, or ``os.urandom(32)`` when none is
injected). The key is NEVER derived from any public identifier. Checkpoint
trust comes from an externally-supplied trusted-key set (E6): a public key
stored in the row proves nothing on its own.

``verify_checkpoint`` needs no key service (archived public key) and is
implemented exactly as specified — fails closed on ANY error.
"""

from __future__ import annotations

import base64
import os
from typing import Protocol, runtime_checkable

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

__all__ = [
    "CheckpointSigner",
    "LocalEd25519Signer",
    "generate_signing_seed",
    "verify_checkpoint",
]


def generate_signing_seed() -> bytes:
    """A fresh 32-byte Ed25519 seed from the OS CSPRNG.

    The ONLY key-material source in the ledger domain (tests/ledger
    ``test_os_urandom_only_in_signer`` pins that): durable checkpoint
    custody (:mod:`bdpay.ledger.trust_registry`) mints its keys through
    here rather than reaching for OS randomness of its own.
    """
    return os.urandom(32)


@runtime_checkable
class CheckpointSigner(Protocol):
    """sign(chain_hash) -> (signature_hex, public_key_b64)."""

    def sign(self, chain_hash: str) -> tuple[str, str]: ...

    def public_key_b64(self) -> str: ...


class LocalEd25519Signer:
    """In-process Ed25519 signer; key material supplied externally or via os.urandom."""

    def __init__(self, private_key_bytes: bytes | None = None) -> None:
        if private_key_bytes is None:
            private_key_bytes = os.urandom(32)
        if len(private_key_bytes) != 32:
            raise ValueError("Ed25519 private key seed must be exactly 32 bytes")
        self._private_key = Ed25519PrivateKey.from_private_bytes(private_key_bytes)
        raw_pub = self._private_key.public_key().public_bytes_raw()
        self._public_key_b64 = base64.b64encode(raw_pub).decode("ascii")

    def sign(self, chain_hash: str) -> tuple[str, str]:
        """Ed25519 signature over the UTF-8 bytes of chain_hash (hex, 128 chars)."""
        signature = self._private_key.sign(chain_hash.encode("utf-8"))
        return signature.hex(), self._public_key_b64

    def public_key_b64(self) -> str:
        return self._public_key_b64


def verify_checkpoint(chain_hash: str, signature_hex: str, public_key_b64: str) -> bool:
    """Verify an Ed25519 checkpoint signature. Fail closed: False on ANY error."""
    try:
        pub_key_bytes = base64.b64decode(public_key_b64, validate=True)
        pub_key = Ed25519PublicKey.from_public_bytes(pub_key_bytes)
        sig_bytes = bytes.fromhex(signature_hex)
        pub_key.verify(sig_bytes, chain_hash.encode("utf-8"))
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False

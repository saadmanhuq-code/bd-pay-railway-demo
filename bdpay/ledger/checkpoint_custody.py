"""Checkpoint-signing custody adapter (additive; E6 verification unchanged).

The ledger checkpoint SIGNING key may live in a key-custody provider (the
YubiHSM 2 secure element — ``bdpay.connectors.hsm.yubi`` — soft mode until
hardware activation) instead of process memory. VERIFICATION does not move:
``verify_checkpoint`` and the LedgerService externally-supplied trusted-key
set (SPEC_ERRATA E6) are untouched — a custody-held key still proves nothing
unless its public key is in the trusted set.

Zero behavior change when unconfigured: nothing in the ledger package imports
this module (test-pinned); LedgerService keeps its default
``LocalEd25519Signer`` path unless an operator explicitly constructs it with a
``CustodyCheckpointSigner``. The adapter depends only on the narrow
``CustodySigningPort`` Protocol, so the ledger package never imports the
connectors package.
"""

from __future__ import annotations

import base64
from typing import Protocol, runtime_checkable

__all__ = ["CustodyCheckpointSigner", "CustodySigningPort"]


@runtime_checkable
class CustodySigningPort(Protocol):
    """Sync signing surface a custody provider must expose (mode-gated, audited)."""

    def sign_ed25519_sync(self, key_label: str, message: bytes) -> bytes: ...

    def public_key_ed25519_sync(self, key_label: str) -> bytes: ...


class CustodyCheckpointSigner:
    """``CheckpointSigner`` backed by a key-custody provider.

    Drop-in for ``LocalEd25519Signer``: same ``sign(chain_hash) ->
    (signature_hex, public_key_b64)`` contract over the UTF-8 bytes of the
    chain hash, so signatures verify with the unchanged ``verify_checkpoint``
    against the E6 trusted-key set.
    """

    def __init__(self, custody: CustodySigningPort, key_label: str) -> None:
        self._custody = custody
        self._key_label = key_label
        raw_public = custody.public_key_ed25519_sync(key_label)
        if len(raw_public) != 32:
            raise ValueError("custody provider returned a non-Ed25519 public key")
        self._public_key_b64 = base64.b64encode(raw_public).decode("ascii")

    def sign(self, chain_hash: str) -> tuple[str, str]:
        signature = self._custody.sign_ed25519_sync(
            self._key_label, chain_hash.encode("utf-8")
        )
        return signature.hex(), self._public_key_b64

    def public_key_b64(self) -> str:
        return self._public_key_b64

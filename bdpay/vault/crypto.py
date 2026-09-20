"""Vault cryptography — AES-256-GCM envelope encryption under a soft-HSM key source.

Hierarchy (spec/14 Flow 3), soft-HSM realization until hardware lands:

- master key (LMK analog) — held only by :class:`SoftHsm` process memory;
  wraps KEK material (``softlmk:v1:<b64(nonce+ct)>``).
- KEK — wraps DEK/PEPPER material; wrapped blobs stored in ``vault_keys`` as
  ``vault:v<kek_generation>:<b64(nonce+ct)>`` (OpenBao-transit-shaped string).
- DEK — AES-256-GCM field encryption of PAN/holder-name/CVV-in-memory.
- PEPPER — HMAC-SHA256 token-derivation key (keyed digest; an unkeyed
  ``sha256(PAN)`` is normatively forbidden by spec/14 §Entities).

Crypto *nonces* are exempt from the determinism rule (spec/14 §Entities) and
MUST come from the HSM RNG or ``os.urandom`` — a deterministic nonce under
AES-GCM is a catastrophic break. ``SoftHsm.random`` is the single RNG used.

Fail-closed: when the key source is marked unavailable every operation raises
:class:`KeySourceUnavailableError`; callers map it to
``500 internal / vault_crypto_unavailable`` (spec/14 F1/F2).

Production key-root gate (PCI DSS Req 3 — strong cryptography / key management):
the soft-HSM is an in-process key root acceptable for development, simulator
certification, and tests only. A production deployment MUST root key material in
hardware (the spec/12 ``hsm_thales_v1`` / YubiHSM connector). When a
:class:`SoftHsm` is constructed with ``production=True`` it refuses to serve any
crypto material — every operation raises
:class:`SoftHsmForbiddenInProductionError`. Because all card/PAN key derivation
(DEK unwrap, PEPPER, nonces, KEK wrap/unwrap) routes through this object — the
``kek_wrap``/``kek_unwrap`` KEK-domain operations are instance methods guarded by
the same production gate — the refusal fail-closes the entire card-processing
path at the crypto boundary rather than letting PAN ciphertext be served under a
software-only key root in production.
"""

from __future__ import annotations

import base64
import hashlib
import hmac as hmac_mod
import os

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from bdpay.platform.canonical import sha256_canonical

__all__ = [
    "GCM_NONCE_BYTES",
    "KEY_BYTES",
    "KeySourceUnavailableError",
    "SoftHsm",
    "SoftHsmForbiddenInProductionError",
    "aes_gcm_decrypt",
    "aes_gcm_encrypt",
    "hmac_sha256_hex",
    "key_check_value",
    "zeroize",
]

KEY_BYTES = 32  # AES-256
GCM_NONCE_BYTES = 12


class KeySourceUnavailableError(RuntimeError):
    """The key source cannot serve crypto material (fail-closed trigger)."""


class SoftHsmForbiddenInProductionError(KeySourceUnavailableError):
    """A soft-HSM key root was used in a production deployment (PCI Req 3).

    A subclass of :class:`KeySourceUnavailableError` so existing fail-closed
    callers (mapping to ``500 / vault_crypto_unavailable``) already refuse the
    card-processing operation; the distinct type names the production-gate
    cause for ops/audit without changing the caller contract.
    """


def zeroize(buffer: bytearray) -> None:
    """Best-effort in-place zero of sensitive bytes (spec/14 SAD/PAN handling)."""
    for index in range(len(buffer)):
        buffer[index] = 0


def key_check_value(key: bytes) -> str:
    """6-hex-char KCV: AES-ECB of one zero block, first 3 bytes (evidence only)."""
    encryptor = Cipher(algorithms.AES(key), modes.ECB()).encryptor()  # noqa: S305 - KCV only
    block = encryptor.update(b"\x00" * 16) + encryptor.finalize()
    return block[:3].hex()


def hmac_sha256_hex(key: bytes, message: bytes) -> str:
    """Keyed digest used for PAN -> token derivation (PEPPER) and grants."""
    return hmac_mod.new(key, message, hashlib.sha256).hexdigest()


def aes_gcm_encrypt(key: bytes, plaintext: bytes, aad: bytes, nonce: bytes) -> bytes:
    """AES-256-GCM encrypt; ciphertext has the 16-byte tag appended (library form)."""
    if len(nonce) != GCM_NONCE_BYTES:
        raise ValueError(f"GCM nonce must be {GCM_NONCE_BYTES} bytes")
    return AESGCM(key).encrypt(nonce, plaintext, aad)


def aes_gcm_decrypt(key: bytes, ciphertext: bytes, aad: bytes, nonce: bytes) -> bytes:
    """AES-256-GCM decrypt; raises ``cryptography.exceptions.InvalidTag`` on
    any tampering of ciphertext, nonce, or AAD (the AAD binds token identity)."""
    return AESGCM(key).decrypt(nonce, ciphertext, aad)


class SoftHsm:
    """Soft-HSM key source — the operations the vault requires of spec/12's
    ``hsm_thales_v1`` (random / generate / wrap / unwrap / kcv / delete /
    health), fully implemented in-process until hardware procurement lands.

    The master key never leaves this object (LMK analog). ``set_available``
    drives deterministic failure-mode tests (F1/F2 fail-closed posture).
    """

    _LMK_PREFIX = "softlmk:v1:"

    def __init__(self, master_key: bytes | None = None, *, production: bool = False) -> None:
        if master_key is not None and len(master_key) != KEY_BYTES:
            raise ValueError(f"master key must be {KEY_BYTES} bytes")
        self._master = bytearray(master_key if master_key is not None else os.urandom(KEY_BYTES))
        self._available = True
        #: A soft-HSM is never an acceptable key root in production (PCI Req 3).
        #: When True every operation fails closed before touching key material.
        self._production = bool(production)

    # -- availability ---------------------------------------------------------

    def set_available(self, available: bool) -> None:
        self._available = bool(available)

    def health_check(self) -> bool:
        return self._available and not self._production

    def _guard(self) -> None:
        # Production gate first: a software-only key root must NEVER serve card
        # crypto in production — refuse at the boundary before any operation.
        if self._production:
            raise SoftHsmForbiddenInProductionError(
                "soft-HSM key root is forbidden in production; "
                "card processing requires a hardware HSM (fail closed)"
            )
        if not self._available:
            raise KeySourceUnavailableError("soft-HSM marked unavailable (fail closed)")

    # -- RNG -------------------------------------------------------------------

    def random(self, n_bytes: int) -> bytes:
        """The single nonce/key RNG (spec/14: HSM RNG or os.urandom only)."""
        self._guard()
        if n_bytes <= 0:
            raise ValueError("n_bytes must be positive")
        return os.urandom(n_bytes)

    # -- LMK-domain wrap (KEK material) -----------------------------------------

    def generate_key_under_lmk(self) -> dict:
        """New AES-256 key wrapped under the master key. Returns wrapped + kcv."""
        self._guard()
        material = bytearray(self.random(KEY_BYTES))
        try:
            return {
                "wrapped": self.wrap_under_lmk(bytes(material)),
                "kcv": key_check_value(bytes(material)),
            }
        finally:
            zeroize(material)

    def wrap_under_lmk(self, key_material: bytes) -> str:
        self._guard()
        nonce = self.random(GCM_NONCE_BYTES)
        ciphertext = aes_gcm_encrypt(bytes(self._master), key_material, b"softlmk", nonce)
        return self._LMK_PREFIX + base64.b64encode(nonce + ciphertext).decode("ascii")

    def unwrap_under_lmk(self, wrapped: str) -> bytes:
        self._guard()
        if not wrapped.startswith(self._LMK_PREFIX):
            raise ValueError("not an LMK-wrapped blob")
        blob = base64.b64decode(wrapped[len(self._LMK_PREFIX) :])
        nonce, ciphertext = blob[:GCM_NONCE_BYTES], blob[GCM_NONCE_BYTES:]
        return aes_gcm_decrypt(bytes(self._master), ciphertext, b"softlmk", nonce)

    def key_check_value_of(self, wrapped: str) -> str:
        material = bytearray(self.unwrap_under_lmk(wrapped))
        try:
            return key_check_value(bytes(material))
        finally:
            zeroize(material)

    def delete_key(self, wrapped_ref: str) -> dict:
        """Destroy receipt for a wrapped blob (soft mode: the receipt is the
        evidence record; the caller drops every copy of the blob)."""
        self._guard()
        return {"deleted_ref_hash": sha256_canonical({"wrapped": wrapped_ref}), "kind": "softhsm"}

    # -- KEK-domain wrap (DEK / PEPPER material, transit-shaped strings) --------

    def kek_wrap(self, kek: bytes, key_material: bytes, kek_generation: int) -> str:
        # Production gate (PCI Req 3): a software-only key root must NEVER wrap
        # card-key material in production — refuse before touching key bytes.
        self._guard()
        nonce = os.urandom(GCM_NONCE_BYTES)
        aad = f"kekv{kek_generation}".encode("ascii")
        ciphertext = aes_gcm_encrypt(kek, key_material, aad, nonce)
        return f"vault:v{kek_generation}:" + base64.b64encode(nonce + ciphertext).decode("ascii")

    def kek_unwrap(self, kek: bytes, wrapped: str) -> bytes:
        # Production gate (PCI Req 3): refuse to unwrap card-key material under a
        # soft-HSM root in production before any crypto runs (fail closed).
        self._guard()
        prefix, _, body = wrapped.partition(":")
        version, _, payload = body.partition(":")
        if prefix != "vault" or not version.startswith("v"):
            raise ValueError("not a KEK-wrapped transit blob")
        generation = int(version[1:])
        blob = base64.b64decode(payload)
        nonce, ciphertext = blob[:GCM_NONCE_BYTES], blob[GCM_NONCE_BYTES:]
        aad = f"kekv{generation}".encode("ascii")
        return aes_gcm_decrypt(kek, ciphertext, aad, nonce)

    @staticmethod
    def wrapped_generation(wrapped: str) -> int:
        """The KEK generation embedded in a ``vault:vN:...`` blob."""
        _, _, body = wrapped.partition(":")
        version, _, _ = body.partition(":")
        return int(version[1:])

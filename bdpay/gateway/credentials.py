"""Credential key material — generation, hashing-at-rest, HMAC derivation.

- API-key secrets: ``bdpk_live_``/``bdpk_test_`` + 64 hex chars of OS entropy
  (spec/01 §B). At rest only ``bcrypt(sha256(secret))`` is stored — the
  sha256 pre-hash keeps the input under bcrypt's 72-byte limit while the full
  secret stays significant (spec/01's 74-char secret would otherwise be
  silently truncated).
- HMAC request-signing key: HKDF-SHA256 derived from the raw secret at
  creation time (spec/01 §A HMAC model, CORRECTION block), stored AES-256-GCM
  encrypted under the gateway master key (the OpenBao transit role in the
  deployed topology).
- All comparisons are constant-time (``hmac.compare_digest`` / bcrypt).
"""

from __future__ import annotations

import hashlib
import hmac
import os

import bcrypt
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

__all__ = [
    "KEY_PREFIX_LIVE",
    "KEY_PREFIX_TEST",
    "constant_time_equals",
    "compute_request_signature",
    "decrypt_blob",
    "derive_hmac_key",
    "encrypt_blob",
    "generate_api_key_secret",
    "hash_secret",
    "sha256_hex",
    "verify_secret",
]

KEY_PREFIX_LIVE = "bdpk_live_"
KEY_PREFIX_TEST = "bdpk_test_"

_HKDF_SALT = b"bdpay-hmac-v1"
_NONCE_BYTES = 12


def sha256_hex(data: bytes | str) -> str:
    raw = data.encode("utf-8") if isinstance(data, str) else data
    return hashlib.sha256(raw).hexdigest()


def constant_time_equals(left: str | bytes, right: str | bytes) -> bool:
    lb = left.encode("utf-8") if isinstance(left, str) else left
    rb = right.encode("utf-8") if isinstance(right, str) else right
    return hmac.compare_digest(lb, rb)


def generate_api_key_secret(prefix: str) -> str:
    """``prefix + hex(os.urandom(32))`` per spec/01 §B (shown exactly once)."""
    if prefix not in (KEY_PREFIX_LIVE, KEY_PREFIX_TEST):
        raise ValueError(f"unknown api key prefix {prefix!r}")
    return prefix + os.urandom(32).hex()


def _prehash(secret: str) -> bytes:
    # sha256 pre-hash: bcrypt ignores input beyond 72 bytes; spec/01 secrets
    # are 74 chars, so hashing the digest keeps every character significant.
    return hashlib.sha256(secret.encode("utf-8")).hexdigest().encode("ascii")


def hash_secret(secret: str, *, rounds: int = 12) -> str:
    """bcrypt hash for storage (spec/01: raw secret never stored)."""
    if not 4 <= rounds <= 31:
        raise ValueError("bcrypt rounds must be between 4 and 31")
    return bcrypt.hashpw(_prehash(secret), bcrypt.gensalt(rounds=rounds)).decode("ascii")


def verify_secret(secret: str, secret_hash: str) -> bool:
    """Constant-time bcrypt check of a presented secret."""
    try:
        return bcrypt.checkpw(_prehash(secret), secret_hash.encode("ascii"))
    except ValueError:
        return False


def derive_hmac_key(raw_secret: str, key_id: str) -> bytes:
    """HKDF-SHA256(ikm=raw_secret, salt="bdpay-hmac-v1", info=key_id, L=32)."""
    if not raw_secret or not key_id:
        raise ValueError("raw_secret and key_id must be non-empty")
    prk = hmac.new(_HKDF_SALT, raw_secret.encode("utf-8"), hashlib.sha256).digest()
    okm = hmac.new(prk, key_id.encode("utf-8") + b"\x01", hashlib.sha256).digest()
    return okm[:32]


def encrypt_blob(master_key: bytes, plaintext: bytes) -> str:
    """AES-256-GCM encrypt; returns hex(nonce || ciphertext+tag)."""
    if len(master_key) != 32:
        raise ValueError("master key must be 32 bytes")
    nonce = os.urandom(_NONCE_BYTES)
    sealed = AESGCM(master_key).encrypt(nonce, plaintext, None)
    return (nonce + sealed).hex()


def decrypt_blob(master_key: bytes, blob_hex: str) -> bytes:
    """AES-256-GCM decrypt of :func:`encrypt_blob` output; fails closed."""
    if len(master_key) != 32:
        raise ValueError("master key must be 32 bytes")
    try:
        raw = bytes.fromhex(blob_hex)
        return AESGCM(master_key).decrypt(raw[:_NONCE_BYTES], raw[_NONCE_BYTES:], None)
    except (ValueError, InvalidTag) as exc:
        raise ValueError("encrypted blob failed authentication") from exc


def compute_request_signature(
    hmac_key: bytes, *, method: str, path: str, timestamp: str, body: bytes
) -> str:
    """spec/01 §A preimage: ``method \\n path \\n timestamp \\n sha256(body)``."""
    preimage = "\n".join((method.upper(), path, timestamp, sha256_hex(body)))
    return hmac.new(hmac_key, preimage.encode("utf-8"), hashlib.sha256).hexdigest()

"""In-memory SAD (CVV) store — spec/14 intake step 6 and the normative
"SAD store (non-table)" section.

CVV/CVC lives exclusively in process memory keyed by ``vint_id``, encrypted
under the active DEK, TTL = min(session remaining TTL, 30 minutes). Entries
are zeroized and dropped on: first successful detokenize-egress consume, TTL
expiry, session void/expiry, or process shutdown. There is deliberately no
persistence path of any kind (PCI Req 3.3.1 / 3.3.2) — a migration adding a
CVV column anywhere is a build-breaking violation.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta

from bdpay.platform.clock import Clock
from bdpay.vault.crypto import GCM_NONCE_BYTES, aes_gcm_decrypt, aes_gcm_encrypt, zeroize

__all__ = ["SadStore"]

MAX_TTL = timedelta(minutes=30)
_AAD = b"vault-sad-v1"


class _SadEntry:
    __slots__ = ("ciphertext", "dek_id", "expires_at", "nonce")

    def __init__(
        self, ciphertext: bytearray, nonce: bytes, dek_id: str, expires_at: datetime
    ) -> None:
        self.ciphertext = ciphertext
        self.nonce = nonce
        self.dek_id = dek_id
        self.expires_at = expires_at


class SadStore:
    """DEK-encrypted, TTL-bound, memory-only CVV map ``{vint_id -> entry}``.

    ``dek_provider`` returns ``(dek_material, dek_key_id)`` for the active
    DEK; ``rng`` supplies GCM nonces (HSM RNG / os.urandom only).
    """

    def __init__(
        self,
        clock: Clock,
        *,
        dek_provider: Callable[[], tuple[bytes, str]],
        rng: Callable[[int], bytes],
    ) -> None:
        self._clock = clock
        self._dek_provider = dek_provider
        self._rng = rng
        self._entries: dict[str, _SadEntry] = {}

    def __len__(self) -> int:
        return len(self._entries)

    def put(self, vint_id: str, cvv: str, *, session_expires_at: datetime) -> None:
        now = self._clock.now()
        expires_at = min(session_expires_at, now + MAX_TTL)
        dek, dek_id = self._dek_provider()
        nonce = self._rng(GCM_NONCE_BYTES)
        plaintext = bytearray(cvv.encode("ascii"))
        try:
            ciphertext = bytearray(aes_gcm_encrypt(dek, bytes(plaintext), _AAD, nonce))
        finally:
            zeroize(plaintext)
        self.purge(vint_id)  # single live entry per session
        self._entries[vint_id] = _SadEntry(ciphertext, nonce, dek_id, expires_at)

    def alive(self, vint_id: str) -> bool:
        entry = self._entries.get(vint_id)
        if entry is None:
            return False
        if self._clock.now() >= entry.expires_at:
            self.purge(vint_id)
            return False
        return True

    def consume(self, vint_id: str) -> str | None:
        """Decrypt, zeroize, and remove the entry (single use). None if absent,
        past TTL, or sealed under a DEK that has since rotated (fail-closed
        miss; SAD is never re-encrypted) — the caller maps that to
        ``422 / cvv_not_available``."""
        if not self.alive(vint_id):
            return None
        entry = self._entries[vint_id]
        dek, dek_id = self._dek_provider()
        if dek_id != entry.dek_id:
            self.purge(vint_id)
            return None
        plaintext = bytearray(aes_gcm_decrypt(dek, bytes(entry.ciphertext), _AAD, entry.nonce))
        try:
            value = plaintext.decode("ascii")
        finally:
            zeroize(plaintext)
        self.purge(vint_id)
        return value

    def purge(self, vint_id: str) -> None:
        entry = self._entries.pop(vint_id, None)
        if entry is not None:
            zeroize(entry.ciphertext)

    def purge_expired(self) -> int:
        now = self._clock.now()
        expired = [key for key, entry in self._entries.items() if now >= entry.expires_at]
        for key in expired:
            self.purge(key)
        return len(expired)

    def shutdown(self) -> None:
        """Process-exit hook: zeroize and drop every entry."""
        for key in list(self._entries):
            self.purge(key)

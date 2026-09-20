"""Customer OTP sessions — the 2FA gate input for high-value confirms.

spec/01 §C: a customer-confirmed payment of >= BDT 100.00 requires an OTP
verified within the last 300 seconds. The OTP *delivery* is the spec/12
connector's job; the gateway consumes the resulting short-lived OTP session
token through this port. Tokens are stored as SHA-256 digests with their
issue instant; verification binds the token to the customer and the TTL.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import Protocol, runtime_checkable

from bdpay.gateway.credentials import constant_time_equals, sha256_hex

__all__ = ["InMemoryOtpSessionStore", "OtpSessionStore"]


@runtime_checkable
class OtpSessionStore(Protocol):
    """Issue and verify short-lived customer OTP session tokens."""

    def issue(self, customer_id: str, *, now: datetime) -> str: ...

    def verify(self, token: str, *, customer_id: str, now: datetime) -> bool: ...


class InMemoryOtpSessionStore:
    """Deterministic in-memory OTP session store."""

    def __init__(self, *, ttl_seconds: int = 300) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self._ttl = timedelta(seconds=ttl_seconds)
        self._rows: dict[str, tuple[str, datetime]] = {}

    def issue(self, customer_id: str, *, now: datetime) -> str:
        if not customer_id:
            raise ValueError("customer_id must be non-empty")
        token = os.urandom(16).hex()
        self._rows[sha256_hex(token)] = (customer_id, now)
        return token

    def verify(self, token: str, *, customer_id: str, now: datetime) -> bool:
        digest = sha256_hex(token)
        row = self._rows.get(digest)
        if row is None:
            return False
        owner, issued_at = row
        if not constant_time_equals(owner, customer_id):
            return False
        self._rows.pop(digest, None)
        return now - issued_at <= self._ttl

"""TOTP per RFC 6238 (HOTP per RFC 4226) — stdlib HMAC implementation.

Used for the operator 2FA step (BB ICT §9.2). No third-party OTP dependency:
the algorithm is HMAC-SHA1 over the big-endian time-step counter with dynamic
truncation, verified with a constant-time comparison across a +/- ``window``
step skew allowance (spec/01 failure modes: TOTP clock skew, +/- 1 step).

All time arrives as a timezone-aware ``datetime`` from the injected platform
clock — this module never reads a wall clock.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import struct
from datetime import datetime

__all__ = [
    "generate_totp_secret",
    "hotp",
    "provisioning_uri",
    "totp_at",
    "verify_totp",
]


def generate_totp_secret(*, num_bytes: int = 20) -> str:
    """A fresh base32 TOTP shared secret (RFC 4226 recommends 160 bits)."""
    if num_bytes < 16:
        raise ValueError("TOTP secrets must be at least 128 bits")
    return base64.b32encode(os.urandom(num_bytes)).decode("ascii")


def _decode_secret(secret: str) -> bytes:
    normalized = secret.strip().replace(" ", "").upper()
    padding = (-len(normalized)) % 8
    try:
        return base64.b32decode(normalized + "=" * padding)
    except (ValueError, TypeError) as exc:
        raise ValueError("TOTP secret is not valid base32") from exc


def hotp(secret: str, counter: int, *, digits: int = 6) -> str:
    """RFC 4226 HOTP value for ``counter`` (zero-padded to ``digits``)."""
    if counter < 0:
        raise ValueError("counter must be >= 0")
    if not 6 <= digits <= 8:
        raise ValueError("digits must be between 6 and 8")
    key = _decode_secret(secret)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (
        ((digest[offset] & 0x7F) << 24)
        | (digest[offset + 1] << 16)
        | (digest[offset + 2] << 8)
        | digest[offset + 3]
    )
    return str(code % (10**digits)).zfill(digits)


def _time_step(at: datetime, step_seconds: int) -> int:
    if at.tzinfo is None or at.tzinfo.utcoffset(at) is None:
        raise ValueError("TOTP time must be timezone-aware (injected clock)")
    if step_seconds <= 0:
        raise ValueError("step_seconds must be positive")
    return int(at.timestamp()) // step_seconds


def totp_at(secret: str, at: datetime, *, step_seconds: int = 30, digits: int = 6) -> str:
    """RFC 6238 TOTP value at instant ``at``."""
    return hotp(secret, _time_step(at, step_seconds), digits=digits)


def verify_totp(
    secret: str,
    code: str,
    *,
    at: datetime,
    step_seconds: int = 30,
    digits: int = 6,
    window_steps: int = 1,
) -> bool:
    """Constant-time TOTP check across ``window_steps`` of clock skew.

    Every candidate step in the window is evaluated (no early exit on match)
    so verification time does not reveal which step matched.
    """
    if not isinstance(code, str):
        return False
    if window_steps < 0:
        raise ValueError("window_steps must be >= 0")
    current = _time_step(at, step_seconds)
    matched = False
    for skew in range(-window_steps, window_steps + 1):
        step = current + skew
        if step < 0:
            continue
        candidate = hotp(secret, step, digits=digits)
        if hmac.compare_digest(candidate.encode("ascii"), code.encode("utf-8", "replace")):
            matched = True
    return matched


def provisioning_uri(account: str, secret: str, *, issuer: str = "BDPay") -> str:
    """``otpauth://`` enrollment URI (spec/01 §A TOTP setup response shape)."""
    return f"otpauth://totp/{issuer}:{account}?secret={secret}&issuer={issuer}"

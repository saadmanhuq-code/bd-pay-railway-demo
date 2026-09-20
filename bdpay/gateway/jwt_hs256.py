"""Customer JWT — HS256, stdlib implementation (spec/01 §B verification).

Why no JWT library: the gateway needs exactly one algorithm (HS256, the
spec/01 minimum), strict claim validation against the injected clock, and a
hard refusal of algorithm-substitution (``none``/``RS256`` headers are
rejected before any signature work). A 60-line explicit implementation is
easier to audit than a configurable dependency.

Verification accepts the current signing secret and, when supplied, the
previous one (spec/01 §B step 2: rotation grace). Signatures are compared
with ``hmac.compare_digest``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Mapping, Sequence
from datetime import datetime

__all__ = ["JwtError", "REQUIRED_CLAIMS", "encode_jwt", "decode_jwt"]

#: Claims every customer token must carry (spec/01 §A token shape).
REQUIRED_CLAIMS = ("sub", "iat", "exp", "jti", "scope")

_HEADER = {"alg": "HS256", "typ": "JWT"}


class JwtError(ValueError):
    """Raised on any token defect; ``reason`` is a stable snake_case code."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(segment: str) -> bytes:
    padding = (-len(segment)) % 4
    try:
        return base64.urlsafe_b64decode(segment + "=" * padding)
    except (ValueError, TypeError) as exc:
        raise JwtError("token_malformed") from exc


def _sign(signing_input: bytes, secret: str) -> bytes:
    return hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()


def encode_jwt(claims: Mapping[str, object], secret: str) -> str:
    """Serialize and sign ``claims`` as an HS256 JWT."""
    if not secret:
        raise ValueError("secret must be non-empty")
    header_b64 = _b64url_encode(
        json.dumps(_HEADER, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    payload_b64 = _b64url_encode(
        json.dumps(dict(claims), separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
    signature_b64 = _b64url_encode(_sign(signing_input, secret))
    return f"{header_b64}.{payload_b64}.{signature_b64}"


def decode_jwt(
    token: str,
    secrets: Sequence[str],
    *,
    now: datetime,
    required_claims: Sequence[str] = REQUIRED_CLAIMS,
) -> dict[str, object]:
    """Verify and parse an HS256 JWT; raises :class:`JwtError` on any defect.

    ``secrets`` is the ordered acceptance set (current key first, previous
    key during rotation grace). ``now`` comes from the injected clock.
    """
    if now.tzinfo is None or now.tzinfo.utcoffset(now) is None:
        raise ValueError("now must be timezone-aware (injected clock)")
    if not isinstance(token, str) or token.count(".") != 2:
        raise JwtError("token_malformed")
    header_b64, payload_b64, signature_b64 = token.split(".")

    try:
        header = json.loads(_b64url_decode(header_b64))
    except json.JSONDecodeError as exc:
        raise JwtError("token_malformed") from exc
    if not isinstance(header, dict) or header.get("alg") != "HS256":
        raise JwtError("algorithm_not_allowed")

    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
    provided = _b64url_decode(signature_b64)
    valid = False
    for secret in secrets:
        if secret and hmac.compare_digest(_sign(signing_input, secret), provided):
            valid = True
    if not valid:
        raise JwtError("signature_invalid")

    try:
        claims = json.loads(_b64url_decode(payload_b64))
    except json.JSONDecodeError as exc:
        raise JwtError("token_malformed") from exc
    if not isinstance(claims, dict):
        raise JwtError("token_malformed")

    for name in required_claims:
        if name not in claims:
            raise JwtError("claim_missing")
    for name in ("iat", "exp"):
        if name in claims and (
            isinstance(claims[name], bool) or not isinstance(claims[name], int)
        ):
            raise JwtError("claim_invalid")
    if "scope" in claims and not (
        isinstance(claims["scope"], list)
        and all(isinstance(item, str) for item in claims["scope"])
    ):
        raise JwtError("claim_invalid")
    if "exp" in claims and int(claims["exp"]) <= int(now.timestamp()):
        raise JwtError("token_expired")
    return claims

"""Shared-secret channel auth for the vault planes (spec/14 Plane B/C access).

spec/14 binds Plane B/C access to an authenticated caller identity checked
against ``vault_api_allowlist`` (identity -> permitted operations, default
deny). TLS/mTLS termination is deployment infrastructure; in-process the
channel is authenticated with a per-caller shared secret (errata LB7): every
request carries the caller identity (the same SPIFFE-style URI the mTLS SAN
would carry), a timestamp, and an HMAC-SHA256 signature over
``canonical_json({body_sha256, caller, method, path, ts})`` under that
caller's secret. Verification is fail-closed: missing/unknown caller, bad
signature, or stale timestamp all deny.

The allowlist itself stays exactly per spec: default-deny operation sets,
mutations require a four-eyes ApprovalRequest reference and are audited.
"""

from __future__ import annotations

import hashlib
import hmac as hmac_mod
from datetime import UTC, datetime, timedelta

from bdpay.platform.canonical import canonical_json
from bdpay.platform.clock import Clock
from bdpay.platform.errors import AuthenticationError, AuthorizationError

__all__ = [
    "CALLER_HEADER",
    "DEFAULT_ALLOWLIST_OPERATIONS",
    "SIGNATURE_HEADER",
    "TIMESTAMP_HEADER",
    "SharedSecretChannelAuth",
    "authorization_denied",
    "operation_permitted",
    "sign_channel_request",
]

CALLER_HEADER = "X-Vault-Caller"
TIMESTAMP_HEADER = "X-Vault-Timestamp"
SIGNATURE_HEADER = "X-Vault-Signature"
MAX_SKEW = timedelta(seconds=300)

#: spec/14 Plane B table (identity -> operations), plus the import operation
#: for the gateway (the migration/import endpoint names the gateway as its
#: caller) and the maintenance plane for the scheduler.
DEFAULT_ALLOWLIST_OPERATIONS: dict[str, tuple[str, ...]] = {
    "spiffe://bdpay/gateway": (
        "intake_session.create",
        "token.read_metadata",
        "token.retire",
        "token.import",
    ),
    "spiffe://bdpay/connector-runner/card-vault-proxy": (
        "detokenize.egress",
        "token.read_metadata",
    ),
    "spiffe://bdpay/ops-console-bff": (
        "key.read_metadata",
        "key.ceremony.*",
        "key.rotate",
        "key.compromise",
        "allowlist.read",
    ),
    "spiffe://bdpay/platform-scheduler": ("maintenance.sweep",),
}


def _preimage(caller: str, method: str, path: str, body: bytes, ts: str) -> bytes:
    return canonical_json(
        {
            "body_sha256": hashlib.sha256(body).hexdigest(),
            "caller": caller,
            "method": method.upper(),
            "path": path,
            "ts": ts,
        }
    )


def sign_channel_request(
    secret: str, *, caller: str, method: str, path: str, body: bytes, ts: str
) -> str:
    """Hex HMAC-SHA256 the client sends in ``X-Vault-Signature`` (used by the
    vault-proxy client and by every legitimate Plane-B caller)."""
    return hmac_mod.new(
        secret.encode("utf-8"), _preimage(caller, method, path, body, ts), hashlib.sha256
    ).hexdigest()


def operation_permitted(operations: tuple[str, ...], operation: str) -> bool:
    for granted in operations:
        if granted == operation:
            return True
        if granted.endswith(".*") and operation.startswith(granted[:-1]):
            return True
    return False


class SharedSecretChannelAuth:
    """Per-caller shared-secret verifier (fail-closed on every branch)."""

    def __init__(self, secrets: dict[str, str], clock: Clock) -> None:
        self._secrets = dict(secrets)
        self._clock = clock

    def register(self, caller: str, secret: str) -> None:
        self._secrets[caller] = secret

    def verify(
        self,
        *,
        caller: str | None,
        method: str,
        path: str,
        body: bytes,
        ts: str | None,
        signature: str | None,
    ) -> str:
        """Returns the authenticated caller identity or raises (401)."""
        if not caller or not ts or not signature:
            raise AuthenticationError(
                "channel auth headers missing", code="vault_channel_unauthenticated"
            )
        secret = self._secrets.get(caller)
        if secret is None:
            raise AuthenticationError(
                "unknown channel caller", code="vault_channel_unauthenticated"
            )
        try:
            presented_at = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError as exc:
            raise AuthenticationError(
                "channel timestamp malformed", code="vault_channel_unauthenticated"
            ) from exc
        if presented_at.tzinfo is None:
            raise AuthenticationError(
                "channel timestamp must be timezone-aware", code="vault_channel_unauthenticated"
            )
        now = self._clock.now()
        skew = abs(now - presented_at.astimezone(UTC))
        if skew > MAX_SKEW:
            raise AuthenticationError(
                "channel timestamp outside freshness window",
                code="vault_channel_unauthenticated",
            )
        expected = sign_channel_request(
            secret, caller=caller, method=method, path=path, body=body, ts=ts
        )
        if not hmac_mod.compare_digest(expected, signature):
            raise AuthenticationError(
                "channel signature mismatch", code="vault_channel_unauthenticated"
            )
        return caller


def authorization_denied(operation: str) -> AuthorizationError:
    """The binding 403 for an identity outside the allowlisted operation set."""
    return AuthorizationError(
        f"caller is not allowlisted for {operation}", code="vault_caller_not_allowlisted"
    )

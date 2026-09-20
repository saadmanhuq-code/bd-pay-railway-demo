"""Authenticated principals — the gateway's post-auth identity record."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

__all__ = ["Principal"]

_KINDS = ("merchant_key", "customer", "operator")


@dataclass(frozen=True)
class Principal:
    """Who is calling: one of merchant API key, customer JWT, operator session.

    ``scopes`` applies to merchant keys and customer tokens; ``roles`` applies
    to operator sessions. ``totp_verified_at`` carries the operator's last 2FA
    instant for freshness gates (spec/01 §E refund 2FA). ``key_env`` is
    ``live``/``test`` for merchant keys (rate-limit tiering).
    """

    kind: str
    principal_id: str
    merchant_id: str | None = None
    customer_id: str | None = None
    operator_id: str | None = None
    scopes: frozenset[str] = field(default_factory=frozenset)
    roles: frozenset[str] = field(default_factory=frozenset)
    kyc_tier: str | None = None
    key_env: str | None = None
    totp_verified_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.kind not in _KINDS:
            raise ValueError(f"kind must be one of {_KINDS}, got {self.kind!r}")
        if not self.principal_id:
            raise ValueError("principal_id must be non-empty")

    @property
    def rate_key(self) -> str:
        """Stable per-principal rate-limit bucket key component."""
        return f"{self.kind}:{self.principal_id}"

"""Environment-driven settings for the vault sub-process (spec/14 topology).

Secrets arrive via environment only (config files in evidence pack are
deployment concerns); nothing here is ever logged. ``VAULT_CALLER_SECRETS``
is a JSON object mapping caller identity (SPIFFE-style URI) -> shared
secret for the channel auth (errata LB7).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field

from bdpay.platform.deployment_env import (
    DeploymentEnvironmentError,
    deployment_is_production,
)

__all__ = ["VaultSettings"]

_DEFAULT_ACQUIRER_HOSTS = ("api.acquirer-bank.example",)


_WEAK_CALLER_SECRETS = frozenset(
    {
        "",
        "s1",
        "secret",
        "secret-gateway",
        "secret-stranger",
        "dev-only-rehearsal-secret",
        "changeme",
        "test",
    }
)


def _validate_caller_secrets(
    caller_secrets: dict[str, str], *, is_production: bool
) -> None:
    for caller, secret in caller_secrets.items():
        if not caller:
            raise ValueError("VAULT_CALLER_SECRETS caller ids must be non-empty")
        if not secret:
            raise ValueError("VAULT_CALLER_SECRETS values must be non-empty")
    if not is_production:
        return
    if not caller_secrets:
        raise ValueError("VAULT_CALLER_SECRETS is required in production")
    for caller, secret in caller_secrets.items():
        normalized = secret.strip()
        if (
            normalized in _WEAK_CALLER_SECRETS
            or len(normalized) < 32
            or len(set(normalized)) <= 8
        ):
            raise ValueError(
                "VAULT_CALLER_SECRETS contains a weak shared secret for "
                f"{caller!r}; production requires a high-entropy value"
            )


@dataclass(frozen=True)
class VaultSettings:
    listen_host: str = "127.0.0.1"
    listen_port: int = 9614
    caller_secrets: dict[str, str] = field(default_factory=dict)
    acquirer_hosts: tuple[str, ...] = _DEFAULT_ACQUIRER_HOSTS
    import_window_open: bool = False
    master_key_hex: str | None = None
    egress_mode: str = "simulator"  # simulator | live (sandbox/live = real wire)
    tls_verify: bool = True
    #: RFC 7469 SPKI pins (``base64(sha256(SPKI-DER))``) for the acquirer egress
    #: channel. Empty = rely on ``tls_verify`` alone (pinning is opt-in via config).
    acquirer_spki_pins: frozenset[str] = frozenset()
    #: When True the vault runs in production: a soft-HSM key root is refused at
    #: the crypto boundary (PCI Req 3). Direct construction defaults False for
    #: tests; ``from_env({})`` treats missing env posture as production.
    is_production: bool = False

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> VaultSettings:
        secrets_raw = env.get("VAULT_CALLER_SECRETS", "{}")
        try:
            caller_secrets = {str(k): str(v) for k, v in json.loads(secrets_raw).items()}
        except (ValueError, AttributeError) as exc:
            raise ValueError("VAULT_CALLER_SECRETS must be a JSON object") from exc
        hosts_raw = env.get("VAULT_ACQUIRER_HOSTS", "")
        hosts = tuple(h.strip() for h in hosts_raw.split(",") if h.strip()) or (
            _DEFAULT_ACQUIRER_HOSTS
        )
        pins_raw = env.get("VAULT_ACQUIRER_SPKI_PINS", "")
        pins = frozenset(p.strip() for p in pins_raw.split(",") if p.strip())
        try:
            is_production = deployment_is_production(env)
        except DeploymentEnvironmentError as exc:
            raise ValueError(str(exc)) from exc
        _validate_caller_secrets(caller_secrets, is_production=is_production)
        return cls(
            listen_host=env.get("VAULT_LISTEN_HOST", "127.0.0.1"),
            listen_port=int(env.get("VAULT_LISTEN_PORT", "9614")),
            caller_secrets=caller_secrets,
            acquirer_hosts=hosts,
            import_window_open=env.get("VAULT_IMPORT_WINDOW", "") == "open",
            master_key_hex=env.get("VAULT_MASTER_KEY_HEX") or None,
            egress_mode=env.get("VAULT_EGRESS_MODE", "simulator"),
            tls_verify=env.get("VAULT_TLS_VERIFY", "1") != "0",
            acquirer_spki_pins=pins,
            is_production=is_production,
        )

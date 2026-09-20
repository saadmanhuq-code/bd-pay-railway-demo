"""Gateway settings — env-driven, immutable, injected (never read ambiently).

All thresholds are integer paisa; all durations are integer seconds. The HMAC
master key encrypts API-key HMAC material and operator TOTP secrets at rest
(AES-256-GCM); in the spec/01 deployment it lives in the secrets manager —
here it arrives via environment/injection and never appears in any log.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

from bdpay.platform.deployment_env import (
    DeploymentEnvironmentError,
    deployment_env_label,
    deployment_is_production,
)

__all__ = ["GatewaySettings", "GatewayCredentialError"]

_RATE_LIMIT_MODES = ("enforce", "shadow")

_WEAK_JWT_SECRETS = frozenset({"", "dev-jwt-secret-32chars!!", "changeme", "secret", "test"})
_WEAK_HMAC_KEYS = frozenset({"ab" * 32, "00" * 32, "ff" * 32, "de" * 32, "cd" * 32})


class GatewayCredentialError(ValueError):
    """Raised when production boots with a missing or known-weak gateway secret."""


@dataclass(frozen=True)
class GatewaySettings:
    """Immutable gateway configuration (spec/01 binding values as defaults)."""

    jwt_secret: str
    hmac_master_key_hex: str
    jwt_previous_secret: str | None = None  # rotation grace (spec/01 §B verify step 2)
    bcrypt_rounds: int = 12
    access_token_ttl_seconds: int = 900  # customer JWT, 15 min
    operator_session_ttl_seconds: int = 28_800  # 8 h
    operator_inactivity_timeout_seconds: int = 900  # 15 min
    totp_step_seconds: int = 30
    totp_digits: int = 6
    totp_window_steps: int = 1  # +/- 1 step (spec/01 failure modes: clock skew)
    totp_max_failures: int = 3  # BB ICT 3-attempt lockout
    totp_failure_window_seconds: int = 1800  # 30 min
    operator_lock_seconds: int = 1800  # 30 min lock
    otp_session_ttl_seconds: int = 300  # customer OTP session window
    customer_confirm_2fa_threshold_minor: int = 10_000  # BDT 100.00
    operator_refund_2fa_threshold_minor: int = 100_000  # BDT 1,000.00
    operator_refund_totp_freshness_seconds: int = 900
    hmac_timestamp_tolerance_seconds: int = 300
    hmac_replay_ttl_seconds: int = 600
    rate_limit_mode: str = "enforce"  # enforce | shadow (spec/01 shadow rollout)
    rate_limit_fail_open: bool = True  # spec/01 documented inversion of the source default
    public_ip_limit_per_minute: int = 10
    idempotency_ttl_seconds: int = 86_400
    max_stored_response_bytes: int = 65_536  # 64KB replay-body cap
    max_request_body_bytes: int = 1_048_576  # 1 MiB edge request-body cap
    request_id_seed: str = ""
    cors_allowed_origins: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.jwt_secret:
            raise ValueError("jwt_secret must be non-empty")
        key = self.hmac_master_key_hex
        if len(key) != 64 or any(c not in "0123456789abcdefABCDEF" for c in key):
            raise ValueError("hmac_master_key_hex must be 64 hex chars (32 bytes)")
        if self.rate_limit_mode not in _RATE_LIMIT_MODES:
            raise ValueError(
                f"rate_limit_mode must be one of {_RATE_LIMIT_MODES}, "
                f"got {self.rate_limit_mode!r}"
            )
        for name in (
            "bcrypt_rounds",
            "access_token_ttl_seconds",
            "operator_session_ttl_seconds",
            "operator_inactivity_timeout_seconds",
            "totp_step_seconds",
            "totp_digits",
            "totp_max_failures",
            "totp_failure_window_seconds",
            "operator_lock_seconds",
            "otp_session_ttl_seconds",
            "customer_confirm_2fa_threshold_minor",
            "operator_refund_2fa_threshold_minor",
            "operator_refund_totp_freshness_seconds",
            "hmac_timestamp_tolerance_seconds",
            "hmac_replay_ttl_seconds",
            "public_ip_limit_per_minute",
            "idempotency_ttl_seconds",
            "max_stored_response_bytes",
            "max_request_body_bytes",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive int, got {value!r}")
        if self.totp_window_steps < 0:
            raise ValueError("totp_window_steps must be >= 0")
        origins = tuple(str(origin) for origin in self.cors_allowed_origins)
        normalized_origins = tuple(origin.strip() for origin in origins)
        if origins != normalized_origins or any(not origin for origin in origins):
            raise ValueError("cors_allowed_origins must contain trimmed non-empty origins")
        if "*" in origins:
            raise ValueError("cors_allowed_origins must not contain '*'")

    @property
    def hmac_master_key(self) -> bytes:
        return bytes.fromhex(self.hmac_master_key_hex)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> GatewaySettings:
        """Build settings from an environment mapping (pure, test-friendly)."""
        source = os.environ if env is None else env
        jwt_secret = source.get("GATEWAY_JWT_SECRET", "")
        master = source.get("GATEWAY_HMAC_MASTER_KEY_HEX", "")
        previous = source.get("GATEWAY_JWT_PREVIOUS_SECRET") or None
        # Fail closed unless the deployment explicitly opts into a non-production
        # posture. Missing/unknown env labels are production by default, and a
        # prod/non-prod BDPAY_ENV/VAULT_ENV split is refused.
        try:
            is_production = deployment_is_production(source)
        except DeploymentEnvironmentError as exc:
            raise GatewayCredentialError(str(exc)) from exc
        env_label = deployment_env_label(source)
        if is_production:
            if jwt_secret in _WEAK_JWT_SECRETS or len(jwt_secret) < 32:
                raise GatewayCredentialError(
                    "GATEWAY_JWT_SECRET is missing or weak (dev default / <32 chars); "
                    f"refusing to boot in production (BDPAY_ENV={env_label!r})"
                )
            if master.lower() in _WEAK_HMAC_KEYS or len(set(master.lower())) <= 2:
                raise GatewayCredentialError(
                    "GATEWAY_HMAC_MASTER_KEY_HEX is missing or weak (dev default / "
                    f"low-entropy); refusing to boot in production (BDPAY_ENV={env_label!r})"
                )
        kwargs: dict[str, object] = {}
        for env_name, field_name in (
            ("GATEWAY_BCRYPT_ROUNDS", "bcrypt_rounds"),
            ("GATEWAY_PUBLIC_IP_LIMIT_PER_MINUTE", "public_ip_limit_per_minute"),
            ("GATEWAY_IDEMPOTENCY_TTL_SECONDS", "idempotency_ttl_seconds"),
            ("GATEWAY_MAX_REQUEST_BODY_BYTES", "max_request_body_bytes"),
        ):
            raw = source.get(env_name)
            if raw is not None:
                kwargs[field_name] = int(raw.strip(), 10)
        mode = source.get("GATEWAY_RATE_LIMIT_MODE")
        if mode is not None:
            kwargs["rate_limit_mode"] = mode.strip()
        origins = source.get("GATEWAY_CORS_ALLOWED_ORIGINS")
        if origins is not None:
            kwargs["cors_allowed_origins"] = tuple(
                origin.strip() for origin in origins.split(",") if origin.strip()
            )
        if is_production:
            if int(kwargs.get("bcrypt_rounds", cls.bcrypt_rounds)) < 12:
                raise GatewayCredentialError(
                    "GATEWAY_BCRYPT_ROUNDS must be >= 12; refusing to boot in "
                    f"production (BDPAY_ENV={env_label!r})"
                )
            if str(kwargs.get("rate_limit_mode", cls.rate_limit_mode)).strip().lower() == "shadow":
                raise GatewayCredentialError(
                    "GATEWAY_RATE_LIMIT_MODE=shadow is not allowed in production; "
                    f"refusing to boot (BDPAY_ENV={env_label!r})"
                )
        return cls(
            jwt_secret=jwt_secret,
            hmac_master_key_hex=master,
            jwt_previous_secret=previous,
            **kwargs,  # type: ignore[arg-type]
        )

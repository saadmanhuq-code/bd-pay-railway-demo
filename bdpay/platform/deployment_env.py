"""Deployment-environment posture helpers.

Missing or unknown environment labels are production by default. When both
BDPAY_ENV and VAULT_ENV are present, either production label wins; a mixed
production/non-production pair is refused as a configuration error.
"""

from __future__ import annotations

from collections.abc import Mapping

__all__ = [
    "DeploymentEnvironmentError",
    "deployment_env_label",
    "deployment_is_production",
]

_PRODUCTION_ENV_NAMES = frozenset({"production", "prod", "live"})
_NON_PRODUCTION_ENV_NAMES = frozenset(
    {"development", "dev", "test", "local", "ci", "rehearsal", "simulator"}
)


class DeploymentEnvironmentError(ValueError):
    """Raised when deployment posture env vars contradict each other."""


def _labels(env: Mapping[str, str]) -> dict[str, str]:
    return {
        key: value.strip().lower()
        for key in ("BDPAY_ENV", "VAULT_ENV")
        if (value := str(env.get(key) or "").strip())
    }


def deployment_env_label(env: Mapping[str, str]) -> str:
    labels = _labels(env)
    if not labels:
        return "default-production"
    return ",".join(f"{key}={value}" for key, value in sorted(labels.items()))


def deployment_is_production(env: Mapping[str, str]) -> bool:
    labels = _labels(env)
    if not labels:
        return True
    saw_prod = any(value in _PRODUCTION_ENV_NAMES for value in labels.values())
    saw_nonprod = any(value in _NON_PRODUCTION_ENV_NAMES for value in labels.values())
    if saw_prod and saw_nonprod:
        raise DeploymentEnvironmentError(
            "conflicting deployment posture: " + deployment_env_label(env)
        )
    if saw_prod:
        return True
    if any(
        value not in _PRODUCTION_ENV_NAMES and value not in _NON_PRODUCTION_ENV_NAMES
        for value in labels.values()
    ):
        return True
    return False

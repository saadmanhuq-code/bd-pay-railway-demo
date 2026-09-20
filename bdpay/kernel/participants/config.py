"""Injectable spec/19 PSO-1 config keys.

spec/19 §Config registers ``PSO_LICENSE_ACTIVE`` (default false) and
``CONFORMANCE_RETRY_LIMIT`` (default 3) as platform config. The platform
``Settings`` dataclass is platform-lane-owned and frozen for this lane, so
the keys ship here as an injectable, env-driven config object that folds
into ``bdpay.platform.config.Settings`` at the wiring pass (errata row
P19-7, pinned by ``tests/kernel/participants/test_errata_pins.py``).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

__all__ = ["ParticipantOnboardingConfig"]

#: The published conformance suite version (spec/19 §API A / FSM 2).
DEFAULT_SUITE_VERSION = "pso-conf-v1"


@dataclass(frozen=True)
class ParticipantOnboardingConfig:
    """Immutable PSO-1 settings; constructed once at composition time."""

    pso_license_active: bool = False  # PSO_LICENSE_ACTIVE (D-19-1 activation guard)
    conformance_retry_limit: int = 3  # CONFORMANCE_RETRY_LIMIT (FSM 1 guard)
    suite_version: str = DEFAULT_SUITE_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.pso_license_active, bool):
            raise ValueError(
                f"pso_license_active must be a boolean, got {self.pso_license_active!r}"
            )
        if (
            isinstance(self.conformance_retry_limit, bool)
            or not isinstance(self.conformance_retry_limit, int)
            or self.conformance_retry_limit < 1
        ):
            raise ValueError(
                "conformance_retry_limit must be an int >= 1, "
                f"got {self.conformance_retry_limit!r}"
            )
        if not isinstance(self.suite_version, str) or not self.suite_version:
            raise ValueError("suite_version must be a non-empty string")

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> ParticipantOnboardingConfig:
        """Build from an environment mapping (pure, test-friendly)."""
        defaults = cls()
        return cls(
            pso_license_active=_bool_var(
                env, "PSO_LICENSE_ACTIVE", defaults.pso_license_active
            ),
            conformance_retry_limit=_int_var(
                env, "CONFORMANCE_RETRY_LIMIT", defaults.conformance_retry_limit
            ),
            suite_version=env.get("PSO_CONFORMANCE_SUITE_VERSION", defaults.suite_version),
        )


def _bool_var(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = env.get(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in ("true", "1", "yes"):
        return True
    if value in ("false", "0", "no"):
        return False
    raise ValueError(f"{name} must be a boolean (true/false), got {raw!r}")


def _int_var(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name)
    if raw is None:
        return default
    try:
        return int(raw.strip(), 10)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc

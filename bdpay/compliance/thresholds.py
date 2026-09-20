"""Versioned, YAML-externalized compliance thresholds (spec/06 + spec/07).

The CAMLCO-approved thresholds artifact lives at
``bdpay/compliance/config/risk_thresholds.yaml``. Nothing in the rule pack or
code hardcodes these values; the active ``thresholds_version`` is recorded on
every screening row and risk-score row so historical decisions stay
reproducible (spec/07 step 6; BishomIQ confidence-threshold routing pattern).

A parse failure refuses to produce new thresholds (spec/06 failure-mode table:
"Risk scoring thresholds YAML parse error" — the service refuses; the last
valid version remains in use for in-flight requests).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

import yaml

__all__ = ["ComplianceThresholds", "ThresholdsError", "load_thresholds"]

DEFAULT_THRESHOLDS_PATH = Path(__file__).resolve().parent / "config" / "risk_thresholds.yaml"


class ThresholdsError(ValueError):
    """Raised when the thresholds artifact is missing, malformed, or invalid."""


@dataclass(frozen=True)
class ComplianceThresholds:
    """Immutable snapshot of one thresholds artifact version."""

    thresholds_version: str
    # velocity (spec/06 rule_velocity_24h_spike)
    velocity_24h_count_threshold: int
    velocity_24h_amount_threshold_minor: int
    # structuring (spec/06 rule_structuring_detection_3day)
    structuring_window_hours: int
    structuring_min_txn_count: int
    structuring_aggregate_threshold_minor: int
    # mule (spec/06 rule_mule_account_pattern)
    mule_fan_in_sender_count: int
    mule_forward_window_seconds: int
    mule_fan_out_recipient_count: int
    # account-behaviour baseline (dormant reactivation)
    dormancy_days: int
    dormancy_spike_txn_count_24h: int
    # CTR (spec/06 CtrAggregation pipeline)
    ctr_threshold_minor: int
    # STR retry + filing clock (spec/06 StrReport FSM)
    max_str_retry: int
    str_retry_initial_delay_seconds: int
    str_retry_max_delay_seconds: int
    str_filing_clock_hours: int
    # alert escalation TTLs (spec/06 AmlAlert FSM)
    raised_escalation_hours: int
    triaging_escalation_hours: int
    str_camlco_review_hours: int
    # sanctions matching (spec/07 step 6) — Decimal, never float
    t_hit: Decimal
    t_review: Decimal

    def __post_init__(self) -> None:
        for name in (
            "velocity_24h_count_threshold",
            "velocity_24h_amount_threshold_minor",
            "structuring_window_hours",
            "structuring_min_txn_count",
            "structuring_aggregate_threshold_minor",
            "mule_fan_in_sender_count",
            "mule_forward_window_seconds",
            "mule_fan_out_recipient_count",
            "dormancy_days",
            "dormancy_spike_txn_count_24h",
            "ctr_threshold_minor",
            "max_str_retry",
            "str_retry_initial_delay_seconds",
            "str_retry_max_delay_seconds",
            "str_filing_clock_hours",
            "raised_escalation_hours",
            "triaging_escalation_hours",
            "str_camlco_review_hours",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ThresholdsError(f"{name} must be a positive int, got {value!r}")
        if not isinstance(self.thresholds_version, str) or not self.thresholds_version:
            raise ThresholdsError("thresholds_version must be a non-empty string")
        for name in ("t_hit", "t_review"):
            value = getattr(self, name)
            if not isinstance(value, Decimal):
                raise ThresholdsError(f"{name} must be Decimal (floats rejected), got {value!r}")
            if not Decimal(0) < value <= Decimal(1):
                raise ThresholdsError(f"{name} must be in (0, 1], got {value}")
        if self.t_review >= self.t_hit:
            raise ThresholdsError(
                f"T_REVIEW ({self.t_review}) must be strictly below T_HIT ({self.t_hit})"
            )


def _section(doc: dict, key: str) -> dict:
    value = doc.get(key)
    if not isinstance(value, dict):
        raise ThresholdsError(f"thresholds artifact is missing the {key!r} section")
    return value


def _int_of(section: dict, key: str, where: str) -> int:
    value = section.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ThresholdsError(f"{where}.{key} must be an integer, got {value!r}")
    return value


def _decimal_of(section: dict, key: str, where: str) -> Decimal:
    value = section.get(key)
    if not isinstance(value, str):
        raise ThresholdsError(
            f"{where}.{key} must be a quoted decimal string (floats rejected), got {value!r}"
        )
    try:
        return Decimal(value)
    except InvalidOperation as exc:
        raise ThresholdsError(f"{where}.{key} is not a valid decimal: {value!r}") from exc


def load_thresholds(path: Path | None = None) -> ComplianceThresholds:
    """Load and validate the thresholds artifact (refuses on any defect)."""
    artifact = DEFAULT_THRESHOLDS_PATH if path is None else path
    try:
        raw = artifact.read_text(encoding="utf-8")
    except OSError as exc:
        raise ThresholdsError(f"cannot read thresholds artifact at {artifact}") from exc
    try:
        doc = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ThresholdsError("thresholds artifact is not valid YAML") from exc
    if not isinstance(doc, dict):
        raise ThresholdsError("thresholds artifact must be a YAML mapping")
    version = doc.get("thresholds_version")
    if not isinstance(version, str) or not version:
        raise ThresholdsError("thresholds_version must be a non-empty string")
    velocity = _section(doc, "velocity_rules")
    structuring = _section(doc, "structuring")
    mule = _section(doc, "mule_detection")
    dormancy = _section(doc, "dormancy")
    ctr = _section(doc, "ctr")
    str_retry = _section(doc, "str_retry")
    filing_clock = _section(doc, "str_filing_clock")
    escalation = _section(doc, "alert_escalation")
    matching = _section(doc, "sanctions_matching")
    return ComplianceThresholds(
        thresholds_version=version,
        velocity_24h_count_threshold=_int_of(
            velocity, "VELOCITY_24H_COUNT_THRESHOLD", "velocity_rules"
        ),
        velocity_24h_amount_threshold_minor=_int_of(
            velocity, "VELOCITY_24H_AMOUNT_THRESHOLD_MINOR", "velocity_rules"
        ),
        structuring_window_hours=_int_of(structuring, "STRUCTURING_WINDOW_HOURS", "structuring"),
        structuring_min_txn_count=_int_of(structuring, "STRUCTURING_MIN_TXN_COUNT", "structuring"),
        structuring_aggregate_threshold_minor=_int_of(
            structuring, "STRUCTURING_AGGREGATE_THRESHOLD_MINOR", "structuring"
        ),
        mule_fan_in_sender_count=_int_of(mule, "MULE_FAN_IN_SENDER_COUNT", "mule_detection"),
        mule_forward_window_seconds=_int_of(
            mule, "MULE_FORWARD_WINDOW_SECONDS", "mule_detection"
        ),
        mule_fan_out_recipient_count=_int_of(
            mule, "MULE_FAN_OUT_RECIPIENT_COUNT", "mule_detection"
        ),
        dormancy_days=_int_of(dormancy, "DORMANCY_DAYS", "dormancy"),
        dormancy_spike_txn_count_24h=_int_of(
            dormancy, "DORMANCY_SPIKE_TXN_COUNT_24H", "dormancy"
        ),
        ctr_threshold_minor=_int_of(ctr, "CTR_THRESHOLD_MINOR", "ctr"),
        max_str_retry=_int_of(str_retry, "MAX_STR_RETRY", "str_retry"),
        str_retry_initial_delay_seconds=_int_of(
            str_retry, "STR_RETRY_INITIAL_DELAY_SECONDS", "str_retry"
        ),
        str_retry_max_delay_seconds=_int_of(
            str_retry, "STR_RETRY_MAX_DELAY_SECONDS", "str_retry"
        ),
        str_filing_clock_hours=_int_of(
            filing_clock, "STR_FILING_CLOCK_HOURS", "str_filing_clock"
        ),
        raised_escalation_hours=_int_of(
            escalation, "RAISED_ESCALATION_HOURS", "alert_escalation"
        ),
        triaging_escalation_hours=_int_of(
            escalation, "TRIAGING_ESCALATION_HOURS", "alert_escalation"
        ),
        str_camlco_review_hours=_int_of(
            escalation, "STR_CAMLCO_REVIEW_HOURS", "alert_escalation"
        ),
        t_hit=_decimal_of(matching, "T_HIT", "sanctions_matching"),
        t_review=_decimal_of(matching, "T_REVIEW", "sanctions_matching"),
    )

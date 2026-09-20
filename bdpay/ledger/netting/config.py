"""Netting configuration keys registered by spec/19 (PSO-2 rows).

The keys live in their own dataclass (not ``bdpay.platform.config.Settings``)
because the platform settings module is owned by another lane; the values fold
into ``Settings`` at the next integration pass. All keys are injectable; none
varies engine semantics silently:

- ``PSO_UNWIND_POLICY`` records the D-19-2 ratified decision. The only
  buildable value in v1 is ``UNWIND_AND_RECOMPUTE`` — any other value is
  REFUSED at construction (config records the decision, it does not vary
  behavior; a carry-forward direction from the principal is a spec change).
- ``PSO_MAX_UNWIND_ROUNDS`` defaults to 1 (D-19-2: max one unwind round per
  window; a second failure leaves the window ``SETTLEMENT_FAILED``).
- ``PSO_NETTING_INSTRUCTION_RAIL`` defaults to ``rtgs_iso20022_v1`` (D-19-3;
  VERIFY-BEFORE-EXTERNAL FC-08). Must be a registered spec/04 rail.
- ``PSO_DNS_SESSION_SCHEDULE`` defaults to ``AM=11:59,PM=14:59,EOD=17:59``
  Asia/Dhaka (spec/05 Open Question 2 assumption; VERIFY-BEFORE-EXTERNAL
  FC-07).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import time

from bdpay.ledger.settlement.types import RAILS

__all__ = [
    "DEFAULT_SESSION_SCHEDULE",
    "PSO_SESSIONS",
    "NettingConfig",
    "parse_session_schedule",
]

#: The three PSO DNS sessions, in within-day order (spec/19 window calendar).
PSO_SESSIONS: tuple[str, ...] = ("AM", "PM", "EOD")

#: spec/19 defaults — Asia/Dhaka local cutoff clock times (FC-07 assumption).
DEFAULT_SESSION_SCHEDULE: dict[str, time] = {
    "AM": time(11, 59),
    "PM": time(14, 59),
    "EOD": time(17, 59),
}

UNWIND_AND_RECOMPUTE = "UNWIND_AND_RECOMPUTE"


def parse_session_schedule(raw: str) -> dict[str, time]:
    """Parse ``"AM=11:59,PM=14:59,EOD=17:59"`` into session cutoff times.

    All three sessions are required, cutoffs must be strictly increasing
    within the day, and unknown session names are refused (closed set).
    """
    cutoffs: dict[str, time] = {}
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        name, _, clock_text = chunk.partition("=")
        name = name.strip().upper()
        if name not in PSO_SESSIONS:
            raise ValueError(f"unknown PSO DNS session {name!r}; allowed: {PSO_SESSIONS}")
        if name in cutoffs:
            raise ValueError(f"duplicate PSO DNS session {name!r}")
        hh, _, mm = clock_text.strip().partition(":")
        try:
            cutoffs[name] = time(int(hh, 10), int(mm, 10))
        except ValueError as exc:
            raise ValueError(f"bad cutoff time {clock_text!r} for session {name}") from exc
    missing = [s for s in PSO_SESSIONS if s not in cutoffs]
    if missing:
        raise ValueError(f"PSO_DNS_SESSION_SCHEDULE missing session(s) {missing}")
    ordered = [cutoffs[s] for s in PSO_SESSIONS]
    if not (ordered[0] < ordered[1] < ordered[2]):
        raise ValueError("PSO DNS session cutoffs must be strictly increasing (AM < PM < EOD)")
    return cutoffs


@dataclass(frozen=True)
class NettingConfig:
    """Immutable netting configuration (spec/19 config-key table)."""

    unwind_policy: str = UNWIND_AND_RECOMPUTE
    max_unwind_rounds: int = 1
    instruction_rail: str = "rtgs_iso20022_v1"
    session_schedule: Mapping[str, time] = field(
        default_factory=lambda: dict(DEFAULT_SESSION_SCHEDULE)
    )

    def __post_init__(self) -> None:
        if self.unwind_policy != UNWIND_AND_RECOMPUTE:
            raise ValueError(
                "PSO_UNWIND_POLICY must be 'UNWIND_AND_RECOMPUTE' (D-19-2, pinned); "
                f"got {self.unwind_policy!r} — carry-forward is a spec change, not a config flip"
            )
        if (
            isinstance(self.max_unwind_rounds, bool)
            or not isinstance(self.max_unwind_rounds, int)
            or self.max_unwind_rounds < 1
        ):
            raise ValueError(
                f"PSO_MAX_UNWIND_ROUNDS must be an int >= 1, got {self.max_unwind_rounds!r}"
            )
        if self.instruction_rail not in RAILS:
            raise ValueError(
                f"PSO_NETTING_INSTRUCTION_RAIL {self.instruction_rail!r} is not a "
                f"registered spec/04 rail"
            )
        missing = [s for s in PSO_SESSIONS if s not in self.session_schedule]
        if missing:
            raise ValueError(f"session_schedule missing session(s) {missing}")

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> NettingConfig:
        """Build from an environment mapping (pure, test-friendly)."""
        defaults = cls()
        raw_rounds = env.get("PSO_MAX_UNWIND_ROUNDS")
        raw_schedule = env.get("PSO_DNS_SESSION_SCHEDULE")
        return cls(
            unwind_policy=env.get("PSO_UNWIND_POLICY", defaults.unwind_policy).strip(),
            max_unwind_rounds=(
                defaults.max_unwind_rounds if raw_rounds is None else int(raw_rounds.strip(), 10)
            ),
            instruction_rail=env.get(
                "PSO_NETTING_INSTRUCTION_RAIL", defaults.instruction_rail
            ).strip(),
            session_schedule=(
                dict(DEFAULT_SESSION_SCHEDULE)
                if raw_schedule is None
                else parse_session_schedule(raw_schedule)
            ),
        )

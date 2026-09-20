"""Refusal-first finite-state-machine engine for the kernel package.

Spec/00 §8 notation (binding): every FSM is a transition table with columns
``from | trigger | guard | to | side_effects``; terminal states are explicit;
the default rule is DENY — any (state, trigger) pair not declared in the
table is refused with a typed error, and terminal states refuse every trigger.

This module is the single transition arbiter for the kernel's FSMs
(PaymentIntent / PaymentAttempt / Refund from spec/02, merchant KYB /
participant onboarding / document review from spec/08, KycRecord / manual
review from spec/09). Services never assign a state directly; they ask the
table to resolve the transition first, which is what makes the refusal-first
default non-bypassable.

Where one (from, trigger) pair legally has more than one outcome (e.g.
``TIMED_OUT --poll_success--> AUTHORIZED | CHARGED`` in spec/02 FSM 2), each
outcome carries a ``guard`` key and the caller must name the branch; an
ambiguous call without a guard is denied, never defaulted.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from bdpay.platform.errors import ConflictError

__all__ = ["TransitionDeniedError", "TransitionRule", "TransitionTable"]


class TransitionDeniedError(ConflictError):
    """A transition outside the declared table — refused (spec/00 §8).

    Maps to the spec/02 error envelope ``409 conflict/invalid_state_transition``.
    """

    default_code = "invalid_state_transition"

    def __init__(
        self,
        message: str,
        *,
        machine: str,
        from_state: str,
        trigger: str,
        code: str | None = None,
    ) -> None:
        super().__init__(message, code=code or self.default_code)
        self.machine = machine
        self.from_state = from_state
        self.trigger = trigger


@dataclass(frozen=True)
class TransitionRule:
    """One row of a spec/00 §8 transition table."""

    from_state: str
    trigger: str
    to_state: str
    #: Branch key when one (from, trigger) pair has multiple declared outcomes.
    guard: str | None = None
    #: Declarative side-effect labels (ledger/audit/event) — documentation of
    #: the spec row; the owning service performs them.
    side_effects: tuple[str, ...] = field(default_factory=tuple)


class TransitionTable:
    """An immutable, validated transition table with refusal-first resolve."""

    def __init__(
        self,
        name: str,
        *,
        states: Iterable[str],
        terminal_states: Iterable[str],
        rules: Iterable[TransitionRule],
    ) -> None:
        self._name = name
        self._states = frozenset(states)
        self._terminal = frozenset(terminal_states)
        if not self._terminal <= self._states:
            unknown = sorted(self._terminal - self._states)
            raise ValueError(f"{name}: terminal states not in state set: {unknown}")
        index: dict[tuple[str, str], tuple[TransitionRule, ...]] = {}
        for rule in rules:
            if rule.from_state not in self._states:
                raise ValueError(f"{name}: rule from unknown state {rule.from_state!r}")
            if rule.to_state not in self._states:
                raise ValueError(f"{name}: rule to unknown state {rule.to_state!r}")
            if rule.from_state in self._terminal:
                raise ValueError(
                    f"{name}: terminal state {rule.from_state!r} cannot have an "
                    "outgoing transition (terminal states are immutable)"
                )
            key = (rule.from_state, rule.trigger)
            existing = index.get(key, ())
            if any(r.guard == rule.guard for r in existing):
                raise ValueError(
                    f"{name}: duplicate rule for ({rule.from_state!r}, "
                    f"{rule.trigger!r}, guard={rule.guard!r})"
                )
            index[key] = (*existing, rule)
        for key, candidates in index.items():
            if len(candidates) > 1 and any(r.guard is None for r in candidates):
                raise ValueError(
                    f"{name}: ambiguous rules for {key!r} must all carry guard keys"
                )
        self._index = index

    # -- introspection -------------------------------------------------------

    @property
    def name(self) -> str:
        return self._name

    @property
    def states(self) -> frozenset[str]:
        return self._states

    @property
    def terminal_states(self) -> frozenset[str]:
        return self._terminal

    @property
    def rules(self) -> tuple[TransitionRule, ...]:
        return tuple(rule for rules in self._index.values() for rule in rules)

    def is_terminal(self, state: str) -> bool:
        return state in self._terminal

    def rules_for(self, from_state: str, trigger: str) -> tuple[TransitionRule, ...]:
        """Declared rules for the pair — empty tuple when undeclared."""
        return self._index.get((from_state, trigger), ())

    # -- the arbiter ---------------------------------------------------------

    def resolve(
        self, from_state: str, trigger: str, *, guard: str | None = None
    ) -> TransitionRule:
        """Return the single declared rule for (from_state, trigger[, guard]).

        Refusal-first: unknown state, terminal state, undeclared pair,
        unmatched guard, and ambiguous-without-guard all raise
        :class:`TransitionDeniedError`. Nothing is ever defaulted.
        """

        def deny(detail: str) -> TransitionDeniedError:
            return TransitionDeniedError(
                f"{self._name}: transition denied from {from_state!r} on "
                f"{trigger!r}: {detail}",
                machine=self._name,
                from_state=from_state,
                trigger=trigger,
            )

        if from_state not in self._states:
            raise deny("unknown state")
        if from_state in self._terminal:
            raise deny("terminal states are immutable")
        candidates = self._index.get((from_state, trigger), ())
        if not candidates:
            raise deny("no such transition is declared in the table")
        if len(candidates) == 1 and candidates[0].guard is None:
            if guard is not None:
                raise deny(f"guard {guard!r} supplied but the rule is unguarded")
            return candidates[0]
        if guard is None:
            raise deny(
                "this transition has guarded branches; the caller must name one of "
                f"{sorted(r.guard for r in candidates if r.guard is not None)}"
            )
        for rule in candidates:
            if rule.guard == guard:
                return rule
        raise deny(
            f"guard {guard!r} does not match a declared branch "
            f"{sorted(r.guard for r in candidates if r.guard is not None)}"
        )

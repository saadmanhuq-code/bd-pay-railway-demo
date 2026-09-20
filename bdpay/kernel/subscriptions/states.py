"""Refusal-first FSM tables for spec/17 VX3 subscriptions."""

from __future__ import annotations

from bdpay.kernel.fsm import TransitionRule, TransitionTable

__all__ = ["MANDATE_TABLE", "SUBSCRIPTION_CYCLE_TABLE", "SUBSCRIPTION_TABLE"]

_R = TransitionRule

SUBSCRIPTION_TABLE = TransitionTable(
    "Subscription",
    states=("ACTIVE", "PAST_DUE", "PAUSED", "CANCELLED", "COMPLETED"),
    terminal_states=("CANCELLED", "COMPLETED"),
    rules=(
        _R(
            "ACTIVE",
            "cycle_failed_final",
            "PAST_DUE",
            side_effects=("aud", "event:subscription.past_due", "notification"),
        ),
        _R("ACTIVE", "pause", "PAUSED", side_effects=("aud", "event:subscription.paused")),
        _R(
            "ACTIVE",
            "cancel",
            "CANCELLED",
            side_effects=("aud", "event:subscription.cancelled"),
        ),
        _R(
            "ACTIVE",
            "term_completed",
            "COMPLETED",
            side_effects=("aud", "event:subscription.completed"),
        ),
        _R(
            "PAST_DUE",
            "cycle_paid",
            "ACTIVE",
            side_effects=("aud", "event:subscription.reactivated"),
        ),
        _R(
            "PAST_DUE",
            "grace_expired",
            "CANCELLED",
            side_effects=("aud", "event:subscription.cancelled"),
        ),
        _R("PAUSED", "resume", "ACTIVE", side_effects=("aud", "event:subscription.resumed")),
        _R(
            "PAUSED",
            "cancel",
            "CANCELLED",
            side_effects=("aud", "event:subscription.cancelled"),
        ),
    ),
)

SUBSCRIPTION_CYCLE_TABLE = TransitionTable(
    "SubscriptionCycle",
    states=("SCHEDULED", "COLLECTING", "DUNNING", "PAID", "FAILED", "SKIPPED", "CANCELLED"),
    terminal_states=("PAID", "FAILED", "SKIPPED", "CANCELLED"),
    rules=(
        _R(
            "SCHEDULED",
            "cycle_due",
            "COLLECTING",
            side_effects=("aud", "event:subscription_cycle.invoiced"),
        ),
        _R(
            "SCHEDULED",
            "subscription_paused_or_cancelled",
            "SKIPPED",
            side_effects=("aud", "event:subscription_cycle.skipped"),
        ),
        _R(
            "COLLECTING",
            "intent_succeeded",
            "PAID",
            side_effects=("aud", "event:subscription_cycle.paid"),
        ),
        _R(
            "COLLECTING",
            "intent_failed_or_expired",
            "DUNNING",
            side_effects=("aud", "event:subscription_cycle.dunning_started"),
        ),
        _R(
            "DUNNING",
            "dunning_attempt",
            "DUNNING",
            side_effects=("aud", "event:subscription_cycle.invoiced"),
        ),
        _R(
            "DUNNING",
            "attempt_succeeded",
            "PAID",
            side_effects=("aud", "event:subscription_cycle.paid"),
        ),
        _R(
            "DUNNING",
            "schedule_exhausted",
            "FAILED",
            side_effects=("aud", "event:subscription_cycle.failed"),
        ),
        _R("COLLECTING", "subscription_cancelled", "CANCELLED", side_effects=("aud",)),
        _R("DUNNING", "subscription_cancelled", "CANCELLED", side_effects=("aud",)),
    ),
)

MANDATE_TABLE = TransitionTable(
    "Mandate",
    states=("PENDING_CONFIRMATION", "ACTIVE", "SUSPENDED", "REVOKED", "EXPIRED"),
    terminal_states=("REVOKED", "EXPIRED"),
    rules=(
        _R(
            "PENDING_CONFIRMATION",
            "confirmed",
            "ACTIVE",
            side_effects=("aud", "event:mandate.activated"),
        ),
        _R(
            "PENDING_CONFIRMATION",
            "ttl_expired",
            "EXPIRED",
            side_effects=("aud", "event:mandate.expired"),
        ),
        _R("ACTIVE", "revoke", "REVOKED", side_effects=("aud", "event:mandate.revoked")),
        _R(
            "ACTIVE",
            "instrument_invalid",
            "SUSPENDED",
            side_effects=("aud", "event:mandate.suspended"),
        ),
        _R(
            "SUSPENDED",
            "re_confirmed",
            "ACTIVE",
            side_effects=("aud", "event:mandate.activated"),
        ),
        _R(
            "SUSPENDED",
            "ttl_expired",
            "REVOKED",
            side_effects=("aud", "event:mandate.revoked"),
        ),
    ),
)

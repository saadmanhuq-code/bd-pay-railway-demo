"""Offer + OfferRedemption transition tables (spec/18 §State machines, binding).

Both tables are transcribed row by row into the kernel's refusal-first
:class:`~bdpay.kernel.fsm.TransitionTable` engine: any (state, trigger) pair
not declared here is DENIED, and terminal states refuse every trigger.

Guards that need data the engine cannot see (merchant ACTIVE, ``now``
against ``valid_until``, outstanding RESERVED redemptions, cap totals) are
enforced by :mod:`bdpay.kernel.offers.service` /
:mod:`bdpay.kernel.offers.consumer` BEFORE asking the table to resolve — the
table remains the single transition arbiter.
"""

from __future__ import annotations

from bdpay.kernel.fsm import TransitionRule, TransitionTable

__all__ = ["OFFER_STATES", "OFFER_TABLE", "REDEMPTION_STATES", "REDEMPTION_TABLE"]

_R = TransitionRule

# ---------------------------------------------------------------------------
# FSM 1: Offer (spec/18)
# ---------------------------------------------------------------------------

OFFER_STATES: tuple[str, ...] = (
    "DRAFT",
    "ACTIVE",
    "PAUSED",
    "EXHAUSTED",
    "EXPIRED",
    "ARCHIVED",
)

OFFER_TABLE = TransitionTable(
    "Offer",
    states=OFFER_STATES,
    terminal_states=("EXPIRED", "ARCHIVED"),
    rules=(
        _R(
            "DRAFT",
            "activate",
            "ACTIVE",
            side_effects=("aud", "event:offer.activated"),
        ),
        _R("DRAFT", "archive", "ARCHIVED", side_effects=("aud", "event:offer.archived")),
        _R("ACTIVE", "pause", "PAUSED", side_effects=("aud", "event:offer.paused")),
        _R("PAUSED", "resume", "ACTIVE", side_effects=("aud", "event:offer.resumed")),
        _R(
            "ACTIVE",
            "cap_total_reached",
            "EXHAUSTED",
            side_effects=("aud", "event:offer.exhausted"),
        ),
        _R(
            "EXHAUSTED",
            "cap_raised",
            "ACTIVE",
            side_effects=("aud", "version_row", "event:offer.resumed"),
        ),
        _R("ACTIVE", "ttl_expired", "EXPIRED", side_effects=("aud", "event:offer.expired")),
        _R("PAUSED", "ttl_expired", "EXPIRED", side_effects=("aud", "event:offer.expired")),
        # ACTIVE/PAUSED/EXHAUSTED --archive--> ARCHIVED, guard: no RESERVED
        # redemptions outstanding (service-enforced before resolve).
        _R("ACTIVE", "archive", "ARCHIVED", side_effects=("aud", "event:offer.archived")),
        _R("PAUSED", "archive", "ARCHIVED", side_effects=("aud", "event:offer.archived")),
        _R("EXHAUSTED", "archive", "ARCHIVED", side_effects=("aud", "event:offer.archived")),
        # any non-terminal --merchant_suspended--> PAUSED (spec/08 merchant
        # leaves ACTIVE); PAUSED -> PAUSED is a declared self-loop.
        _R("DRAFT", "merchant_suspended", "PAUSED", side_effects=("aud", "event:offer.paused")),
        _R("ACTIVE", "merchant_suspended", "PAUSED", side_effects=("aud", "event:offer.paused")),
        _R("PAUSED", "merchant_suspended", "PAUSED", side_effects=("aud", "event:offer.paused")),
        _R(
            "EXHAUSTED",
            "merchant_suspended",
            "PAUSED",
            side_effects=("aud", "event:offer.paused"),
        ),
    ),
)

# ---------------------------------------------------------------------------
# FSM 2: OfferRedemption (spec/18)
# ---------------------------------------------------------------------------

REDEMPTION_STATES: tuple[str, ...] = (
    "RESERVED",
    "APPLIED",
    "RELEASED",
    "REVERSED",
    "SETTLED",
)

REDEMPTION_TABLE = TransitionTable(
    "OfferRedemption",
    states=REDEMPTION_STATES,
    terminal_states=("RELEASED", "REVERSED", "SETTLED"),
    rules=(
        _R(
            "RESERVED",
            "intent_succeeded",
            "APPLIED",
            side_effects=(
                "je:offer_commission_collected",  # iff commission configured
                "je:offer_subsidy_granted",  # iff subsidy configured
                "aud",
                "event:offer_redemption.applied",
            ),
        ),
        _R(
            "RESERVED",
            "intent_failed",
            "RELEASED",
            side_effects=("counter_recredit", "aud", "event:offer_redemption.released"),
        ),
        _R(
            "RESERVED",
            "reservation_ttl_expired",
            "RELEASED",
            side_effects=("counter_recredit", "aud", "event:offer_redemption.released"),
        ),
        _R(
            "APPLIED",
            "intent_reversed_or_refunded_full",
            "REVERSED",
            side_effects=(
                "je:offer_commission_reversed",  # offsetting, iff commission was posted
                "je:offer_subsidy_reversed",  # offsetting, iff subsidy was granted
                "counter_recredit_iff_cap_recredit_on_refund",
                "aud",
                "event:offer_redemption.reversed",
            ),
        ),
        _R(
            "APPLIED",
            "settlement_confirmed",
            "SETTLED",
            side_effects=("aud", "event:offer_redemption.settled"),
        ),
    ),
)

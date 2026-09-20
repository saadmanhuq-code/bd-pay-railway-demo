"""PaymentIntent / PaymentAttempt / Refund transition tables (spec/02, binding).

Each table is the spec/02 §State machines transition table transcribed row by
row into :class:`~bdpay.kernel.fsm.TransitionTable`. Refusal-first: anything
not declared here is denied by the engine.

Documented divergences (all invariant-preserving, errata
SPEC_ERRATA-LANE-A-kernel.md, pinned by tests/kernel/):

- K-3: spec/02 marks ``SUCCEEDED`` / ``CHARGED`` "(terminal)" while the same
  tables declare refund/reversal transitions out of them. The engine's
  terminal sets exclude them; their immutability is preserved by
  refusal-first (the ONLY trigger out of ``SUCCEEDED`` is ``refund_initiated``;
  the only one out of ``CHARGED`` is ``reversal_submitted``) and by the rule
  that refunds are new offsetting flows — the original charge row and its
  journal entry are never mutated.
- K-4: the spec/02 API allows ``POST .../cancel`` for ``CREATED``,
  ``REQUIRES_PAYMENT_METHOD`` and ``REQUIRES_CONFIRMATION`` but the FSM table
  declares no such trigger. Added as the declared ``cancel_requested`` rows.
- K-5: ``REFUND_INITIATED --refund_failed--> SUCCEEDED`` would erase the
  PARTIALLY_REFUNDED standing of an intent with prior partial refunds. Added
  the guarded branch returning to ``PARTIALLY_REFUNDED`` when prior refunds
  exist.
"""

from __future__ import annotations

from bdpay.kernel.fsm import TransitionRule, TransitionTable

__all__ = [
    "ATTEMPT_TABLE",
    "CAPTURE_METHODS",
    "INTENT_TABLE",
    "PAYMENT_METHODS",
    "REFUND_REASONS",
    "REFUND_TABLE",
]

#: spec/02 ``payment_method_type`` enum.
PAYMENT_METHODS: tuple[str, ...] = (
    "NPSB_IBFT",
    "BEFTN_CREDIT",
    "BKASH",
    "NAGAD",
    "ROCKET",
    "CARD",
    "BANGLA_QR",
)

CAPTURE_METHODS: tuple[str, ...] = ("automatic", "manual")

#: spec/02 ``refund_reason`` enum.
REFUND_REASONS: tuple[str, ...] = (
    "customer_request",
    "duplicate",
    "fraudulent",
    "merchant_initiated",
)

_R = TransitionRule

# ---------------------------------------------------------------------------
# FSM 1: PaymentIntent (spec/02)
# ---------------------------------------------------------------------------

INTENT_STATES = (
    "CREATED",
    "REQUIRES_PAYMENT_METHOD",
    "REQUIRES_CONFIRMATION",
    "PRE_FLIGHT",
    "REQUIRES_ACTION",
    "PROCESSING",
    "REQUIRES_CAPTURE",
    "SUCCEEDED",
    "FAILED",
    "CANCELLED",
    "REVERSAL_INITIATED",
    "REVERSAL_PENDING",
    "REVERSED",
    "REFUND_INITIATED",
    "PARTIALLY_REFUNDED",
    "REFUNDED",
)

INTENT_TABLE = TransitionTable(
    "PaymentIntent",
    states=INTENT_STATES,
    # Spec terminal set is {SUCCEEDED, FAILED, CANCELLED, REVERSED, REFUNDED};
    # SUCCEEDED is refund-capable per the same table (errata K-3).
    terminal_states=("FAILED", "CANCELLED", "REVERSED", "REFUNDED"),
    rules=(
        _R("CREATED", "method_supplied", "REQUIRES_PAYMENT_METHOD", side_effects=("aud",)),
        _R("CREATED", "confirm", "PRE_FLIGHT", side_effects=("aud",)),
        _R(
            "CREATED",
            "ttl_expired",
            "CANCELLED",
            side_effects=("aud", "event:payment_intent.expired"),
        ),
        _R("CREATED", "cancel_requested", "CANCELLED", side_effects=("aud",)),  # K-4
        _R("REQUIRES_PAYMENT_METHOD", "method_supplied", "REQUIRES_CONFIRMATION"),
        _R(
            "REQUIRES_PAYMENT_METHOD",
            "ttl_expired",
            "CANCELLED",
            side_effects=("aud", "event:payment_intent.expired"),
        ),
        _R("REQUIRES_PAYMENT_METHOD", "cancel_requested", "CANCELLED"),  # K-4
        _R("REQUIRES_CONFIRMATION", "confirm", "PRE_FLIGHT"),
        _R(
            "REQUIRES_CONFIRMATION",
            "ttl_expired",
            "CANCELLED",
            side_effects=("aud", "event:payment_intent.expired"),
        ),
        _R("REQUIRES_CONFIRMATION", "cancel_requested", "CANCELLED"),  # K-4
        _R(
            "PRE_FLIGHT",
            "preflight_passed",
            "PROCESSING",
            side_effects=("aud", "event:payment_intent.processing"),
        ),
        _R("PRE_FLIGHT", "action_required", "REQUIRES_ACTION"),
        _R(
            "PRE_FLIGHT",
            "sanctions_hit",
            "FAILED",
            side_effects=("aud:SANCTIONS_BLOCK", "event:payment_intent.failed"),
        ),
        _R(
            "PRE_FLIGHT",
            "aml_block",
            "FAILED",
            side_effects=("aud:AML_BLOCK", "event:payment_intent.failed"),
        ),
        _R(
            "PRE_FLIGHT",
            "limit_exceeded",
            "FAILED",
            side_effects=("aud", "event:payment_intent.failed"),
        ),
        _R(
            "REQUIRES_ACTION",
            "action_completed",
            "PROCESSING",
            side_effects=("aud", "event:payment_intent.processing"),
        ),
        _R(
            "REQUIRES_ACTION",
            "action_failed",
            "FAILED",
            side_effects=("aud", "event:payment_intent.failed"),
        ),
        _R(
            "REQUIRES_ACTION",
            "ttl_expired",
            "FAILED",
            side_effects=("aud", "event:payment_intent.failed"),
        ),
        _R(
            "PROCESSING",
            "attempt_authorized",
            "REQUIRES_CAPTURE",
            side_effects=("aud", "event:payment_attempt.authorized"),
        ),
        _R(
            "PROCESSING",
            "attempt_charged",
            "SUCCEEDED",
            side_effects=(
                "je:payment_captured",
                "je:fee_collected",
                "event:payment_attempt.charged",
                "event:payment_intent.succeeded",
            ),
        ),
        _R("PROCESSING", "attempt_failed_retryable", "PROCESSING", side_effects=("aud",)),
        _R(
            "PROCESSING",
            "attempt_hard_declined",
            "FAILED",
            side_effects=("aud", "event:payment_intent.failed"),
        ),
        _R("PROCESSING", "attempt_timed_out", "PROCESSING", side_effects=("aud",)),
        _R(
            "PROCESSING",
            "reversal_triggered",
            "REVERSAL_INITIATED",
            side_effects=("aud", "event:payment_intent.reversal_initiated"),
        ),
        _R(
            "PROCESSING",
            "ttl_expired",
            "REVERSAL_INITIATED",
            side_effects=("aud", "event:payment_intent.reversal_initiated"),
        ),
        _R(
            "REQUIRES_CAPTURE",
            "capture_requested",
            "SUCCEEDED",
            side_effects=(
                "je:payment_captured",
                "je:fee_collected",
                "event:payment_attempt.charged",
                "event:payment_intent.succeeded",
            ),
        ),
        _R(
            "REQUIRES_CAPTURE",
            "void_requested",
            "CANCELLED",
            side_effects=("aud:VOID_MARKER", "event:payment_intent.cancelled"),
        ),
        _R("REQUIRES_CAPTURE", "ttl_expired", "REVERSAL_INITIATED", side_effects=("aud",)),
        _R(
            "REVERSAL_INITIATED",
            "reversal_dispatch_claimed",
            "REVERSAL_PENDING",
            side_effects=("aud",),
        ),
        _R(
            "REVERSAL_INITIATED",
            "reversal_confirmed",
            "REVERSED",
            side_effects=("je:payment_reversed", "event:payment_intent.reversed"),
        ),
        _R(
            "REVERSAL_INITIATED",
            "reversal_failed",
            "FAILED",
            side_effects=("aud:REVERSAL_FAILED", "event:compensation_item.queued"),
        ),
        _R(
            "REVERSAL_PENDING",
            "reversal_dispatch_claimed",
            "REVERSAL_PENDING",
            side_effects=("aud",),
        ),
        _R(
            "REVERSAL_PENDING",
            "reversal_confirmed",
            "REVERSED",
            side_effects=("je:payment_reversed", "event:payment_intent.reversed"),
        ),
        _R(
            "REVERSAL_PENDING",
            "reversal_failed",
            "FAILED",
            side_effects=("aud:REVERSAL_FAILED", "event:compensation_item.queued"),
        ),
        _R(
            "SUCCEEDED",
            "refund_initiated",
            "REFUND_INITIATED",
            side_effects=("event:refund.initiated",),
        ),
        _R(
            "REFUND_INITIATED",
            "refund_partial_succeeded",
            "PARTIALLY_REFUNDED",
            side_effects=("je:refund_settled",),
        ),
        _R(
            "REFUND_INITIATED",
            "refund_full_succeeded",
            "REFUNDED",
            side_effects=("je:refund_settled", "event:refund.succeeded"),
        ),
        _R(
            "REFUND_INITIATED",
            "refund_failed",
            "SUCCEEDED",
            guard="no_prior_partial",
            side_effects=("aud:REFUND_FAILED",),
        ),
        _R(  # errata K-5
            "REFUND_INITIATED",
            "refund_failed",
            "PARTIALLY_REFUNDED",
            guard="prior_partial",
            side_effects=("aud:REFUND_FAILED",),
        ),
        _R(
            "PARTIALLY_REFUNDED",
            "refund_initiated",
            "REFUND_INITIATED",
            side_effects=("event:refund.initiated",),
        ),
        _R(
            "PARTIALLY_REFUNDED",
            "refund_full_succeeded",
            "REFUNDED",
            side_effects=("je:refund_settled", "event:refund.succeeded"),
        ),
    ),
)

# ---------------------------------------------------------------------------
# FSM 2: PaymentAttempt (spec/02)
# ---------------------------------------------------------------------------

ATTEMPT_STATES = (
    "STARTED",
    "AUTHENTICATION_PENDING",
    "AUTHENTICATION_FAILED",
    "AUTHORIZATION_PENDING",
    "AUTHORIZATION_FAILED",
    "TIMED_OUT",
    "AUTHORIZED",
    "CAPTURE_INITIATED",
    "CHARGED",
    "CAPTURE_FAILED",
    "VOIDED",
    "REVERSED",
)

ATTEMPT_TABLE = TransitionTable(
    "PaymentAttempt",
    states=ATTEMPT_STATES,
    # CHARGED is reversal-capable per the table (errata K-3).
    terminal_states=(
        "AUTHENTICATION_FAILED",
        "AUTHORIZATION_FAILED",
        "CAPTURE_FAILED",
        "VOIDED",
        "REVERSED",
    ),
    rules=(
        _R("STARTED", "authentication_required", "AUTHENTICATION_PENDING"),
        _R("STARTED", "submitted", "AUTHORIZATION_PENDING"),
        _R("AUTHENTICATION_PENDING", "auth_success", "AUTHORIZATION_PENDING"),
        _R(
            "AUTHENTICATION_PENDING",
            "auth_failure",
            "AUTHENTICATION_FAILED",
            side_effects=("event:payment_attempt.authentication_failed",),
        ),
        _R("AUTHENTICATION_PENDING", "auth_timeout", "AUTHENTICATION_FAILED"),
        _R(
            "AUTHORIZATION_PENDING",
            "connector_authorized",
            "AUTHORIZED",
            side_effects=("event:payment_attempt.authorized",),
        ),
        _R(
            "AUTHORIZATION_PENDING",
            "connector_success_direct",
            "CHARGED",
            side_effects=("je:payment_captured", "event:payment_attempt.charged"),
        ),
        _R("AUTHORIZATION_PENDING", "connector_failed", "AUTHORIZATION_FAILED"),
        _R(
            "AUTHORIZATION_PENDING",
            "connector_rejected",
            "AUTHORIZATION_FAILED",
            side_effects=("event:payment_attempt.hard_declined",),
        ),
        _R("AUTHORIZATION_PENDING", "connector_timeout", "TIMED_OUT"),
        _R("TIMED_OUT", "poll_success", "AUTHORIZED", guard="authorized"),
        _R(
            "TIMED_OUT",
            "poll_success",
            "CHARGED",
            guard="charged",
            side_effects=("je:payment_captured", "event:payment_attempt.charged"),
        ),
        _R("TIMED_OUT", "poll_pending", "TIMED_OUT"),
        _R("TIMED_OUT", "poll_failed", "AUTHORIZATION_FAILED"),
        _R("TIMED_OUT", "poll_exhausted", "AUTHORIZATION_FAILED"),
        _R("TIMED_OUT", "poll_reversed", "REVERSED"),
        _R("AUTHORIZED", "capture_submitted", "CAPTURE_INITIATED"),
        _R(
            "AUTHORIZED",
            "void_submitted",
            "VOIDED",
            side_effects=("event:payment_attempt.voided",),
        ),
        _R("AUTHORIZED", "auth_expired", "VOIDED"),
        _R(
            "CAPTURE_INITIATED",
            "capture_confirmed",
            "CHARGED",
            side_effects=("je:payment_captured", "event:payment_attempt.charged"),
        ),
        _R(
            "CAPTURE_INITIATED",
            "capture_failed",
            "CAPTURE_FAILED",
            side_effects=("event:payment_attempt.capture_failed",),
        ),
        _R(
            "CHARGED",
            "reversal_submitted",
            "REVERSED",
            side_effects=("je:payment_reversed", "event:payment_attempt.reversed"),
        ),
    ),
)

# ---------------------------------------------------------------------------
# FSM 3: Refund (spec/02)
# ---------------------------------------------------------------------------

REFUND_STATES = (
    "REFUND_INITIATED",
    "REFUND_PENDING_CONNECTOR",
    "REFUND_SUCCEEDED",
    "REFUND_FAILED",
)

REFUND_TABLE = TransitionTable(
    "Refund",
    states=REFUND_STATES,
    terminal_states=("REFUND_SUCCEEDED", "REFUND_FAILED"),
    rules=(
        _R("REFUND_INITIATED", "connector_dispatched", "REFUND_PENDING_CONNECTOR"),
        _R(
            "REFUND_INITIATED",
            "rail_not_supported",
            "REFUND_FAILED",
            side_effects=("aud:REFUND_RAIL_UNSUPPORTED",),
        ),
        _R(
            "REFUND_PENDING_CONNECTOR",
            "connector_success",
            "REFUND_SUCCEEDED",
            side_effects=("je:refund_settled", "event:refund.succeeded"),
        ),
        _R("REFUND_PENDING_CONNECTOR", "connector_failed", "REFUND_PENDING_CONNECTOR"),
        _R(
            "REFUND_PENDING_CONNECTOR",
            "connector_rejected",
            "REFUND_FAILED",
            side_effects=("event:refund.failed",),
        ),
        _R(
            "REFUND_PENDING_CONNECTOR",
            "retries_exhausted",
            "REFUND_FAILED",
            side_effects=("event:refund.failed",),
        ),
        _R("REFUND_PENDING_CONNECTOR", "connector_timeout", "REFUND_PENDING_CONNECTOR"),
    ),
)

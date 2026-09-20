"""Spec/17 VX4 DisbursementBatch and DisbursementItem FSM tables."""

from __future__ import annotations

from bdpay.kernel.fsm import TransitionRule, TransitionTable

__all__ = [
    "BATCH_STATES",
    "BATCH_TABLE",
    "BATCH_TERMINAL_STATES",
    "ITEM_STATES",
    "ITEM_TABLE",
    "ITEM_TERMINAL_STATES",
]

_R = TransitionRule

BATCH_STATES: tuple[str, ...] = (
    "DRAFT",
    "VALIDATED",
    "VALIDATION_FAILED",
    "PENDING_APPROVAL",
    "APPROVED",
    "DISPATCHING",
    "COMPLETED",
    "PARTIALLY_RETURNED",
    "CANCELLED",
)

BATCH_TERMINAL_STATES: tuple[str, ...] = (
    "VALIDATION_FAILED",
    "COMPLETED",
    "PARTIALLY_RETURNED",
    "CANCELLED",
)

BATCH_TABLE = TransitionTable(
    "DisbursementBatch",
    states=BATCH_STATES,
    terminal_states=BATCH_TERMINAL_STATES,
    rules=(
        _R(
            "DRAFT",
            "validated",
            "VALIDATED",
            side_effects=("aud", "event:disbursement_batch.validated"),
        ),
        _R(
            "DRAFT",
            "validation_failed",
            "VALIDATION_FAILED",
            side_effects=("aud", "per-row validation report"),
        ),
        _R(
            "VALIDATED",
            "submit",
            "PENDING_APPROVAL",
            side_effects=(
                "ApprovalRequest:bulk_disbursement_release",
                "aud",
                "event:disbursement_batch.submitted",
            ),
        ),
        _R(
            "PENDING_APPROVAL",
            "approved",
            "APPROVED",
            side_effects=("je:disbursement_funded", "aud", "event:disbursement_batch.approved"),
        ),
        _R(
            "PENDING_APPROVAL",
            "rejected_or_expired",
            "CANCELLED",
            side_effects=("aud", "event:disbursement_batch.cancelled"),
        ),
        _R(
            "VALIDATED",
            "cancel",
            "CANCELLED",
            side_effects=("aud", "event:disbursement_batch.cancelled"),
        ),
        _R(
            "PENDING_APPROVAL",
            "cancel",
            "CANCELLED",
            side_effects=("aud", "event:disbursement_batch.cancelled"),
        ),
        _R(
            "APPROVED",
            "dispatch",
            "DISPATCHING",
            side_effects=("aud", "event:disbursement_batch.dispatched"),
        ),
        _R(
            "DISPATCHING",
            "all_items_terminal",
            "COMPLETED",
            guard="all_paid",
            side_effects=("aud", "event:disbursement_batch.completed"),
        ),
        _R(
            "DISPATCHING",
            "all_items_terminal",
            "PARTIALLY_RETURNED",
            guard="any_returned_or_failed",
            side_effects=(
                "hold release",
                "aud",
                "event:disbursement_batch.completed",
            ),
        ),
    ),
)

ITEM_STATES: tuple[str, ...] = (
    "QUEUED",
    "DISPATCHING",
    "DISPATCHED",
    "PAID",
    "RETURNED",
    "FAILED",
)
ITEM_TERMINAL_STATES: tuple[str, ...] = ("PAID", "RETURNED", "FAILED")

ITEM_TABLE = TransitionTable(
    "DisbursementItem",
    states=ITEM_STATES,
    terminal_states=ITEM_TERMINAL_STATES,
    rules=(
        _R("QUEUED", "dispatched", "DISPATCHED", side_effects=("aud",)),
        # Pre-rail sanctions screening block (audit P0 cab6a60): a QUEUED item
        # whose recipient hits the watchlist at dispatch is failed terminally
        # before any rail handoff; the funded hold-reserve share is released
        # via the 0155 disbursement_failed_release vocabulary.
        _R(
            "QUEUED",
            "failed",
            "FAILED",
            side_effects=(
                "je:disbursement_failed_release",
                "aud",
                "event:disbursement_item.failed",
            ),
        ),
        _R("DISPATCHING", "dispatched", "DISPATCHED", side_effects=("aud",)),
        _R(
            "DISPATCHING",
            "settled",
            "PAID",
            side_effects=("je:disbursement_item_paid", "aud", "event:disbursement_item.paid"),
        ),
        _R(
            "DISPATCHING",
            "returned",
            "RETURNED",
            side_effects=(
                "je:disbursement_item_returned",
                "aud",
                "event:disbursement_item.returned",
            ),
        ),
        _R(
            "DISPATCHING",
            "failed",
            "FAILED",
            side_effects=(
                "je:disbursement_failed_release",
                "aud",
                "event:disbursement_item.failed",
            ),
        ),
        _R(
            "DISPATCHED",
            "settled",
            "PAID",
            side_effects=("je:disbursement_item_paid", "aud", "event:disbursement_item.paid"),
        ),
        _R(
            "DISPATCHED",
            "returned",
            "RETURNED",
            side_effects=(
                "je:disbursement_item_returned",
                "aud",
                "event:disbursement_item.returned",
            ),
        ),
        _R(
            "DISPATCHED",
            "failed",
            "FAILED",
            side_effects=(
                "je:disbursement_failed_release",
                "aud",
                "event:disbursement_item.failed",
            ),
        ),
    ),
)

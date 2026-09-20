"""OfferRedemption FSM event consumer (spec/18 §Events produced/consumed).

Consumes ``payment.events`` / ``settlement.events`` envelopes and drives the
OfferRedemption FSM. Binding properties:

- **Idempotent on event_id** — a durable dedupe row guards every envelope;
  replaying the journal reproduces identical terminal states and counter
  values (test gate 7, replay determinism).
- **Refusal-first on FSM state** — an envelope whose (state, trigger) pair
  is not declared is a structural no-op for the redemption (the payment FSM
  has zero dependencies on redemption FSM progress, so the consumer never
  throws back at the bus).
- **Counter re-credit** runs in the same transaction as the redemption
  transition; ``day``-scope re-credit only on the same Asia/Dhaka calendar
  day as the reservation (guard in the SQL on the counter's ``scope_key``);
  ``total``/``customer`` scopes always re-credit. On ``REVERSED`` the whole
  re-credit happens iff ``cap_recredit_on_refund`` (merchant anti-abuse
  option).
- **Money legs** post from the STORED ``commission_minor``/``subsidy_minor``
  (computed once at reservation — no recomputation drift). The commission
  leg exists iff the redemption is APPLIED; the offsetting leg posts with
  REVERSED (errata S18-E11 records the consumer-transaction placement).
- Partial refunds accumulate on ``refunded_minor`` and do NOT transition
  the redemption; reaching ``net_amount_minor`` is the full-refund trigger.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import Any

from bdpay.kernel.offers.eligibility import dhaka_date_str
from bdpay.kernel.offers.ids_ext import make_offer_id
from bdpay.kernel.offers.ledger_legs import (
    PRODUCER,
    commission_collected_spec,
    commission_reversed_spec,
    subsidy_granted_spec,
    subsidy_reversed_spec,
)
from bdpay.kernel.offers.models import OfferRedemptionRecord
from bdpay.kernel.offers.repository import OfferStore
from bdpay.kernel.offers.service import OFFER_EVENTS_TOPIC
from bdpay.kernel.offers.states import REDEMPTION_TABLE
from bdpay.platform.clock import Clock
from bdpay.platform.errors import InvalidRequestError
from bdpay.platform.interfaces import AuditEventSpec, AuditPort, LedgerPort, OutboxPort
from bdpay.platform.outbox import build_event

__all__ = ["CONSUMED_EVENT_TYPES", "OfferEventConsumer"]

#: spec/18 §Events consumed (from payment.events / settlement.events).
CONSUMED_EVENT_TYPES: tuple[str, ...] = (
    "payment_intent.succeeded",
    "payment_intent.failed",
    "payment_intent.cancelled",
    "payment_intent.expired",
    "payment_intent.reversed",
    "refund.succeeded",
    "settlement.confirmed",
)

#: Intent statuses that count as terminal-success for the reservation sweep
#: guard ("intent not terminal-success" — the success event will APPLY it).
_SUCCESS_FAMILY = ("SUCCEEDED", "REFUND_INITIATED", "PARTIALLY_REFUNDED", "REFUNDED")


class OfferEventConsumer:
    """Drives RESERVED -> APPLIED/RELEASED -> REVERSED/SETTLED from the bus."""

    def __init__(
        self,
        *,
        store: OfferStore,
        ledger: LedgerPort,
        audit: AuditPort,
        outbox: OutboxPort,
        clock: Clock,
        intent_status: Callable[[str], str | None] | None = None,
    ) -> None:
        self._store = store
        self._ledger = ledger
        self._audit = audit
        self._outbox = outbox
        self._clock = clock
        self._intent_status = intent_status

    # ------------------------------------------------------------------
    # envelope intake
    # ------------------------------------------------------------------

    def handle(self, envelope: Mapping[str, object], *, conn: Any | None = None) -> bool:
        """Consume one spec/00 §5 envelope. Returns True when work was done.

        Duplicates (same ``event_id``) and envelopes that do not apply to a
        redemption are no-ops returning False — consumption never raises at
        the bus boundary for ordinary FSM non-applicability.
        """
        event_id = envelope.get("event_id")
        event_type = envelope.get("type")
        if not isinstance(event_id, str) or not event_id:
            raise InvalidRequestError("envelope is missing event_id")
        if event_type not in CONSUMED_EVENT_TYPES:
            return False
        now = self._clock.now()
        if not self._store.mark_event_consumed(event_id, now=now, conn=conn):
            return False  # idempotent replay: already consumed
        payload = envelope.get("payload")
        payload = payload if isinstance(payload, Mapping) else {}

        if event_type == "payment_intent.succeeded":
            return self._on_intent_succeeded(payload, conn=conn)
        if event_type in (
            "payment_intent.failed",
            "payment_intent.cancelled",
            "payment_intent.expired",
        ):
            return self._on_intent_dead(payload, conn=conn)
        if event_type == "payment_intent.reversed":
            return self._on_intent_reversed(payload, conn=conn)
        if event_type == "refund.succeeded":
            return self._on_refund_succeeded(payload, conn=conn)
        return self._on_settlement_confirmed(payload, conn=conn)

    # ------------------------------------------------------------------
    # scheduler backstop (offer-reservation-sweep, 60 s)
    # ------------------------------------------------------------------

    def sweep_expired_reservations(self, *, conn: Any | None = None) -> int:
        """RESERVED past ``reserved_expires_at`` AND intent not terminal-success
        -> RELEASED (defense-in-depth behind the event-driven release)."""
        now = self._clock.now()
        moved = 0
        for redemption in self._store.list_expired_reservations(now=now, conn=conn):
            if self._intent_status is not None:
                status = self._intent_status(redemption.payment_intent_id)
                if status in _SUCCESS_FAMILY:
                    continue  # the success event applies it; never release a paid slot
            self._release(redemption, trigger="reservation_ttl_expired", conn=conn)
            moved += 1
        return moved

    # ------------------------------------------------------------------
    # transitions
    # ------------------------------------------------------------------

    def _redemption_for(
        self, payload: Mapping[str, object], *, conn: Any | None
    ) -> OfferRedemptionRecord | None:
        intent_id = payload.get("payment_intent_id")
        if not isinstance(intent_id, str) or not intent_id:
            return None
        return self._store.get_redemption_by_intent(intent_id, conn=conn)

    def _on_intent_succeeded(
        self, payload: Mapping[str, object], *, conn: Any | None
    ) -> bool:
        redemption = self._redemption_for(payload, conn=conn)
        if redemption is None or not REDEMPTION_TABLE.rules_for(
            redemption.state, "intent_succeeded"
        ):
            return False  # zero-offer intent or refusal-first non-applicability
        now = self._clock.now()
        if redemption.commission_minor > 0:
            self._ledger.post_journal_entry(
                commission_collected_spec(redemption), clock=self._clock, conn=conn
            )
        if redemption.subsidy_minor > 0:
            self._ledger.post_journal_entry(
                subsidy_granted_spec(redemption), clock=self._clock, conn=conn
            )
        updated = self._advance(redemption, "intent_succeeded", resolved_at=None, conn=conn)
        self._emit(
            "offer_redemption.applied",
            updated,
            {
                "redemption_id": updated.redemption_id,
                "offer_id": updated.offer_id,
                "payment_intent_id": updated.payment_intent_id,
                "discount_minor": updated.discount_minor,
                "net_amount_minor": updated.net_amount_minor,
                "commission_minor": updated.commission_minor,
                "subsidy_minor": updated.subsidy_minor,
                "applied_at": now,
            },
            conn=conn,
        )
        return True

    def _on_intent_dead(self, payload: Mapping[str, object], *, conn: Any | None) -> bool:
        redemption = self._redemption_for(payload, conn=conn)
        if redemption is None or not REDEMPTION_TABLE.rules_for(
            redemption.state, "intent_failed"
        ):
            return False
        self._release(redemption, trigger="intent_failed", conn=conn)
        return True

    def _on_intent_reversed(
        self, payload: Mapping[str, object], *, conn: Any | None
    ) -> bool:
        redemption = self._redemption_for(payload, conn=conn)
        if redemption is None or not REDEMPTION_TABLE.rules_for(
            redemption.state, "intent_reversed_or_refunded_full"
        ):
            # A RESERVED redemption has no declared row for this trigger:
            # the reservation sweep is the backstop (refusal-first no-op).
            return False
        self._reverse(redemption, conn=conn)
        return True

    def _on_refund_succeeded(
        self, payload: Mapping[str, object], *, conn: Any | None
    ) -> bool:
        redemption = self._redemption_for(payload, conn=conn)
        if redemption is None:
            return False
        amount = payload.get("amount_minor")
        if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
            return False
        # Partial refunds accumulate and do NOT transition (spec/18 binding).
        accumulated = replace(
            redemption, refunded_minor=redemption.refunded_minor + amount
        )
        self._store.update_redemption(accumulated, conn=conn)
        if accumulated.refunded_minor < accumulated.net_amount_minor:
            return True
        if not REDEMPTION_TABLE.rules_for(
            accumulated.state, "intent_reversed_or_refunded_full"
        ):
            return False
        self._reverse(accumulated, conn=conn)
        return True

    def _on_settlement_confirmed(
        self, payload: Mapping[str, object], *, conn: Any | None
    ) -> bool:
        intent_ids = payload.get("payment_intent_ids")
        if not isinstance(intent_ids, list | tuple):
            single = payload.get("payment_intent_id")
            intent_ids = [single] if isinstance(single, str) else []
        moved = False
        for intent_id in intent_ids:
            if not isinstance(intent_id, str):
                continue
            redemption = self._store.get_redemption_by_intent(intent_id, conn=conn)
            if redemption is None or not REDEMPTION_TABLE.rules_for(
                redemption.state, "settlement_confirmed"
            ):
                continue
            now = self._clock.now()
            updated = self._advance(
                redemption, "settlement_confirmed", resolved_at=now, conn=conn
            )
            self._emit(
                "offer_redemption.settled",
                updated,
                {
                    "redemption_id": updated.redemption_id,
                    "offer_id": updated.offer_id,
                    "payment_intent_id": updated.payment_intent_id,
                },
                conn=conn,
            )
            moved = True
        return moved

    # ------------------------------------------------------------------
    # release / reverse with counter re-credit
    # ------------------------------------------------------------------

    def _counter_id(self, redemption: OfferRedemptionRecord, scope: str, scope_key: str) -> str:
        return make_offer_id(
            "octr",
            {"offer_id": redemption.offer_id, "scope": scope, "scope_key": scope_key},
        )

    def _recredit_scopes(
        self,
        redemption: OfferRedemptionRecord,
        *,
        include_day: bool,
        conn: Any | None,
    ) -> None:
        """Re-credit total + customer always; day only same-Dhaka-day (in-SQL guard)."""
        now = self._clock.now()
        offer = self._store.get_offer(redemption.offer_id, conn=conn)
        if offer is None:
            return
        if offer.cap_total is not None:
            self._store.recredit_counter(
                self._counter_id(redemption, "total", "ALL"), now=now, conn=conn
            )
        if offer.cap_per_customer is not None:
            self._store.recredit_counter(
                self._counter_id(redemption, "customer", redemption.customer_id),
                now=now,
                conn=conn,
            )
        if include_day and offer.cap_per_day is not None:
            reservation_day = dhaka_date_str(redemption.reserved_at)
            today = dhaka_date_str(now)
            if reservation_day == today:
                self._store.recredit_counter(
                    self._counter_id(redemption, "day", reservation_day),
                    now=now,
                    require_scope_key=today,
                    conn=conn,
                )

    def _release(
        self, redemption: OfferRedemptionRecord, *, trigger: str, conn: Any | None
    ) -> None:
        now = self._clock.now()
        updated = self._advance(redemption, trigger, resolved_at=now, conn=conn)
        self._recredit_scopes(updated, include_day=True, conn=conn)
        self._emit(
            "offer_redemption.released",
            updated,
            {
                "redemption_id": updated.redemption_id,
                "offer_id": updated.offer_id,
                "payment_intent_id": updated.payment_intent_id,
                "trigger": trigger,
            },
            conn=conn,
        )

    def _reverse(self, redemption: OfferRedemptionRecord, *, conn: Any | None) -> None:
        now = self._clock.now()
        if redemption.commission_minor > 0:
            self._ledger.post_journal_entry(
                commission_reversed_spec(redemption), clock=self._clock, conn=conn
            )
        if redemption.subsidy_minor > 0:
            self._ledger.post_journal_entry(
                subsidy_reversed_spec(redemption), clock=self._clock, conn=conn
            )
        updated = self._advance(
            redemption, "intent_reversed_or_refunded_full", resolved_at=now, conn=conn
        )
        offer = self._store.get_offer(redemption.offer_id, conn=conn)
        if offer is not None and offer.cap_recredit_on_refund:
            self._recredit_scopes(updated, include_day=True, conn=conn)
        self._emit(
            "offer_redemption.reversed",
            updated,
            {
                "redemption_id": updated.redemption_id,
                "offer_id": updated.offer_id,
                "payment_intent_id": updated.payment_intent_id,
                "refunded_minor": updated.refunded_minor,
            },
            conn=conn,
        )

    # ------------------------------------------------------------------
    # shared internals
    # ------------------------------------------------------------------

    def _advance(
        self,
        redemption: OfferRedemptionRecord,
        trigger: str,
        *,
        resolved_at,
        conn: Any | None,
    ) -> OfferRedemptionRecord:
        rule = REDEMPTION_TABLE.resolve(redemption.state, trigger)
        updated = replace(
            redemption,
            state=rule.to_state,
            resolved_at=resolved_at if resolved_at is not None else redemption.resolved_at,
        )
        self._store.update_redemption(updated, conn=conn)
        self._audit.append(
            AuditEventSpec(
                event_type="OFFER_REDEMPTION_STATE_TRANSITION",
                actor_id=PRODUCER,
                subject_type="OfferRedemption",
                subject_id=updated.redemption_id,
                from_state=redemption.state,
                to_state=rule.to_state,
                payload={"trigger": trigger, "offer_id": updated.offer_id},
            ),
            clock=self._clock,
            conn=conn,
        )
        return updated

    def _emit(
        self,
        event_type: str,
        redemption: OfferRedemptionRecord,
        payload: dict[str, object],
        *,
        conn: Any | None,
    ) -> None:
        self._outbox.enqueue(
            build_event(
                event_type=event_type,
                subject_type="OfferRedemption",
                subject_id=redemption.redemption_id,
                producer=PRODUCER,
                topic=OFFER_EVENTS_TOPIC,
                payload=payload,
                occurred_at=self._clock.now(),
            ),
            conn=conn,
        )

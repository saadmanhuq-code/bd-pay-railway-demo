"""Subscription service: link-mode cycles, PaymentIntent materialization, dunning."""

from __future__ import annotations

import calendar as month_calendar
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Protocol, runtime_checkable

from bdpay.kernel.orchestrator import PaymentIntentRequest
from bdpay.kernel.subscriptions.ids_ext import make_subscription_id
from bdpay.kernel.subscriptions.repository import (
    DunningAttemptRecord,
    SubscriptionCycleRecord,
    SubscriptionRecord,
    SubscriptionStore,
)
from bdpay.kernel.subscriptions.states import SUBSCRIPTION_CYCLE_TABLE, SUBSCRIPTION_TABLE
from bdpay.platform.clock import Clock
from bdpay.platform.errors import ConflictError, InvalidRequestError, NotFoundError
from bdpay.platform.interfaces import AuditEventSpec, AuditPort, OutboxPort
from bdpay.platform.outbox import build_event
from bdpay.platform.pii import redact
from bdpay.platform.scheduler import DHAKA_TZ, BangladeshBankCalendar, ScheduledTask

__all__ = ["DunningConfig", "PaymentIntentCreator", "SubscriptionService"]

PRODUCER = "payment-orchestrator@1"
TOPIC = "payment.events"


@dataclass(frozen=True)
class DunningConfig:
    """Merchant-configurable dunning bounds, defaulting to spec/17 values."""

    schedule_days: tuple[int, ...] = (1, 3, 5)
    past_due_grace_days: int = 15

    def __post_init__(self) -> None:
        if not self.schedule_days:
            raise InvalidRequestError("schedule_days must be non-empty")
        previous = 0
        for day in self.schedule_days:
            if isinstance(day, bool) or not isinstance(day, int) or day <= previous:
                raise InvalidRequestError("schedule_days must be increasing positive ints")
            previous = day
        if self.past_due_grace_days < 1:
            raise InvalidRequestError("past_due_grace_days must be >= 1")


@runtime_checkable
class PaymentIntentCreator(Protocol):
    """Subset of PaymentOrchestrator used by subscriptions."""

    def create_intent(
        self, request: PaymentIntentRequest, *, conn: Any | None = None
    ) -> object: ...

    def cancel(self, payment_intent_id: str, *, conn: Any | None = None) -> object: ...


class SubscriptionService:
    """Spec/17 VX3 interim implementation: payment-link-per-cycle."""

    def __init__(
        self,
        *,
        store: SubscriptionStore,
        payment_orchestrator: PaymentIntentCreator,
        audit: AuditPort,
        outbox: OutboxPort,
        dunning: DunningConfig | None = None,
        calendar: BangladeshBankCalendar | None = None,
        default_method: str = "BKASH",
    ) -> None:
        self._store = store
        self._payments = payment_orchestrator
        self._audit = audit
        self._outbox = outbox
        self._dunning = dunning or DunningConfig()
        self._calendar = calendar or BangladeshBankCalendar()
        self._default_method = default_method
        self._seen_events: set[str] = set()

    # -- scheduler seam -----------------------------------------------------

    def scheduled_task(self, *, clock: Clock, cadence_seconds: float = 60.0) -> ScheduledTask:
        """Platform scheduler task for generation + invoicing ticks."""

        async def tick() -> dict[str, int]:
            generated = self.generate_due_cycles(clock=clock)
            invoiced = self.invoice_due_cycles(clock=clock)
            dunning = self.run_dunning(clock=clock)
            return {
                "generated": len(generated),
                "invoiced": len(invoiced),
                "dunning": len(dunning),
            }

        return ScheduledTask(
            name="subscription-cycles",
            fn=tick,
            cadence_seconds=cadence_seconds,
            window_gate="ALWAYS",
        )

    # -- create / views -----------------------------------------------------

    def create(
        self,
        merchant_id: str,
        payload: Mapping[str, object],
        *,
        idempotency_key: str,
        clock: Clock,
    ) -> dict[str, object]:
        if not merchant_id:
            raise InvalidRequestError("merchant_id is required", code="invalid_request")
        if not idempotency_key:
            raise InvalidRequestError(
                "Idempotency-Key is required", code="idempotency_key_required"
            )
        existing = self._store.find_subscription_by_idempotency_key(
            merchant_id, idempotency_key
        )
        if existing is not None:
            return self._subscription_view(existing)

        collection = str(payload.get("collection", "LINK"))
        if collection == "MANDATE":
            raise InvalidRequestError(
                "Mandate collection is not active in the interim model.",
                code="mandates_not_active",
            )
        customer_ref = payload.get("customer_ref")
        if not isinstance(customer_ref, str) or not customer_ref:
            raise InvalidRequestError("customer_ref is required", code="validation_failed")
        amount = _amount(payload.get("plan_amount_minor"), "plan_amount_minor")
        currency = payload.get("currency", "BDT")
        if currency != "BDT":
            raise InvalidRequestError("currency must be BDT", code="currency_unsupported")
        interval = str(payload.get("interval", ""))
        if interval not in ("WEEKLY", "MONTHLY"):
            raise InvalidRequestError("interval must be WEEKLY or MONTHLY", code="invalid_interval")
        anchor_date = _parse_date(payload.get("anchor_date"))
        metadata = _metadata(payload.get("metadata", {}))
        now = clock.now()
        sub_id = make_subscription_id(
            "subs",
            {
                "merchant_id": merchant_id,
                "customer_ref": customer_ref,
                "plan_amount_minor": amount,
                "interval": interval,
                "anchor_date": anchor_date.isoformat(),
                "created_idempotency_key": idempotency_key,
            },
        )
        record = SubscriptionRecord(
            subscription_id=sub_id,
            merchant_id=merchant_id,
            customer_ref=customer_ref,
            plan_amount_minor=amount,
            interval=interval,
            anchor_date=anchor_date,
            collection=collection,
            status="ACTIVE",
            metadata=metadata,
            created_idempotency_key=idempotency_key,
            created_at=now,
            updated_at=now,
        )
        self._store.insert_subscription(record)
        self._audit_subscription(record, None, "ACTIVE", "subscription_created", clock=clock)
        self._emit_subscription("subscription.created", record, clock=clock)
        return self._subscription_view(record)

    def get(self, subscription_id: str, *, merchant_id: str) -> dict[str, object]:
        return self._subscription_view(self._require_subscription(subscription_id, merchant_id))

    def list(
        self, *, merchant_id: str, limit: int, cursor: str | None
    ) -> tuple[list[dict[str, object]], str | None]:
        records, next_cursor = self._store.list_subscriptions(
            merchant_id=merchant_id, limit=limit, cursor=cursor
        )
        return [self._subscription_view(record) for record in records], next_cursor

    def get_cycle(self, cycle_id: str) -> dict[str, object]:
        cycle = self._store.get_cycle(cycle_id)
        if cycle is None:
            raise NotFoundError("subscription cycle not found", code="subscription_cycle_not_found")
        return self._cycle_view(cycle)

    def list_cycles(
        self, subscription_id: str, *, merchant_id: str, limit: int, cursor: str | None
    ) -> tuple[list[dict[str, object]], str | None]:
        self._require_subscription(subscription_id, merchant_id)
        rows, next_cursor = self._store.list_cycles(
            subscription_id, limit=limit, cursor=cursor
        )
        return [self._cycle_view(row) for row in rows], next_cursor

    # -- subscription FSM actions ------------------------------------------

    def pause(self, subscription_id: str, *, merchant_id: str, clock: Clock) -> dict[str, object]:
        record = self._require_subscription(subscription_id, merchant_id)
        return self._transition_subscription(
            record,
            "pause",
            event_type="subscription.paused",
            clock=clock,
        )

    def resume(self, subscription_id: str, *, merchant_id: str, clock: Clock) -> dict[str, object]:
        record = self._require_subscription(subscription_id, merchant_id)
        return self._transition_subscription(
            record,
            "resume",
            event_type="subscription.resumed",
            clock=clock,
        )

    def cancel(self, subscription_id: str, *, merchant_id: str, clock: Clock) -> dict[str, object]:
        record = self._require_subscription(subscription_id, merchant_id)
        for cycle in self._open_cycles(record.subscription_id):
            latest = cycle.payment_intent_ids[-1] if cycle.payment_intent_ids else None
            if latest is not None:
                self._payments.cancel(latest)
            cancelled = replace(
                cycle,
                status=SUBSCRIPTION_CYCLE_TABLE.resolve(
                    cycle.status, "subscription_cancelled"
                ).to_state,
                terminal_at=clock.now(),
                updated_at=clock.now(),
            )
            self._store.save_cycle(cancelled)
            self._audit_cycle(
                cancelled,
                cycle.status,
                cancelled.status,
                "subscription_cancelled",
                clock=clock,
            )
        return self._transition_subscription(
            record,
            "cancel",
            event_type="subscription.cancelled",
            clock=clock,
        )

    def create_mandate(
        self,
        payload: Mapping[str, object],
        *,
        merchant_id: str,
        idempotency_key: str,
        clock: Clock,
    ) -> dict[str, object]:
        del payload, merchant_id, idempotency_key, clock
        raise InvalidRequestError(
            "Mandate rails are not active in this interim model.",
            code="mandates_not_active",
        )

    # -- cycle scheduling ---------------------------------------------------

    def preview_due_at(self, subscription_id: str, cycle_number: int) -> datetime:
        record = self._store.get_subscription(subscription_id)
        if record is None:
            raise NotFoundError("subscription not found", code="subscription_not_found")
        return self._due_at(record, cycle_number)

    def generate_due_cycles(self, *, clock: Clock) -> list[dict[str, object]]:
        now = clock.now()
        created: list[dict[str, object]] = []
        for subscription in self._store.all_subscriptions():
            if subscription.status in ("CANCELLED", "COMPLETED", "PAST_DUE"):
                continue
            while True:
                rows, _ = self._store.list_cycles(subscription.subscription_id)
                next_number = len(rows) + 1
                due_at = self._due_at(subscription, next_number)
                if due_at > now:
                    break
                existing = self._store.get_cycle_by_number(
                    subscription.subscription_id, next_number
                )
                if existing is not None:
                    break
                status = "SCHEDULED" if subscription.status == "ACTIVE" else "SKIPPED"
                cycle = SubscriptionCycleRecord(
                    cycle_id=make_subscription_id(
                        "scyc",
                        {
                            "subscription_id": subscription.subscription_id,
                            "cycle_number": next_number,
                        },
                    ),
                    subscription_id=subscription.subscription_id,
                    cycle_number=next_number,
                    amount_minor=subscription.plan_amount_minor,
                    due_at=due_at,
                    status=status,
                    created_at=now,
                    updated_at=now,
                )
                self._store.insert_cycle(cycle)
                if status == "SKIPPED":
                    self._audit_cycle(
                        cycle,
                        "SCHEDULED",
                        "SKIPPED",
                        "subscription_paused",
                        clock=clock,
                    )
                    self._emit_cycle("subscription_cycle.skipped", cycle, subscription, clock=clock)
                created.append(self._cycle_view(cycle))
                if subscription.status != "ACTIVE":
                    break
        return created

    def invoice_due_cycles(self, *, clock: Clock) -> list[dict[str, object]]:
        invoiced: list[dict[str, object]] = []
        for cycle in self._store.list_due_cycles(now=clock.now()):
            subscription = self._require_subscription_by_id(cycle.subscription_id)
            if subscription.status != "ACTIVE":
                skipped = replace(
                    cycle,
                    status=SUBSCRIPTION_CYCLE_TABLE.resolve(
                        cycle.status, "subscription_paused_or_cancelled"
                    ).to_state,
                    terminal_at=clock.now(),
                    updated_at=clock.now(),
                )
                self._store.save_cycle(skipped)
                self._audit_cycle(skipped, cycle.status, skipped.status, "skip", clock=clock)
                self._emit_cycle("subscription_cycle.skipped", skipped, subscription, clock=clock)
                invoiced.append(self._cycle_view(skipped))
                continue
            if cycle.payment_intent_ids:
                continue
            intent_id = self._create_intent(subscription, cycle, attempt_index=0)
            updated = replace(
                cycle,
                status=SUBSCRIPTION_CYCLE_TABLE.resolve(cycle.status, "cycle_due").to_state,
                payment_intent_ids=(intent_id,),
                updated_at=clock.now(),
            )
            self._store.save_cycle(updated)
            self._audit_cycle(updated, cycle.status, updated.status, "cycle_due", clock=clock)
            self._emit_cycle("subscription_cycle.invoiced", updated, subscription, clock=clock)
            invoiced.append(self._cycle_view(updated))
        return invoiced

    def run_dunning(self, *, clock: Clock) -> list[dict[str, object]]:
        retried: list[dict[str, object]] = []
        for cycle in self._store.list_due_dunning(now=clock.now()):
            subscription = self._require_subscription_by_id(cycle.subscription_id)
            if cycle.dunning_attempts >= len(self._dunning.schedule_days):
                failed = self._fail_cycle(cycle, subscription, clock)
                retried.append(self._cycle_view(failed))
                continue
            attempt_number = cycle.dunning_attempts + 1
            intent_id = self._create_intent(subscription, cycle, attempt_index=attempt_number)
            next_due = self._next_dunning_at(cycle.dunning_started_at, attempt_number)
            updated = replace(
                cycle,
                status=SUBSCRIPTION_CYCLE_TABLE.resolve(cycle.status, "dunning_attempt").to_state,
                payment_intent_ids=(*cycle.payment_intent_ids, intent_id),
                dunning_attempts=attempt_number,
                next_dunning_at=next_due,
                updated_at=clock.now(),
            )
            self._store.save_cycle(updated)
            self._store.insert_dunning_attempt(
                DunningAttemptRecord(
                    cycle_id=cycle.cycle_id,
                    attempt_number=attempt_number,
                    payment_intent_id=intent_id,
                    scheduled_at=cycle.next_dunning_at or clock.now(),
                    created_at=clock.now(),
                )
            )
            self._audit_cycle(updated, cycle.status, updated.status, "dunning_attempt", clock=clock)
            self._emit_cycle(
                "subscription_cycle.invoiced",
                updated,
                subscription,
                clock=clock,
                extra={"dunning_attempt": attempt_number},
            )
            retried.append(self._cycle_view(updated))
        return retried

    # -- consumed payment events -------------------------------------------

    def consume_payment_event(self, event: Mapping[str, object], *, clock: Clock) -> None:
        event_id = event.get("event_id")
        if not isinstance(event_id, str) or not event_id:
            raise InvalidRequestError("event_id is required", code="invalid_event")
        if event_id in self._seen_events:
            return
        self._seen_events.add(event_id)
        payload = event.get("payload")
        payload = payload if isinstance(payload, Mapping) else {}
        payment_intent_id = payload.get("payment_intent_id")
        if not isinstance(payment_intent_id, str):
            return
        cycle = self._store.find_cycle_by_intent(payment_intent_id)
        if cycle is None or SUBSCRIPTION_CYCLE_TABLE.is_terminal(cycle.status):
            return
        subscription = self._require_subscription_by_id(cycle.subscription_id)
        event_type = event.get("type")
        if event_type == "payment_intent.succeeded":
            self._mark_cycle_paid(cycle, subscription, clock)
            return
        if event_type not in {
            "payment_intent.failed",
            "payment_intent.expired",
            "payment_intent.cancelled",
        }:
            return
        if cycle.status == "COLLECTING":
            now = clock.now()
            updated = replace(
                cycle,
                status=SUBSCRIPTION_CYCLE_TABLE.resolve(
                    cycle.status, "intent_failed_or_expired"
                ).to_state,
                dunning_started_at=now,
                next_dunning_at=now + timedelta(days=self._dunning.schedule_days[0]),
                updated_at=now,
            )
            self._store.save_cycle(updated)
            self._audit_cycle(
                updated, cycle.status, updated.status, "intent_failed_or_expired", clock=clock
            )
            self._emit_cycle(
                "subscription_cycle.dunning_started", updated, subscription, clock=clock
            )
            return
        if cycle.status == "DUNNING" and cycle.dunning_attempts >= len(
            self._dunning.schedule_days
        ):
            self._fail_cycle(cycle, subscription, clock)

    def sweep_past_due(self, *, clock: Clock) -> list[str]:
        cancelled: list[str] = []
        now = clock.now()
        for subscription in self._store.all_subscriptions():
            if (
                subscription.status == "PAST_DUE"
                and subscription.past_due_grace_expires_at is not None
                and subscription.past_due_grace_expires_at < now
            ):
                view = self._transition_subscription(
                    subscription,
                    "grace_expired",
                    event_type="subscription.cancelled",
                    clock=clock,
                    extra={"reason": "dunning_exhausted"},
                )
                cancelled.append(str(view["subscription_id"]))
        return cancelled

    # -- internals ----------------------------------------------------------

    def _require_subscription(self, subscription_id: str, merchant_id: str) -> SubscriptionRecord:
        record = self._store.get_subscription(subscription_id)
        if record is None or record.merchant_id != merchant_id:
            raise NotFoundError("subscription not found", code="subscription_not_found")
        return record

    def _require_subscription_by_id(self, subscription_id: str) -> SubscriptionRecord:
        record = self._store.get_subscription(subscription_id)
        if record is None:
            raise NotFoundError("subscription not found", code="subscription_not_found")
        return record

    def _open_cycles(self, subscription_id: str) -> Sequence[SubscriptionCycleRecord]:
        rows, _ = self._store.list_cycles(subscription_id)
        return [row for row in rows if row.status in ("COLLECTING", "DUNNING")]

    def _transition_subscription(
        self,
        record: SubscriptionRecord,
        trigger: str,
        *,
        event_type: str,
        clock: Clock,
        extra: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        rule = SUBSCRIPTION_TABLE.resolve(record.status, trigger)
        now = clock.now()
        terminal_at = now if rule.to_state in SUBSCRIPTION_TABLE.terminal_states else None
        updated = replace(record, status=rule.to_state, updated_at=now, terminal_at=terminal_at)
        self._store.save_subscription(updated)
        self._audit_subscription(updated, record.status, rule.to_state, trigger, clock=clock)
        self._emit_subscription(event_type, updated, clock=clock, extra=extra)
        return self._subscription_view(updated)

    def _mark_cycle_paid(
        self, cycle: SubscriptionCycleRecord, subscription: SubscriptionRecord, clock: Clock
    ) -> None:
        trigger = "intent_succeeded" if cycle.status == "COLLECTING" else "attempt_succeeded"
        rule = SUBSCRIPTION_CYCLE_TABLE.resolve(cycle.status, trigger)
        now = clock.now()
        updated = replace(
            cycle,
            status=rule.to_state,
            paid_at=now,
            terminal_at=now,
            next_dunning_at=None,
            updated_at=now,
        )
        self._store.save_cycle(updated)
        self._audit_cycle(updated, cycle.status, updated.status, trigger, clock=clock)
        self._emit_cycle("subscription_cycle.paid", updated, subscription, clock=clock)
        if subscription.status == "PAST_DUE":
            self._transition_subscription(
                subscription,
                "cycle_paid",
                event_type="subscription.reactivated",
                clock=clock,
            )

    def _fail_cycle(
        self,
        cycle: SubscriptionCycleRecord,
        subscription: SubscriptionRecord,
        clock: Clock,
    ) -> SubscriptionCycleRecord:
        rule = SUBSCRIPTION_CYCLE_TABLE.resolve(cycle.status, "schedule_exhausted")
        now = clock.now()
        failed = replace(
            cycle,
            status=rule.to_state,
            failed_at=now,
            terminal_at=now,
            next_dunning_at=None,
            updated_at=now,
        )
        self._store.save_cycle(failed)
        self._audit_cycle(failed, cycle.status, failed.status, "schedule_exhausted", clock=clock)
        self._emit_cycle("subscription_cycle.failed", failed, subscription, clock=clock)
        if subscription.status == "ACTIVE":
            grace = now + timedelta(days=self._dunning.past_due_grace_days)
            past_due = replace(
                subscription,
                status=SUBSCRIPTION_TABLE.resolve("ACTIVE", "cycle_failed_final").to_state,
                past_due_grace_expires_at=grace,
                updated_at=now,
            )
            self._store.save_subscription(past_due)
            self._audit_subscription(
                past_due, "ACTIVE", "PAST_DUE", "cycle_failed_final", clock=clock
            )
            self._emit_subscription("subscription.past_due", past_due, clock=clock)
        return failed

    def _create_intent(
        self,
        subscription: SubscriptionRecord,
        cycle: SubscriptionCycleRecord,
        *,
        attempt_index: int,
    ) -> str:
        idem = f"scyc:{cycle.cycle_id}:attempt:{attempt_index}"
        result = self._payments.create_intent(
            PaymentIntentRequest(
                merchant_id=subscription.merchant_id,
                amount_minor=cycle.amount_minor,
                method=self._default_method,
                idempotency_key=idem,
                description=f"Subscription cycle {cycle.cycle_number}",
                metadata={
                    "subscription_id": subscription.subscription_id,
                    "cycle_id": cycle.cycle_id,
                    "cycle_number": str(cycle.cycle_number),
                },
            )
        )
        intent_id = _intent_id(result)
        if not intent_id:
            raise ConflictError("payment intent creation returned no id", code="intent_not_created")
        return intent_id

    def _next_dunning_at(
        self, dunning_started_at: datetime | None, attempts_created: int
    ) -> datetime | None:
        if dunning_started_at is None or attempts_created >= len(self._dunning.schedule_days):
            return None
        return dunning_started_at + timedelta(days=self._dunning.schedule_days[attempts_created])

    def _due_at(self, subscription: SubscriptionRecord, cycle_number: int) -> datetime:
        due_date = _cycle_date(subscription.anchor_date, subscription.interval, cycle_number)
        while not self._calendar.is_working_day(due_date):
            due_date = self._calendar.next_working_day(due_date)
        return datetime.combine(due_date, time(0, 0), tzinfo=DHAKA_TZ).astimezone(UTC)

    def _audit_subscription(
        self,
        record: SubscriptionRecord,
        from_state: str | None,
        to_state: str,
        trigger: str,
        *,
        clock: Clock,
    ) -> None:
        self._audit.append(
            AuditEventSpec(
                event_type="SUBSCRIPTION_STATE_TRANSITION",
                actor_id=PRODUCER,
                subject_type="Subscription",
                subject_id=record.subscription_id,
                from_state=from_state,
                to_state=to_state,
                payload={"merchant_id": record.merchant_id, "trigger": trigger},
            ),
            clock=clock,
        )

    def _audit_cycle(
        self,
        cycle: SubscriptionCycleRecord,
        from_state: str | None,
        to_state: str,
        trigger: str,
        *,
        clock: Clock,
    ) -> None:
        self._audit.append(
            AuditEventSpec(
                event_type="SUBSCRIPTION_CYCLE_STATE_TRANSITION",
                actor_id=PRODUCER,
                subject_type="SubscriptionCycle",
                subject_id=cycle.cycle_id,
                from_state=from_state,
                to_state=to_state,
                payload={"subscription_id": cycle.subscription_id, "trigger": trigger},
            ),
            clock=clock,
        )

    def _emit_subscription(
        self,
        event_type: str,
        record: SubscriptionRecord,
        *,
        clock: Clock,
        extra: Mapping[str, object] | None = None,
    ) -> None:
        payload: dict[str, object] = {
            "subscription_id": record.subscription_id,
            "merchant_id": record.merchant_id,
            "status": record.status,
        }
        if record.past_due_grace_expires_at is not None:
            payload["past_due_grace_expires_at"] = _rfc3339(record.past_due_grace_expires_at)
        if extra:
            payload.update(dict(extra))
        self._outbox.enqueue(
            build_event(
                event_type=event_type,
                subject_type="Subscription",
                subject_id=record.subscription_id,
                producer=PRODUCER,
                topic=TOPIC,
                payload={key: _clean(value) for key, value in payload.items()},
                occurred_at=clock.now(),
            )
        )

    def _emit_cycle(
        self,
        event_type: str,
        cycle: SubscriptionCycleRecord,
        subscription: SubscriptionRecord,
        *,
        clock: Clock,
        extra: Mapping[str, object] | None = None,
    ) -> None:
        payload: dict[str, object] = {
            "cycle_id": cycle.cycle_id,
            "subscription_id": cycle.subscription_id,
            "cycle_number": cycle.cycle_number,
            "merchant_id": subscription.merchant_id,
            "amount_minor": cycle.amount_minor,
            "status": cycle.status,
        }
        if cycle.payment_intent_ids:
            payload["payment_intent_id"] = cycle.payment_intent_ids[-1]
        if extra:
            payload.update(dict(extra))
        self._outbox.enqueue(
            build_event(
                event_type=event_type,
                subject_type="SubscriptionCycle",
                subject_id=cycle.cycle_id,
                producer=PRODUCER,
                topic=TOPIC,
                payload={key: _clean(value) for key, value in payload.items()},
                occurred_at=clock.now(),
            )
        )

    def _subscription_view(self, record: SubscriptionRecord) -> dict[str, object]:
        return {
            "subscription_id": record.subscription_id,
            "merchant_id": record.merchant_id,
            "customer_ref": record.customer_ref,
            "plan_amount_minor": record.plan_amount_minor,
            "currency": record.currency,
            "interval": record.interval,
            "anchor_date": record.anchor_date.isoformat(),
            "end_date": None if record.end_date is None else record.end_date.isoformat(),
            "collection": record.collection,
            "status": record.status,
            "metadata": dict(record.metadata),
            "past_due_grace_expires_at": (
                None
                if record.past_due_grace_expires_at is None
                else _rfc3339(record.past_due_grace_expires_at)
            ),
            "created_at": _rfc3339(record.created_at),
            "updated_at": _rfc3339(record.updated_at),
            "terminal_at": None if record.terminal_at is None else _rfc3339(record.terminal_at),
        }

    def _cycle_view(self, cycle: SubscriptionCycleRecord) -> dict[str, object]:
        attempts = self._store.list_dunning_attempts(cycle.cycle_id)
        return {
            "cycle_id": cycle.cycle_id,
            "subscription_id": cycle.subscription_id,
            "cycle_number": cycle.cycle_number,
            "amount_minor": cycle.amount_minor,
            "due_at": _rfc3339(cycle.due_at),
            "status": cycle.status,
            "payment_intent_ids": list(cycle.payment_intent_ids),
            "dunning_attempts": cycle.dunning_attempts,
            "dunning_attempt_log": [
                {
                    "attempt_number": row.attempt_number,
                    "payment_intent_id": row.payment_intent_id,
                    "scheduled_at": _rfc3339(row.scheduled_at),
                    "created_at": _rfc3339(row.created_at),
                }
                for row in attempts
            ],
            "next_dunning_at": (
                None if cycle.next_dunning_at is None else _rfc3339(cycle.next_dunning_at)
            ),
            "paid_at": None if cycle.paid_at is None else _rfc3339(cycle.paid_at),
            "failed_at": None if cycle.failed_at is None else _rfc3339(cycle.failed_at),
        }


def _amount(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidRequestError(f"{name} must be integer paisa", code="invalid_amount")
    if value <= 0:
        raise InvalidRequestError(f"{name} must be > 0", code="invalid_amount")
    return value


def _parse_date(value: object) -> date:
    if not isinstance(value, str):
        raise InvalidRequestError("anchor_date is required", code="invalid_anchor_date")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise InvalidRequestError(
            "anchor_date must be YYYY-MM-DD", code="invalid_anchor_date"
        ) from exc


def _metadata(value: object) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise InvalidRequestError("metadata must be an object", code="metadata_invalid")
    output: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, str):
            raise InvalidRequestError("metadata must be string-to-string", code="metadata_invalid")
        output[key] = redact(item)
    return output


def _cycle_date(anchor: date, interval: str, cycle_number: int) -> date:
    if interval == "WEEKLY":
        return anchor + timedelta(days=7 * (cycle_number - 1))
    month_index = anchor.month - 1 + (cycle_number - 1)
    year = anchor.year + month_index // 12
    month = month_index % 12 + 1
    day = min(anchor.day, month_calendar.monthrange(year, month)[1])
    return date(year, month, day)


def _intent_id(result: object) -> str:
    if isinstance(result, Mapping):
        return str(result.get("payment_intent_id", ""))
    return str(getattr(result, "payment_intent_id", ""))


def _rfc3339(at: datetime) -> str:
    return at.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _clean(value: object) -> object:
    return redact(value) if isinstance(value, str) else value

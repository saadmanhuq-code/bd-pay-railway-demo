"""TransactionMonitor — streaming rule evaluation over payment events (spec/06).

The monitor is a pure consumer of spec/00 §5 envelopes from ``payment.events``
(idempotent on ``event_id``; at-least-once delivery never duplicates an
alert). It evaluates the ACTIVE signed rule pack only — no unsigned rule is
ever evaluated (spec/06 cut-last #5) — covering:

- structuring (``rule_structuring_detection_3day``): >=3 transactions each
  below the CTR threshold inside a 72h rolling window, aggregating at or
  above it;
- velocity (``rule_velocity_24h_spike``): 24h count OR volume above the
  thresholds-artifact values;
- account-behaviour baselines: fan-in/fan-out mule pattern
  (``rule_mule_account_pattern``) and dormant-reactivation deviation
  (monitor-local ``rule_account_baseline_deviation`` — same class as the
  spec's ``rule_monitor_evaluation_failure``, see errata E-C5).

Monitoring is NOT pre-auth: an evaluation exception never blocks a payment.
It is caught, audited, and surfaced as an alert under
``rule_monitor_evaluation_failure`` (spec/06 failure-mode table).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from bdpay.compliance.alerts import AmlAlertService
from bdpay.compliance.ids_ext import make_compliance_id
from bdpay.compliance.packs import RulePackStore
from bdpay.compliance.thresholds import ComplianceThresholds
from bdpay.platform.clock import Clock
from bdpay.platform.errors import ConflictError, InvalidRequestError
from bdpay.platform.interfaces import AuditEventSpec, AuditPort

if TYPE_CHECKING:
    # Forward reference only: avoids a circular import between compliance and
    # kernel. The type is checked structurally via the PaymentStore protocol.
    from bdpay.kernel.repository import PaymentStore

__all__ = [
    "InMemoryMonitoringEventStore",
    "MonitoringEventStore",
    "MonitoringRuleEvent",
    "PaymentObservation",
    "TransactionMonitor",
]

PRODUCER = "aml-monitor@1.0.0"

MONITOR_FAILURE_RULE_ID = "rule_monitor_evaluation_failure"
BASELINE_RULE_ID = "rule_account_baseline_deviation"

_EVALUATION_RESULTS = ("ALERT_RAISED", "NEAR_MISS", "PASSED")


@dataclass(frozen=True)
class PaymentObservation:
    """One observed charged payment, as the monitor remembers it."""

    event_id: str
    payment_intent_id: str
    subject_type: str
    subject_id: str
    amount_minor: int
    occurred_at: datetime
    sender_ref: str | None = None
    beneficiary_ref: str | None = None
    direction: str = "OUTBOUND"  # OUTBOUND = subject pays | INBOUND = subject receives


@dataclass(frozen=True)
class MonitoringRuleEvent:
    """One monitoring_rule_events row (migration 0078)."""

    event_id: str  # mrev_<...>
    payment_intent_id: str
    rule_id: str
    rule_pack_id: str
    subject_type: str
    subject_id: str
    evaluation_result: str
    evaluated_at: datetime
    score_delta: int | None = None
    alert_id: str | None = None
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.evaluation_result not in _EVALUATION_RESULTS:
            raise ValueError(
                f"evaluation_result must be one of {_EVALUATION_RESULTS}, "
                f"got {self.evaluation_result!r}"
            )


@runtime_checkable
class MonitoringEventStore(Protocol):
    """Storage contract for monitoring_rule_events."""

    def insert(self, row: MonitoringRuleEvent) -> None: ...

    def by_subject(self, subject_type: str, subject_id: str) -> list[MonitoringRuleEvent]: ...


class InMemoryMonitoringEventStore:
    """Deterministic in-memory monitoring-event store for unit tests."""

    def __init__(self) -> None:
        self._rows: dict[str, MonitoringRuleEvent] = {}
        self._order: list[str] = []

    def insert(self, row: MonitoringRuleEvent) -> None:
        if row.event_id in self._rows:  # content-addressed dedupe (spec/06)
            return
        self._rows[row.event_id] = row
        self._order.append(row.event_id)

    def by_subject(self, subject_type: str, subject_id: str) -> list[MonitoringRuleEvent]:
        return [
            self._rows[e]
            for e in self._order
            if self._rows[e].subject_type == subject_type
            and self._rows[e].subject_id == subject_id
        ]


@dataclass
class _SubjectHistory:
    observations: list[PaymentObservation] = field(default_factory=list)
    last_seen_at: datetime | None = None
    #: When activity resumed after a dormancy gap (> DORMANCY_DAYS quiet).
    dormancy_break_at: datetime | None = None


class TransactionMonitor:
    """Streaming evaluator: one envelope in, zero or more alerts out."""

    def __init__(
        self,
        *,
        alerts: AmlAlertService,
        packs: RulePackStore,
        events: MonitoringEventStore,
        clock: Clock,
        audit: AuditPort,
        thresholds: ComplianceThresholds,
        payment_store: PaymentStore | None = None,
    ) -> None:
        self._alerts = alerts
        self._packs = packs
        self._events = events
        self._clock = clock
        self._audit = audit
        self._thresholds = thresholds
        self._seen_event_ids: set[str] = set()
        self._history: dict[tuple[str, str], _SubjectHistory] = {}
        # AML-H2: rehydrate rolling-window observation history from durable
        # payment data on startup so monitoring windows survive restarts.
        # Without this a ring can evade structuring detection by timing a
        # service restart — the 72h window forgets all prior sub-threshold
        # transactions and the alert never fires.
        #
        # We reconstruct PaymentObservation from payment_intents +
        # payment_attempts using the same field mapping as
        # _emit_attempt / _observation_from:
        #   direction    = "OUTBOUND"  (hardcoded at emit — all charges are OUTBOUND)
        #   sender_ref   = intent.customer_id
        #   beneficiary_ref = intent.merchant_id
        #   amount_minor = attempt.amount_minor
        #   occurred_at  = attempt.charged_at
        #
        # No new table is required (path d) — every field is derivable from
        # the existing persisted schema.  event_id is set to attempt_id, which
        # is unique per charged attempt and serves as a stable dedup key for
        # the rehydrated observations inside _history (the live _seen_event_ids
        # set still uses outbox event_ids, which differ, so rehydrated
        # observations are not pre-deduplicated from the live stream — that is
        # correct: the live stream's event_id is content-addressed from the
        # full envelope and is added to _seen_event_ids on first delivery).
        if payment_store is not None:
            since = self._clock.now() - timedelta(
                hours=thresholds.structuring_window_hours
            )
            for intent, attempt in payment_store.list_charged_since(since):
                if attempt.charged_at is None:
                    continue
                customer_id = intent.customer_id
                merchant_id = intent.merchant_id
                if customer_id:
                    subject_type, subject_id = "Customer", customer_id
                elif merchant_id:
                    subject_type, subject_id = "Merchant", merchant_id
                else:
                    continue  # no subject — skip (mirrors _observation_from guard)
                observation = PaymentObservation(
                    event_id=attempt.attempt_id,
                    payment_intent_id=intent.payment_intent_id,
                    subject_type=subject_type,
                    subject_id=subject_id,
                    amount_minor=attempt.amount_minor,
                    occurred_at=attempt.charged_at,
                    sender_ref=customer_id,
                    beneficiary_ref=merchant_id,
                    direction="OUTBOUND",
                )
                key = (observation.subject_type, observation.subject_id)
                history = self._history.setdefault(key, _SubjectHistory())
                if (
                    history.last_seen_at is not None
                    and observation.occurred_at - history.last_seen_at
                    > timedelta(days=thresholds.dormancy_days)
                ):
                    history.dormancy_break_at = observation.occurred_at
                history.observations.append(observation)
                history.last_seen_at = observation.occurred_at

    def has_active_rule_pack(self) -> bool:
        """Return True only when a verified ACTIVE rule pack is loaded."""
        return self._packs.active() is not None

    # -- envelope intake ----------------------------------------------------------

    def intake_event(self, envelope: Mapping[str, object]) -> list[str]:
        """Consume one spec/00 §5 envelope; returns alert ids raised.

        Idempotent on ``event_id``: a duplicate delivery is a recorded no-op.
        Only ``payment_attempt.charged`` envelopes are evaluated; other types
        update the behaviour baseline without rule evaluation.
        """
        event_id = envelope.get("event_id")
        if not isinstance(event_id, str) or not event_id:
            raise InvalidRequestError(
                "envelope is missing a string event_id", code="invalid_envelope"
            )
        if event_id in self._seen_event_ids:
            return []
        self._seen_event_ids.add(event_id)
        event_type = envelope.get("type")
        if event_type != "payment_attempt.charged":
            return []

        pack = self._packs.active()
        if pack is None:
            raise ConflictError(
                "no ACTIVE signed rule pack is loaded; the monitor refuses to "
                "evaluate unsigned/absent rules (spec/06 cut-last #5)",
                code="no_active_rule_pack",
            )

        observation = self._observation_from(envelope, event_id)
        key = (observation.subject_type, observation.subject_id)
        history = self._history.setdefault(key, _SubjectHistory())
        previous_last_seen = history.last_seen_at
        if previous_last_seen is not None and (
            observation.occurred_at - previous_last_seen
            > timedelta(days=self._thresholds.dormancy_days)
        ):
            history.dormancy_break_at = observation.occurred_at
        history.observations.append(observation)
        history.last_seen_at = observation.occurred_at

        raised: list[str] = []
        for rule_id, evaluator in (
            ("rule_structuring_detection_3day", self._evaluate_structuring),
            ("rule_velocity_24h_spike", self._evaluate_velocity),
            ("rule_mule_account_pattern", self._evaluate_mule),
            (BASELINE_RULE_ID, None),
        ):
            try:
                if rule_id == BASELINE_RULE_ID:
                    outcome = self._evaluate_baseline(observation, history, previous_last_seen)
                else:
                    if rule_id not in pack.rules_json:
                        continue  # only signed, loaded rules are evaluated
                    outcome = evaluator(observation, history)  # type: ignore[misc]
            except Exception as exc:  # noqa: BLE001 - monitoring is not pre-auth
                raised.extend(self._record_evaluation_failure(observation, pack.pack_id, exc))
                continue
            result, window_start, contributing, amount = outcome
            alert_id: str | None = None
            if result == "ALERT_RAISED":
                rule_def = pack.rules_json.get(rule_id, {})
                is_legal = (
                    isinstance(rule_def, Mapping)
                    and rule_def.get("certification_tier") == "legal"
                )
                tier = "HIGH" if is_legal else "MEDIUM"
                alert = self._alerts.raise_alert(
                    subject_type=observation.subject_type,
                    subject_id=observation.subject_id,
                    rule_id=rule_id,
                    rule_pack_id=pack.pack_id,
                    risk_tier=tier,
                    window_start=window_start,
                    contributing_payment_ids=contributing,
                    triggered_amount_minor=amount,
                )
                alert_id = alert.alert_id
                if alert_id not in raised:
                    raised.append(alert_id)
            self._events.insert(
                MonitoringRuleEvent(
                    event_id=make_compliance_id(
                        "mrev",
                        {
                            "rule_id": rule_id,
                            "payment_intent_id": observation.payment_intent_id,
                            "evaluated_at": observation.occurred_at,
                        },
                    ),
                    payment_intent_id=observation.payment_intent_id,
                    rule_id=rule_id,
                    rule_pack_id=pack.pack_id,
                    subject_type=observation.subject_type,
                    subject_id=observation.subject_id,
                    evaluation_result=result,
                    evaluated_at=observation.occurred_at,
                    alert_id=alert_id,
                )
            )
        return raised

    @staticmethod
    def _observation_from(envelope: Mapping[str, object], event_id: str) -> PaymentObservation:
        payload = envelope.get("payload")
        if not isinstance(payload, Mapping):
            raise InvalidRequestError(
                "envelope payload must be a mapping", code="invalid_envelope"
            )
        occurred_at = envelope.get("occurred_at")
        if isinstance(occurred_at, str):
            occurred_at = datetime.fromisoformat(occurred_at.replace("Z", "+00:00"))
        if not isinstance(occurred_at, datetime) or occurred_at.tzinfo is None:
            raise InvalidRequestError(
                "occurred_at must be an aware timestamp", code="invalid_envelope"
            )
        customer_id = payload.get("customer_id")
        merchant_id = payload.get("merchant_id")
        if isinstance(customer_id, str) and customer_id:
            subject_type, subject_id = "Customer", customer_id
        elif isinstance(merchant_id, str) and merchant_id:
            subject_type, subject_id = "Merchant", merchant_id
        else:
            raise InvalidRequestError(
                "payload carries neither customer_id nor merchant_id",
                code="invalid_envelope",
            )
        amount = payload.get("amount_minor")
        if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
            raise InvalidRequestError(
                "payload amount_minor must be positive int paisa", code="invalid_envelope"
            )
        pi_id = payload.get("payment_intent_id")
        if not isinstance(pi_id, str) or not pi_id:
            raise InvalidRequestError(
                "payload is missing payment_intent_id", code="invalid_envelope"
            )
        direction = payload.get("direction", "OUTBOUND")
        if direction not in ("OUTBOUND", "INBOUND"):
            raise InvalidRequestError(
                "direction must be OUTBOUND or INBOUND", code="invalid_envelope"
            )
        sender_ref = payload.get("sender_ref")
        beneficiary_ref = payload.get("beneficiary_ref")
        return PaymentObservation(
            event_id=event_id,
            payment_intent_id=pi_id,
            subject_type=subject_type,
            subject_id=subject_id,
            amount_minor=amount,
            occurred_at=occurred_at,
            sender_ref=sender_ref if isinstance(sender_ref, str) else None,
            beneficiary_ref=beneficiary_ref if isinstance(beneficiary_ref, str) else None,
            direction=str(direction),
        )

    # -- rule evaluators ------------------------------------------------------------
    # Each returns (evaluation_result, window_start, contributing_ids, amount).

    def _window(
        self, history: _SubjectHistory, *, ending_at: datetime, hours: int
    ) -> list[PaymentObservation]:
        start = ending_at - timedelta(hours=hours)
        return [
            o for o in history.observations if start <= o.occurred_at <= ending_at
        ]

    def _evaluate_structuring(
        self, observation: PaymentObservation, history: _SubjectHistory
    ) -> tuple[str, datetime, tuple[str, ...], int | None]:
        t = self._thresholds
        window = self._window(
            history,
            ending_at=observation.occurred_at,
            hours=t.structuring_window_hours,
        )
        below = [o for o in window if o.amount_minor < t.structuring_aggregate_threshold_minor]
        if any(o.amount_minor >= t.structuring_aggregate_threshold_minor for o in window):
            # max single txn in window at/above the threshold => not structuring
            return ("PASSED", observation.occurred_at, (), None)
        total = sum(o.amount_minor for o in below)
        count = len(below)
        if count >= t.structuring_min_txn_count:
            if total >= t.structuring_aggregate_threshold_minor:
                contributing = tuple(o.payment_intent_id for o in below)
                window_start = min(o.occurred_at for o in below)
                return ("ALERT_RAISED", window_start, contributing, total)
            return ("NEAR_MISS", observation.occurred_at, (), total)
        return ("PASSED", observation.occurred_at, (), None)

    def _evaluate_velocity(
        self, observation: PaymentObservation, history: _SubjectHistory
    ) -> tuple[str, datetime, tuple[str, ...], int | None]:
        t = self._thresholds
        window = self._window(history, ending_at=observation.occurred_at, hours=24)
        count = len(window)
        total = sum(o.amount_minor for o in window)
        if (
            count >= t.velocity_24h_count_threshold
            or total >= t.velocity_24h_amount_threshold_minor
        ):
            contributing = tuple(o.payment_intent_id for o in window)
            window_start = min(o.occurred_at for o in window)
            return ("ALERT_RAISED", window_start, contributing, total)
        return ("PASSED", observation.occurred_at, (), None)

    def _evaluate_mule(
        self, observation: PaymentObservation, history: _SubjectHistory
    ) -> tuple[str, datetime, tuple[str, ...], int | None]:
        t = self._thresholds
        window = self._window(history, ending_at=observation.occurred_at, hours=24)
        inbound = [o for o in window if o.direction == "INBOUND"]
        outbound = [o for o in window if o.direction == "OUTBOUND"]
        senders = {o.sender_ref for o in inbound if o.sender_ref}
        recipients = {o.beneficiary_ref for o in outbound if o.beneficiary_ref}
        if len(senders) < t.mule_fan_in_sender_count:
            return ("PASSED", observation.occurred_at, (), None)
        if not outbound or not recipients or len(recipients) > t.mule_fan_out_recipient_count:
            return ("PASSED", observation.occurred_at, (), None)
        # Deterministic forward-delay median (lower middle; integer seconds).
        delays: list[int] = []
        for out in outbound:
            priors = [i.occurred_at for i in inbound if i.occurred_at <= out.occurred_at]
            if priors:
                delays.append(int((out.occurred_at - max(priors)).total_seconds()))
        if not delays:
            return ("PASSED", observation.occurred_at, (), None)
        delays.sort()
        median = delays[(len(delays) - 1) // 2]
        if median <= t.mule_forward_window_seconds:
            contributing = tuple(o.payment_intent_id for o in window)
            window_start = min(o.occurred_at for o in window)
            total = sum(o.amount_minor for o in window)
            return ("ALERT_RAISED", window_start, contributing, total)
        return ("PASSED", observation.occurred_at, (), None)

    def _evaluate_baseline(
        self,
        observation: PaymentObservation,
        history: _SubjectHistory,
        previous_last_seen: datetime | None,
    ) -> tuple[str, datetime, tuple[str, ...], int | None]:
        """Dormant-reactivation deviation: quiet > DORMANCY_DAYS, then a spike.

        The reactivation instant is remembered on the subject history; the
        rule fires when >= DORMANCY_SPIKE_TXN_COUNT_24H transactions land
        within 24h of that break (idempotency anchored to the break instant).
        """
        t = self._thresholds
        break_at = history.dormancy_break_at
        if break_at is None or observation.occurred_at - break_at > timedelta(hours=24):
            return ("PASSED", observation.occurred_at, (), None)
        window = [
            o
            for o in history.observations
            if break_at <= o.occurred_at <= observation.occurred_at
        ]
        if len(window) >= t.dormancy_spike_txn_count_24h:
            contributing = tuple(o.payment_intent_id for o in window)
            total = sum(o.amount_minor for o in window)
            return ("ALERT_RAISED", break_at, contributing, total)
        return ("NEAR_MISS", observation.occurred_at, (), None)

    # -- failure surface --------------------------------------------------------

    def _record_evaluation_failure(
        self, observation: PaymentObservation, pack_id: str, exc: Exception
    ) -> list[str]:
        """spec/06 failure mode: exception caught, payment proceeds, alert raised."""
        self._audit.append(
            AuditEventSpec(
                event_type="MONITOR_EVALUATION_FAILED",
                actor_id=PRODUCER,
                subject_type="PaymentIntent",
                subject_id=observation.payment_intent_id,
                payload={"error_type": type(exc).__name__},
            ),
            clock=self._clock,
        )
        alert = self._alerts.raise_alert(
            subject_type=observation.subject_type,
            subject_id=observation.subject_id,
            rule_id=MONITOR_FAILURE_RULE_ID,
            rule_pack_id=pack_id,
            risk_tier="HIGH",
            window_start=observation.occurred_at,
            contributing_payment_ids=(observation.payment_intent_id,),
            notes="monitoring engine evaluation failure; CAMLCO paged",
        )
        return [alert.alert_id]

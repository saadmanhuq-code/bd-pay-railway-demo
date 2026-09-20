"""AmlAlert entity, repository, and the refusal-first alert FSM (spec/06).

States: ``RAISED -> TRIAGING -> IN_CASE -> {STR_FILED | CLOSED}`` with
``TRIAGING -> CLOSED`` permitted; ``STR_FILED`` and ``CLOSED`` are terminal.
Any transition not in the spec/06 table is DENIED (refusal-first). Every
transition writes one audit row; every state change enqueues one outbox event
in the same transaction context.

Closing from ``IN_CASE`` is four-eyes: the analyst's close call creates an
``ApprovalRequest`` (action ``aml_case_close``) and the alert closes only via
the approval callback (:meth:`AmlAlertService.complete_in_case_close`).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any, Protocol, runtime_checkable

from bdpay.compliance.thresholds import ComplianceThresholds
from bdpay.platform.clock import Clock
from bdpay.platform.errors import AuthorizationError, ConflictError, InvalidRequestError
from bdpay.platform.ids import make_id
from bdpay.platform.interfaces import ApprovalPort, AuditEventSpec, AuditPort, OutboxPort
from bdpay.platform.outbox import build_event

__all__ = [
    "AmlAlert",
    "AmlAlertService",
    "AlertStore",
    "InMemoryAlertStore",
    "SubjectControlPort",
    "RecordingSubjectControl",
]

PRODUCER = "aml-monitor@1.0.0"

_STATES = ("RAISED", "TRIAGING", "IN_CASE", "STR_FILED", "CLOSED")
_TERMINAL = ("STR_FILED", "CLOSED")
_RISK_TIERS = ("HIGH", "MEDIUM", "LOW")
_CLOSE_REASONS = ("FALSE_POSITIVE", "RESOLVED", "BELOW_THRESHOLD", "OTHER")
_ANALYST_ROLES = ("COMPLIANCE_ANALYST", "CAMLCO")


@runtime_checkable
class SubjectControlPort(Protocol):
    """Freeze/halt side effects, executed by the kernel's LimitEnforcer.

    The kernel package owns the implementation (spec/02); compliance calls
    through this narrow port so every freeze transition produces proper audit
    entries through the kernel FSM (spec/06 Scope). Fail-closed: a raise from
    any method aborts the calling compliance transaction.
    """

    def freeze_subject(
        self, subject_type: str, subject_id: str, *, reason: str, reference_id: str
    ) -> None: ...

    def unfreeze_subject(
        self, subject_type: str, subject_id: str, *, reason: str, reference_id: str
    ) -> None: ...

    def halt_onboarding(
        self, subject_type: str, subject_id: str, *, reason: str, reference_id: str
    ) -> None: ...

    def freeze_payouts(
        self, subject_type: str, subject_id: str, *, reason: str, reference_id: str
    ) -> None: ...


class RecordingSubjectControl:
    """In-memory SubjectControlPort: records calls, tracks frozen subjects."""

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[tuple[str, str, str, str, str]] = []
        self.frozen: set[tuple[str, str]] = set()
        self.halted: set[tuple[str, str]] = set()
        self.payouts_frozen: set[tuple[str, str]] = set()
        self.fail = fail

    def _record(self, op: str, subject_type: str, subject_id: str, reason: str, ref: str) -> None:
        if self.fail:
            raise ConnectionError(f"simulated kernel outage during {op}")
        self.calls.append((op, subject_type, subject_id, reason, ref))

    def freeze_subject(
        self, subject_type: str, subject_id: str, *, reason: str, reference_id: str
    ) -> None:
        self._record("freeze_subject", subject_type, subject_id, reason, reference_id)
        self.frozen.add((subject_type, subject_id))

    def unfreeze_subject(
        self, subject_type: str, subject_id: str, *, reason: str, reference_id: str
    ) -> None:
        self._record("unfreeze_subject", subject_type, subject_id, reason, reference_id)
        self.frozen.discard((subject_type, subject_id))
        self.halted.discard((subject_type, subject_id))
        self.payouts_frozen.discard((subject_type, subject_id))

    def halt_onboarding(
        self, subject_type: str, subject_id: str, *, reason: str, reference_id: str
    ) -> None:
        self._record("halt_onboarding", subject_type, subject_id, reason, reference_id)
        self.halted.add((subject_type, subject_id))

    def freeze_payouts(
        self, subject_type: str, subject_id: str, *, reason: str, reference_id: str
    ) -> None:
        self._record("freeze_payouts", subject_type, subject_id, reason, reference_id)
        self.payouts_frozen.add((subject_type, subject_id))

    def is_frozen(self, subject_type: str, subject_id: str) -> bool:
        return (subject_type, subject_id) in self.frozen

    def is_onboarding_halted(self, subject_type: str, subject_id: str) -> bool:
        return (subject_type, subject_id) in self.halted

    def is_payouts_frozen(self, subject_type: str, subject_id: str) -> bool:
        return (subject_type, subject_id) in self.payouts_frozen


@dataclass(frozen=True)
class AmlAlert:
    """One aml_alerts row (migration 0071)."""

    alert_id: str
    subject_type: str  # Customer | Merchant
    subject_id: str
    rule_id: str
    rule_pack_id: str
    status: str
    risk_tier: str
    raised_at: datetime
    idempotency_key: str
    triggered_amount_minor: int | None = None
    currency: str = "BDT"
    contributing_payment_ids: tuple[str, ...] = ()
    notes: str | None = None
    assigned_to: str | None = None
    triaged_at: datetime | None = None
    case_opened_at: datetime | None = None
    str_report_id: str | None = None
    closed_at: datetime | None = None
    close_reason: str | None = None
    freeze_applied: bool = False
    escalation_count: int = 0
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.status not in _STATES:
            raise ValueError(f"status must be one of {_STATES}, got {self.status!r}")
        if self.risk_tier not in _RISK_TIERS:
            raise ValueError(f"risk_tier must be one of {_RISK_TIERS}, got {self.risk_tier!r}")
        if self.subject_type not in ("Customer", "Merchant"):
            raise ValueError(f"subject_type must be Customer|Merchant, got {self.subject_type!r}")
        if self.triggered_amount_minor is not None and (
            isinstance(self.triggered_amount_minor, bool)
            or not isinstance(self.triggered_amount_minor, int)
        ):
            raise ValueError("triggered_amount_minor must be int paisa or None")

    @property
    def is_terminal(self) -> bool:
        return self.status in _TERMINAL


@runtime_checkable
class AlertStore(Protocol):
    """Storage contract for aml_alerts."""

    def insert(self, alert: AmlAlert) -> None: ...

    def save(self, alert: AmlAlert) -> None: ...

    def get(self, alert_id: str) -> AmlAlert | None: ...

    def by_idempotency_key(self, idempotency_key: str) -> AmlAlert | None: ...

    def by_status(self, status: str) -> list[AmlAlert]: ...

    def by_subject(self, subject_type: str, subject_id: str) -> list[AmlAlert]: ...


class InMemoryAlertStore:
    """Deterministic in-memory alert store for unit tests."""

    def __init__(self) -> None:
        self._rows: dict[str, AmlAlert] = {}
        self._order: list[str] = []
        self._by_idem: dict[str, str] = {}

    def insert(self, alert: AmlAlert) -> None:
        if alert.alert_id in self._rows:
            raise ConflictError(f"alert {alert.alert_id} already exists")
        if alert.idempotency_key in self._by_idem:
            raise ConflictError(
                f"alert idempotency_key {alert.idempotency_key} already exists",
                code="duplicate_alert",
            )
        self._rows[alert.alert_id] = alert
        self._order.append(alert.alert_id)
        self._by_idem[alert.idempotency_key] = alert.alert_id

    def save(self, alert: AmlAlert) -> None:
        if alert.alert_id not in self._rows:
            raise ConflictError(f"unknown alert {alert.alert_id}")
        self._rows[alert.alert_id] = alert

    def get(self, alert_id: str) -> AmlAlert | None:
        return self._rows.get(alert_id)

    def by_idempotency_key(self, idempotency_key: str) -> AmlAlert | None:
        alert_id = self._by_idem.get(idempotency_key)
        return self._rows.get(alert_id) if alert_id else None

    def by_status(self, status: str) -> list[AmlAlert]:
        return [self._rows[a] for a in self._order if self._rows[a].status == status]

    def by_subject(self, subject_type: str, subject_id: str) -> list[AmlAlert]:
        return [
            self._rows[a]
            for a in self._order
            if self._rows[a].subject_type == subject_type
            and self._rows[a].subject_id == subject_id
        ]


class AmlAlertService:
    """The AmlAlert FSM engine — refusal-first, audited, outbox-composed."""

    def __init__(
        self,
        store: AlertStore,
        *,
        clock: Clock,
        audit: AuditPort,
        outbox: OutboxPort,
        approvals: ApprovalPort,
        subject_control: SubjectControlPort,
        thresholds: ComplianceThresholds,
    ) -> None:
        self._store = store
        self._clock = clock
        self._audit = audit
        self._outbox = outbox
        self._approvals = approvals
        self._control = subject_control
        self._thresholds = thresholds

    # -- creation ---------------------------------------------------------------

    def raise_alert(
        self,
        *,
        subject_type: str,
        subject_id: str,
        rule_id: str,
        rule_pack_id: str,
        risk_tier: str,
        window_start: datetime,
        contributing_payment_ids: Sequence[str] = (),
        triggered_amount_minor: int | None = None,
        notes: str | None = None,
        conn: Any | None = None,
    ) -> AmlAlert:
        """Create a RAISED alert; idempotent on (rule_id, subject_id, window).

        A duplicate event replay returns the existing alert unchanged —
        at-least-once delivery never produces duplicate aml_alerts rows.
        """
        idempotency_key = make_id(
            "aml",
            {"rule_id": rule_id, "subject_id": subject_id, "window_start": window_start},
        )
        existing = self._store.by_idempotency_key(idempotency_key)
        if existing is not None:
            return existing
        now = self._clock.now()
        alert_id = make_id(
            "aml",
            {
                "rule_id": rule_id,
                "subject_id": subject_id,
                "subject_type": subject_type,
                "raised_at": now,
                "contributing_payment_ids": list(contributing_payment_ids),
            },
        )
        alert = AmlAlert(
            alert_id=alert_id,
            subject_type=subject_type,
            subject_id=subject_id,
            rule_id=rule_id,
            rule_pack_id=rule_pack_id,
            status="RAISED",
            risk_tier=risk_tier,
            raised_at=now,
            idempotency_key=idempotency_key,
            triggered_amount_minor=triggered_amount_minor,
            contributing_payment_ids=tuple(contributing_payment_ids),
            notes=notes,
        )
        self._store.insert(alert)
        self._audit.append(
            AuditEventSpec(
                event_type="AML_ALERT_RAISED",
                actor_id=PRODUCER,
                subject_type="AmlAlert",
                subject_id=alert_id,
                from_state=None,
                to_state="RAISED",
                payload={"rule_id": rule_id, "risk_tier": risk_tier},
            ),
            clock=self._clock,
            conn=conn,
        )
        self._outbox.enqueue(
            build_event(
                event_type="aml_alert.raised",
                subject_type="AmlAlert",
                subject_id=alert_id,
                producer=PRODUCER,
                topic="aml.events",
                payload={
                    "alert_id": alert_id,
                    "rule_id": rule_id,
                    "subject_id": subject_id,
                    "risk_tier": risk_tier,
                },
                occurred_at=now,
            ),
            conn=conn,
        )
        return alert

    # -- guards -------------------------------------------------------------------

    def _require(self, alert_id: str) -> AmlAlert:
        alert = self._store.get(alert_id)
        if alert is None:
            raise InvalidRequestError(f"unknown alert {alert_id}", code="alert_not_found")
        return alert

    @staticmethod
    def _deny_unless(alert: AmlAlert, expected: tuple[str, ...], trigger: str) -> None:
        if alert.status not in expected:
            raise ConflictError(
                f"transition {trigger!r} from state {alert.status!r} is DENIED "
                "(refusal-first: not in the spec/06 transition table)",
                code="fsm_transition_denied",
            )

    @staticmethod
    def _require_role(actor_role: str, allowed: tuple[str, ...], trigger: str) -> None:
        if actor_role not in allowed:
            raise AuthorizationError(
                f"role {actor_role!r} may not perform {trigger!r}; requires one of {allowed}"
            )

    def _transition_effects(
        self,
        alert: AmlAlert,
        *,
        event_type: str,
        actor_id: str,
        from_state: str,
        payload: Mapping[str, object],
        conn: Any | None,
    ) -> None:
        now = self._clock.now()
        self._audit.append(
            AuditEventSpec(
                event_type="AML_ALERT_STATE_TRANSITION",
                actor_id=actor_id,
                subject_type="AmlAlert",
                subject_id=alert.alert_id,
                from_state=from_state,
                to_state=alert.status,
                payload=payload,
            ),
            clock=self._clock,
            conn=conn,
        )
        self._outbox.enqueue(
            build_event(
                event_type=event_type,
                subject_type="AmlAlert",
                subject_id=alert.alert_id,
                producer=PRODUCER,
                topic="aml.events",
                payload={"alert_id": alert.alert_id, **payload},
                occurred_at=now,
            ),
            conn=conn,
        )

    # -- transitions ----------------------------------------------------------------

    def triage(
        self,
        alert_id: str,
        *,
        actor_id: str,
        actor_role: str,
        assigned_to: str,
        notes: str | None = None,
        conn: Any | None = None,
    ) -> AmlAlert:
        """RAISED --[triage_started / analyst-or-camlco]--> TRIAGING."""
        alert = self._require(alert_id)
        self._deny_unless(alert, ("RAISED",), "triage_started")
        self._require_role(actor_role, _ANALYST_ROLES, "triage_started")
        now = self._clock.now()
        updated = replace(
            alert,
            status="TRIAGING",
            assigned_to=assigned_to,
            triaged_at=now,
            notes=notes if notes is not None else alert.notes,
        )
        self._store.save(updated)
        self._transition_effects(
            updated,
            event_type="aml_alert.triaged",
            actor_id=actor_id,
            from_state="RAISED",
            payload={"assigned_to": assigned_to},
            conn=conn,
        )
        return updated

    def open_case(
        self,
        alert_id: str,
        *,
        actor_id: str,
        actor_role: str,
        case_notes: str,
        freeze_subject: bool = False,
        conn: Any | None = None,
    ) -> AmlAlert:
        """TRIAGING --[case_opened / >=1 contributing payment]--> IN_CASE."""
        alert = self._require(alert_id)
        self._deny_unless(alert, ("TRIAGING",), "case_opened")
        self._require_role(actor_role, _ANALYST_ROLES, "case_opened")
        if len(alert.contributing_payment_ids) < 1:
            raise ConflictError(
                "case_opened guard failed: alert has no contributing payments",
                code="fsm_guard_failed",
            )
        if freeze_subject:
            # Fail-closed: if the kernel freeze raises, this whole transition
            # aborts before any state is saved.
            self._control.freeze_subject(
                alert.subject_type,
                alert.subject_id,
                reason="AML_CASE",
                reference_id=alert.alert_id,
            )
        now = self._clock.now()
        updated = replace(
            alert,
            status="IN_CASE",
            case_opened_at=now,
            notes=case_notes,
            freeze_applied=alert.freeze_applied or freeze_subject,
        )
        self._store.save(updated)
        self._transition_effects(
            updated,
            event_type="aml_alert.case_opened",
            actor_id=actor_id,
            from_state="TRIAGING",
            payload={"freeze_applied": updated.freeze_applied},
            conn=conn,
        )
        if freeze_subject:
            # Settlement engine consumes this on payment.events (spec/06).
            for pi_id in alert.contributing_payment_ids:
                self._outbox.enqueue(
                    build_event(
                        event_type="payment_intent.aml_hold_placed",
                        subject_type="PaymentIntent",
                        subject_id=pi_id,
                        producer=PRODUCER,
                        topic="payment.events",
                        payload={"payment_intent_id": pi_id, "alert_id": alert.alert_id},
                        occurred_at=now,
                    ),
                    conn=conn,
                )
        return updated

    def mark_str_filed(
        self,
        alert_id: str,
        *,
        actor_id: str,
        actor_role: str,
        str_report_id: str,
        camlco_declaration: bool,
        conn: Any | None = None,
    ) -> AmlAlert:
        """IN_CASE --[str_filed / CAMLCO + declaration]--> STR_FILED (terminal)."""
        alert = self._require(alert_id)
        self._deny_unless(alert, ("IN_CASE",), "str_filed")
        self._require_role(actor_role, ("CAMLCO",), "str_filed")
        if camlco_declaration is not True:
            raise InvalidRequestError(
                "camlco_declaration must be true to file an STR",
                code="camlco_declaration_required",
            )
        updated = replace(alert, status="STR_FILED", str_report_id=str_report_id)
        self._store.save(updated)
        self._transition_effects(
            updated,
            event_type="aml_alert.str_filed",
            actor_id=actor_id,
            from_state="IN_CASE",
            payload={"str_report_id": str_report_id},
            conn=conn,
        )
        return updated

    def close(
        self,
        alert_id: str,
        *,
        actor_id: str,
        actor_role: str,
        close_reason: str,
        notes: str | None = None,
        conn: Any | None = None,
    ) -> AmlAlert | Mapping[str, object]:
        """Close an alert. Mandatory reason. Four-eyes when state is IN_CASE.

        From TRIAGING: closes immediately. From IN_CASE: creates an
        ``aml_case_close`` ApprovalRequest and returns
        ``{"approval_request_id": ...}`` — the close completes only via
        :meth:`complete_in_case_close` after a distinct CAMLCO approves.
        """
        alert = self._require(alert_id)
        self._deny_unless(alert, ("TRIAGING", "IN_CASE"), "alert_closed")
        self._require_role(actor_role, _ANALYST_ROLES, "alert_closed")
        if close_reason not in _CLOSE_REASONS:
            raise InvalidRequestError(
                f"close_reason must be one of {_CLOSE_REASONS} (mandatory)",
                code="close_reason_required",
            )
        if alert.status == "IN_CASE":
            record = self._approvals.request(
                action_type="aml_case_close",
                subject_type="AmlAlert",
                subject_id=alert.alert_id,
                payload={"close_reason": close_reason, "notes": notes or ""},
                reason=f"close IN_CASE alert: {close_reason}",
                initiator_id=actor_id,
            )
            return {"approval_request_id": record.approval_request_id}
        return self._finalize_close(
            alert, actor_id=actor_id, close_reason=close_reason, notes=notes, conn=conn
        )

    def complete_in_case_close(
        self,
        alert_id: str,
        *,
        approval_request_id: str,
        actor_id: str,
        conn: Any | None = None,
    ) -> AmlAlert:
        """Approval callback: complete an IN_CASE close after CAMLCO approval."""
        alert = self._require(alert_id)
        self._deny_unless(alert, ("IN_CASE",), "alert_closed")
        record = self._approvals.get(approval_request_id)
        if record is None or record.state != "APPROVED":
            raise ConflictError(
                "IN_CASE close requires an APPROVED aml_case_close ApprovalRequest "
                "(four-eyes; spec/06 cut-last #4)",
                code="approval_not_granted",
            )
        if record.subject_id != alert.alert_id or record.action_type != "aml_case_close":
            raise ConflictError(
                "approval request does not match this alert", code="approval_mismatch"
            )
        close_reason = str(record.payload.get("close_reason", "OTHER"))
        notes = str(record.payload.get("notes", "")) or None
        return self._finalize_close(
            alert, actor_id=actor_id, close_reason=close_reason, notes=notes, conn=conn
        )

    def _finalize_close(
        self,
        alert: AmlAlert,
        *,
        actor_id: str,
        close_reason: str,
        notes: str | None,
        conn: Any | None,
    ) -> AmlAlert:
        from_state = alert.status
        now = self._clock.now()
        if alert.freeze_applied:
            self._control.unfreeze_subject(
                alert.subject_type,
                alert.subject_id,
                reason="AML_CASE_CLOSED",
                reference_id=alert.alert_id,
            )
        updated = replace(
            alert,
            status="CLOSED",
            closed_at=now,
            close_reason=close_reason,
            notes=notes if notes is not None else alert.notes,
            freeze_applied=False,
        )
        self._store.save(updated)
        self._transition_effects(
            updated,
            event_type="aml_alert.closed",
            actor_id=actor_id,
            from_state=from_state,
            payload={"close_reason": close_reason},
            conn=conn,
        )
        if alert.freeze_applied:
            for pi_id in alert.contributing_payment_ids:
                self._outbox.enqueue(
                    build_event(
                        event_type="payment_intent.aml_hold_released",
                        subject_type="PaymentIntent",
                        subject_id=pi_id,
                        producer=PRODUCER,
                        topic="payment.events",
                        payload={"payment_intent_id": pi_id, "alert_id": alert.alert_id},
                        occurred_at=now,
                    ),
                    conn=conn,
                )
        return updated

    # -- TTL escalation sweep (scheduler-driven; state never changes) ---------------

    def run_escalations(self, *, conn: Any | None = None) -> list[str]:
        """15-minute scheduler tick: RAISED>24h and TRIAGING>72h notify CAMLCO.

        Returns the alert ids escalated this tick. State is unchanged; the
        escalation_count increments and an audit row records the notification
        (spec/06 ttl_24h_escalation / ttl_72h_escalation rows).
        """
        now = self._clock.now()
        escalated: list[str] = []
        sweeps = (
            ("RAISED", timedelta(hours=self._thresholds.raised_escalation_hours), 0),
            ("TRIAGING", timedelta(hours=self._thresholds.triaging_escalation_hours), 1),
        )
        for status, age_limit, min_count in sweeps:
            for alert in self._store.by_status(status):
                anchor = alert.triaged_at if status == "TRIAGING" else alert.raised_at
                if anchor is None or now - anchor <= age_limit:
                    continue
                if alert.escalation_count > min_count:
                    continue  # already notified for this stage
                updated = replace(alert, escalation_count=alert.escalation_count + 1)
                self._store.save(updated)
                event_name = (
                    "aml_alert.escalation_notified"
                    if status == "RAISED"
                    else "aml_alert.second_escalation_notified"
                )
                self._audit.append(
                    AuditEventSpec(
                        event_type=event_name,
                        actor_id="scheduler@1",
                        subject_type="AmlAlert",
                        subject_id=alert.alert_id,
                        from_state=status,
                        to_state=status,
                        payload={"escalation_count": updated.escalation_count},
                    ),
                    clock=self._clock,
                    conn=conn,
                )
                escalated.append(alert.alert_id)
        return escalated

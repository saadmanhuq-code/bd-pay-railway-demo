"""Two-eyes ApprovalGateway — the spec/15 ApprovalRequest FSM, platform-owned.

Ported from ``dse-profit-engine/src/dse_engine/pod/approval.py`` (ADAPT, per
PORTING-MAP.md): retained — the gateway engine shape (single decision path,
per-row terminal transitions, fail-closed gates before any approval, decisions
that leave no half-written state), and the fail-closed test discipline from
``tests/pod/test_approval_fail_closed.py`` (unknown-is-refused, terminal
states never regress, double-decision impossible). Replaced — trade-plan rows
become ``approval_requests`` (migration 0003), NAV/loss thresholds become the
BDT action catalogue from spec/15 with the refund threshold sourced from
:class:`bdpay.platform.config.Settings`.

FSM (spec/15, refusal-first — any transition not listed is DENIED)::

    PENDING_SECOND_APPROVER --[approve / approver != initiator]--> APPROVED
    PENDING_SECOND_APPROVER --[reject  / approver != initiator]--> REJECTED
    PENDING_SECOND_APPROVER --[withdraw / actor == initiator]----> WITHDRAWN
    PENDING_SECOND_APPROVER --[ttl 24h]--------------------------> EXPIRED
    APPROVED --[callback_failed]--> APPROVED_EXECUTION_FAILED
    APPROVED_EXECUTION_FAILED --[callback_retry succeeded]--> APPROVED

Hard rules:

- The requester can NEVER approve their own request (app check here AND the
  DB CHECK in migration 0003 — defence in depth).
- Expiry is fail-closed: the underlying action simply never happens.
- Every request and every decision writes an audit event through the injected
  :class:`~bdpay.platform.interfaces.AuditPort`.
- One live request per (action_type, subject_id) — no racing duplicates.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, runtime_checkable

from bdpay.platform.canonical import sha256_canonical
from bdpay.platform.clock import Clock
from bdpay.platform.config import Settings
from bdpay.platform.ids import make_id
from bdpay.platform.interfaces import AuditEventSpec, AuditPort

__all__ = [
    "ACTION_CATALOGUE",
    "APPROVAL_TTL_HOURS",
    "ApprovalError",
    "ApprovalGateway",
    "ApprovalRecord",
    "ApprovalRule",
    "ApprovalStore",
    "InMemoryApprovalStore",
    "PostgresApprovalStore",
    "build_action_catalogue",
]

APPROVAL_TTL_HOURS = 24

STATES = (
    "PENDING_SECOND_APPROVER",
    "APPROVED",
    "APPROVED_EXECUTION_FAILED",
    "REJECTED",
    "EXPIRED",
    "WITHDRAWN",
)
_LIVE_STATES = ("PENDING_SECOND_APPROVER", "APPROVED_EXECUTION_FAILED")
_TERMINAL_STATES = ("REJECTED", "EXPIRED", "WITHDRAWN")


class ApprovalError(ValueError):
    """Raised on any denied approval operation (refusal-first)."""


@dataclass(frozen=True)
class ApprovalRule:
    """Catalogue entry: when does this action need a second pair of eyes?

    ``threshold_minor is None`` means ALWAYS. With a threshold, amounts
    strictly above it require two-eyes (spec/15: refund "> BDT 50,000");
    a missing amount on a threshold-gated action fails closed to required.
    """

    threshold_minor: int | None = None


def build_action_catalogue(settings: Settings | None = None) -> dict[str, ApprovalRule]:
    """The binding spec/15 four-eyes action catalogue with config thresholds."""
    refund_threshold = (
        settings.refund_two_eyes_threshold_minor if settings is not None else 5_000_000
    )
    always = ApprovalRule()
    return {
        "refund_over_threshold": ApprovalRule(threshold_minor=refund_threshold),
        "merchant_activation": always,
        "merchant_edd_clearance": always,
        "kyc_manual_review": always,
        "settlement_batch_retry_large": ApprovalRule(threshold_minor=50_000_000),
        "settlement_release_override": always,
        "ledger_adjustment": ApprovalRule(threshold_minor=50_000),
        "mdr_fee_rule_change": always,
        "aml_case_close": always,
        "str_withdrawal": always,
        "sanctions_clear_or_unfreeze": always,
        "camlco_freeze": always,
        "camlco_unfreeze": always,
        "rule_pack_promotion": always,
        "signing_key_ceremony": always,
        "connector_mode_to_production": always,
        "connector_manual_result": always,
        "rail_dictionary_activation": always,
        "npsb_session_manual_action": always,
        "compensation_resolution": always,
        "dispute_resolution_money": always,
        "bulk_disbursement_release": always,
        "operator_create_or_role_change": always,
        "bb_incident_report_release": always,
    }


#: Default catalogue with the binding default thresholds (settings override).
ACTION_CATALOGUE: dict[str, ApprovalRule] = build_action_catalogue()


@dataclass(frozen=True)
class ApprovalRecord:
    """One approval_requests row (spec/15 DDL / migration 0003)."""

    approval_request_id: str
    action_type: str
    subject_type: str
    subject_id: str
    payload: Mapping[str, object]
    payload_hash: str
    reason: str
    state: str
    initiator_id: str
    expires_at: datetime
    created_at: datetime
    updated_at: datetime
    approver_id: str | None = None
    decision_reason: str | None = None
    threshold_minor: int | None = None
    decided_at: datetime | None = None
    executed_at: datetime | None = None
    schema_version: int = 1


@runtime_checkable
class ApprovalStore(Protocol):
    """Storage contract for approval requests (migration 0003)."""

    def insert(self, record: ApprovalRecord) -> None: ...

    def save(self, record: ApprovalRecord) -> None: ...

    def get(self, approval_request_id: str) -> ApprovalRecord | None: ...

    def live_request(self, action_type: str, subject_id: str) -> ApprovalRecord | None: ...

    def pending_expired(self, *, now: datetime) -> list[ApprovalRecord]: ...


class InMemoryApprovalStore:
    """Deterministic in-memory store for unit tests."""

    def __init__(self) -> None:
        self._rows: dict[str, ApprovalRecord] = {}

    def insert(self, record: ApprovalRecord) -> None:
        if record.approval_request_id in self._rows:
            raise ApprovalError(
                f"approval request {record.approval_request_id!r} already exists"
            )
        live = self.live_request(record.action_type, record.subject_id)
        if live is not None:
            raise ApprovalError(
                f"a live approval request already exists for "
                f"({record.action_type!r}, {record.subject_id!r}): "
                f"{live.approval_request_id}"
            )
        self._rows[record.approval_request_id] = record

    def save(self, record: ApprovalRecord) -> None:
        if record.approval_request_id not in self._rows:
            raise ApprovalError(f"unknown approval request {record.approval_request_id!r}")
        self._rows[record.approval_request_id] = record

    def get(self, approval_request_id: str) -> ApprovalRecord | None:
        return self._rows.get(approval_request_id)

    def live_request(self, action_type: str, subject_id: str) -> ApprovalRecord | None:
        for row in self._rows.values():
            if (
                row.action_type == action_type
                and row.subject_id == subject_id
                and row.state in _LIVE_STATES
            ):
                return row
        return None

    def pending_expired(self, *, now: datetime) -> list[ApprovalRecord]:
        return sorted(
            (
                row
                for row in self._rows.values()
                if row.state == "PENDING_SECOND_APPROVER" and row.expires_at <= now
            ),
            key=lambda row: row.created_at,
        )


_APPR_COLUMNS = (
    "approval_request_id, action_type, subject_type, subject_id, payload, payload_hash, "
    "reason, state, initiator_id, approver_id, decision_reason, threshold_minor, "
    "expires_at, decided_at, executed_at, created_at, updated_at, schema_version"
)


class PostgresApprovalStore:
    """psycopg3 store matching ``db/migrations/0003_approval_requests.sql``."""

    def __init__(self, connection_factory: Callable[[], Any]) -> None:
        self._connect = connection_factory

    @staticmethod
    def _row_to_record(row: Mapping[str, Any]) -> ApprovalRecord:
        return ApprovalRecord(
            approval_request_id=row["approval_request_id"],
            action_type=row["action_type"],
            subject_type=row["subject_type"],
            subject_id=row["subject_id"],
            payload=row["payload"],
            payload_hash=row["payload_hash"],
            reason=row["reason"],
            state=row["state"],
            initiator_id=row["initiator_id"],
            approver_id=row["approver_id"],
            decision_reason=row["decision_reason"],
            threshold_minor=row["threshold_minor"],
            expires_at=row["expires_at"],
            decided_at=row["decided_at"],
            executed_at=row["executed_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            schema_version=row["schema_version"],
        )

    @staticmethod
    def _jsonb(payload: Mapping[str, object]) -> Any:
        from psycopg.types.json import Jsonb

        from bdpay.platform.canonical import canonical_json

        return Jsonb(dict(payload), dumps=lambda o: canonical_json(o).decode("utf-8"))

    def insert(self, record: ApprovalRecord) -> None:
        import psycopg

        sql = (
            f"INSERT INTO approval_requests ({_APPR_COLUMNS}) VALUES "
            "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
        )
        params = (
            record.approval_request_id,
            record.action_type,
            record.subject_type,
            record.subject_id,
            self._jsonb(record.payload),
            record.payload_hash,
            record.reason,
            record.state,
            record.initiator_id,
            record.approver_id,
            record.decision_reason,
            record.threshold_minor,
            record.expires_at,
            record.decided_at,
            record.executed_at,
            record.created_at,
            record.updated_at,
            record.schema_version,
        )
        with self._connect() as conn:
            try:
                conn.execute(sql, params)
                conn.commit()
            except psycopg.errors.UniqueViolation as exc:
                conn.rollback()
                raise ApprovalError(
                    f"a live approval request already exists for "
                    f"({record.action_type!r}, {record.subject_id!r})"
                ) from exc

    def save(self, record: ApprovalRecord) -> None:
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE approval_requests SET state = %s, approver_id = %s, "
                "decision_reason = %s, decided_at = %s, executed_at = %s, "
                "updated_at = %s WHERE approval_request_id = %s",
                (
                    record.state,
                    record.approver_id,
                    record.decision_reason,
                    record.decided_at,
                    record.executed_at,
                    record.updated_at,
                    record.approval_request_id,
                ),
            )
            if cur.rowcount != 1:
                conn.rollback()
                raise ApprovalError(
                    f"unknown approval request {record.approval_request_id!r}"
                )
            conn.commit()

    def get(self, approval_request_id: str) -> ApprovalRecord | None:
        from psycopg.rows import dict_row

        with self._connect() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    f"SELECT {_APPR_COLUMNS} FROM approval_requests "
                    "WHERE approval_request_id = %s",
                    (approval_request_id,),
                )
                row = cur.fetchone()
        return None if row is None else self._row_to_record(row)

    def live_request(self, action_type: str, subject_id: str) -> ApprovalRecord | None:
        from psycopg.rows import dict_row

        with self._connect() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    f"SELECT {_APPR_COLUMNS} FROM approval_requests "
                    "WHERE action_type = %s AND subject_id = %s "
                    "AND state = ANY(%s) LIMIT 1",
                    (action_type, subject_id, list(_LIVE_STATES)),
                )
                row = cur.fetchone()
        return None if row is None else self._row_to_record(row)

    def pending_expired(self, *, now: datetime) -> list[ApprovalRecord]:
        from psycopg.rows import dict_row

        with self._connect() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    f"SELECT {_APPR_COLUMNS} FROM approval_requests "
                    "WHERE state = 'PENDING_SECOND_APPROVER' AND expires_at <= %s "
                    "ORDER BY created_at",
                    (now,),
                )
                rows = cur.fetchall()
        return [self._row_to_record(row) for row in rows]


def _require_aware(value: datetime, what: str) -> datetime:
    if not isinstance(value, datetime):
        raise ApprovalError(f"{what} must be datetime, got {type(value).__name__}")
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ApprovalError(f"{what} must be timezone-aware (naive rejected)")
    return value.astimezone(UTC)


class ApprovalGateway:
    """Two-eyes request/decide engine (implements ApprovalPort structurally)."""

    def __init__(
        self,
        store: ApprovalStore,
        *,
        clock: Clock,
        audit: AuditPort,
        settings: Settings | None = None,
        catalogue: Mapping[str, ApprovalRule] | None = None,
        ttl_hours: int = APPROVAL_TTL_HOURS,
        logger: logging.Logger | None = None,
    ) -> None:
        if ttl_hours <= 0:
            raise ValueError("ttl_hours must be positive")
        self._store = store
        self._clock = clock
        self._audit = audit
        self._catalogue = dict(
            catalogue if catalogue is not None else build_action_catalogue(settings)
        )
        self._ttl = timedelta(hours=ttl_hours)
        self._log = logger or logging.getLogger("bdpay.platform.approval")

    # -- catalogue -------------------------------------------------------------

    def requires_two_eyes(
        self, action_type: str, *, amount_minor: int | None = None
    ) -> bool:
        """Fail-closed threshold check against the action catalogue.

        Unknown action types are refused outright — an unreviewed action must
        not route around the gate (the dse 'UNKNOWN family' lesson).
        """
        rule = self._catalogue.get(action_type)
        if rule is None:
            raise ApprovalError(
                f"unknown approval action_type {action_type!r}; the catalogue is "
                "closed (spec/15) and unreviewed actions are refused"
            )
        if rule.threshold_minor is None:
            return True
        if amount_minor is None:
            return True  # threshold-gated action with no amount: fails closed
        if isinstance(amount_minor, bool) or not isinstance(amount_minor, int):
            raise ApprovalError(
                f"amount_minor must be int paisa, got {type(amount_minor).__name__}"
            )
        return amount_minor > rule.threshold_minor

    # -- FSM entrypoints ---------------------------------------------------------

    def request(
        self,
        *,
        action_type: str,
        subject_type: str,
        subject_id: str,
        payload: Mapping[str, object],
        reason: str,
        initiator_id: str,
        amount_minor: int | None = None,
    ) -> ApprovalRecord:
        """Create a PENDING_SECOND_APPROVER request (audited)."""
        rule = self._catalogue.get(action_type)
        if rule is None:
            raise ApprovalError(
                f"unknown approval action_type {action_type!r}; refused"
            )
        if not self.requires_two_eyes(action_type, amount_minor=amount_minor):
            raise ApprovalError(
                f"{action_type!r} at amount_minor={amount_minor} is below the "
                f"two-eyes threshold ({rule.threshold_minor}); no approval request "
                "is needed — proceed through the normal single-operator path"
            )
        for name, value in (
            ("subject_type", subject_type),
            ("subject_id", subject_id),
            ("reason", reason),
            ("initiator_id", initiator_id),
        ):
            if not isinstance(value, str) or not value:
                raise ApprovalError(f"{name} must be a non-empty string")
        live = self._store.live_request(action_type, subject_id)
        if live is not None:
            raise ApprovalError(
                f"a live approval request already exists for "
                f"({action_type!r}, {subject_id!r}): {live.approval_request_id}"
            )
        now = self._clock.now()
        payload_dict = dict(payload)
        payload_hash = sha256_canonical(payload_dict)
        approval_request_id = make_id(
            "appr",
            {
                "action_type": action_type,
                "subject_id": subject_id,
                "payload_hash": payload_hash,
                "created_at": now,
            },
        )
        record = ApprovalRecord(
            approval_request_id=approval_request_id,
            action_type=action_type,
            subject_type=subject_type,
            subject_id=subject_id,
            payload=payload_dict,
            payload_hash=payload_hash,
            reason=reason,
            state="PENDING_SECOND_APPROVER",
            initiator_id=initiator_id,
            approver_id=None,
            decision_reason=None,
            threshold_minor=rule.threshold_minor,  # catalogue snapshot at creation
            expires_at=now + self._ttl,
            created_at=now,
            updated_at=now,
        )
        self._store.insert(record)
        self._audit_event(
            "APPROVAL_REQUESTED",
            record,
            actor_id=initiator_id,
            from_state=None,
            to_state="PENDING_SECOND_APPROVER",
        )
        return record

    def decide(
        self,
        approval_request_id: str,
        *,
        approver_id: str,
        approve: bool,
        decision_reason: str | None = None,
    ) -> ApprovalRecord:
        """ApprovalPort facade over approve/reject."""
        if approve:
            return self.approve(
                approval_request_id,
                approver_id=approver_id,
                decision_reason=decision_reason,
            )
        return self.reject(
            approval_request_id,
            approver_id=approver_id,
            decision_reason=decision_reason or "rejected",
        )

    def approve(
        self,
        approval_request_id: str,
        *,
        approver_id: str,
        decision_reason: str | None = None,
    ) -> ApprovalRecord:
        """PENDING_SECOND_APPROVER -> APPROVED. Self-approval is denied."""
        record = self._get_pending(approval_request_id)
        if approver_id == record.initiator_id:
            self._audit_event(
                "APPROVAL_SELF_APPROVAL_DENIED",
                record,
                actor_id=approver_id,
                from_state=record.state,
                to_state=record.state,
            )
            raise ApprovalError(
                "the requester can never approve their own request (two-eyes rule)"
            )
        now = self._clock.now()
        updated = replace(
            record,
            state="APPROVED",
            approver_id=approver_id,
            decision_reason=decision_reason,
            decided_at=now,
            updated_at=now,
        )
        self._store.save(updated)
        self._audit_event(
            "APPROVAL_GRANTED",
            updated,
            actor_id=approver_id,
            from_state="PENDING_SECOND_APPROVER",
            to_state="APPROVED",
        )
        return updated

    def reject(
        self,
        approval_request_id: str,
        *,
        approver_id: str,
        decision_reason: str,
    ) -> ApprovalRecord:
        """PENDING_SECOND_APPROVER -> REJECTED (with mandatory reason)."""
        if not decision_reason:
            raise ApprovalError("a rejection requires a decision_reason")
        record = self._get_pending(approval_request_id)
        if approver_id == record.initiator_id:
            raise ApprovalError(
                "the initiator cannot decide their own request; use withdraw"
            )
        now = self._clock.now()
        updated = replace(
            record,
            state="REJECTED",
            approver_id=approver_id,
            decision_reason=decision_reason,
            decided_at=now,
            updated_at=now,
        )
        self._store.save(updated)
        self._audit_event(
            "APPROVAL_REJECTED",
            updated,
            actor_id=approver_id,
            from_state="PENDING_SECOND_APPROVER",
            to_state="REJECTED",
        )
        return updated

    def withdraw(self, approval_request_id: str, *, actor_id: str) -> ApprovalRecord:
        """PENDING_SECOND_APPROVER -> WITHDRAWN; only the initiator may."""
        record = self._get_pending(approval_request_id)
        if actor_id != record.initiator_id:
            raise ApprovalError("only the initiator can withdraw a request")
        now = self._clock.now()
        updated = replace(record, state="WITHDRAWN", decided_at=now, updated_at=now)
        self._store.save(updated)
        self._audit_event(
            "APPROVAL_WITHDRAWN",
            updated,
            actor_id=actor_id,
            from_state="PENDING_SECOND_APPROVER",
            to_state="WITHDRAWN",
        )
        return updated

    def expire_due(self) -> int:
        """TTL sweep: every overdue PENDING request -> EXPIRED (fail-closed).

        The underlying action simply never happens (spec/15). Returns the
        number of requests expired.
        """
        now = self._clock.now()
        expired = 0
        for record in self._store.pending_expired(now=now):
            updated = replace(record, state="EXPIRED", updated_at=now)
            self._store.save(updated)
            self._audit_event(
                "APPROVAL_EXPIRED",
                updated,
                actor_id="system:approval-ttl-sweep",
                from_state="PENDING_SECOND_APPROVER",
                to_state="EXPIRED",
            )
            expired += 1
        return expired

    def mark_execution_failed(
        self, approval_request_id: str, *, detail: str
    ) -> ApprovalRecord:
        """APPROVED -> APPROVED_EXECUTION_FAILED (callback raised; never silent)."""
        record = self._require(approval_request_id)
        if record.state != "APPROVED":
            raise ApprovalError(
                f"callback_failed is only legal from APPROVED, not {record.state}"
            )
        now = self._clock.now()
        updated = replace(
            record,
            state="APPROVED_EXECUTION_FAILED",
            decision_reason=detail,
            updated_at=now,
        )
        self._store.save(updated)
        self._audit_event(
            "APPROVAL_EXECUTION_FAILED",
            updated,
            actor_id="system:approval-callback",
            from_state="APPROVED",
            to_state="APPROVED_EXECUTION_FAILED",
        )
        return updated

    def mark_executed(self, approval_request_id: str) -> ApprovalRecord:
        """APPROVED | APPROVED_EXECUTION_FAILED -> APPROVED with executed_at set."""
        record = self._require(approval_request_id)
        if record.state not in ("APPROVED", "APPROVED_EXECUTION_FAILED"):
            raise ApprovalError(
                f"execution can only be recorded from APPROVED states, not {record.state}"
            )
        now = self._clock.now()
        updated = replace(record, state="APPROVED", executed_at=now, updated_at=now)
        self._store.save(updated)
        self._audit_event(
            "APPROVAL_EXECUTED",
            updated,
            actor_id="system:approval-callback",
            from_state=record.state,
            to_state="APPROVED",
        )
        return updated

    def get(self, approval_request_id: str) -> ApprovalRecord | None:
        return self._store.get(approval_request_id)

    # -- internals ----------------------------------------------------------------

    def _require(self, approval_request_id: str) -> ApprovalRecord:
        record = self._store.get(approval_request_id)
        if record is None:
            raise ApprovalError(f"unknown approval request {approval_request_id!r}")
        return record

    def _get_pending(self, approval_request_id: str) -> ApprovalRecord:
        record = self._require(approval_request_id)
        if record.state in _TERMINAL_STATES or record.state in (
            "APPROVED",
            "APPROVED_EXECUTION_FAILED",
        ):
            raise ApprovalError(
                f"request {approval_request_id!r} is {record.state}; "
                "decisions are single-shot (refusal-first)"
            )
        now = self._clock.now()
        if now >= _require_aware(record.expires_at, "expires_at"):
            updated = replace(record, state="EXPIRED", updated_at=now)
            self._store.save(updated)
            self._audit_event(
                "APPROVAL_EXPIRED",
                updated,
                actor_id="system:approval-ttl-sweep",
                from_state="PENDING_SECOND_APPROVER",
                to_state="EXPIRED",
            )
            raise ApprovalError(
                f"request {approval_request_id!r} expired at "
                f"{record.expires_at.isoformat()}; the action never happens (fail-closed)"
            )
        return record

    def _audit_event(
        self,
        event_type: str,
        record: ApprovalRecord,
        *,
        actor_id: str,
        from_state: str | None,
        to_state: str | None,
    ) -> None:
        self._audit.append(
            AuditEventSpec(
                event_type=event_type,
                actor_id=actor_id,
                subject_type="ApprovalRequest",
                subject_id=record.approval_request_id,
                from_state=from_state,
                to_state=to_state,
                payload={
                    "action_type": record.action_type,
                    "subject_type": record.subject_type,
                    "subject_id": record.subject_id,
                    "payload_hash": record.payload_hash,
                    "initiator_id": record.initiator_id,
                    "approver_id": record.approver_id,
                },
            ),
            clock=self._clock,
        )

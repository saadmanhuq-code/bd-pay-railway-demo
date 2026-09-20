"""ConnectorRegistry + ConnectorMode FSM (spec/10 §State Machines 1, §Data Model).

Modes: MANUAL_LOCAL / SIMULATOR / SANDBOX / PRODUCTION / DISABLED. Default
refusal-first: any transition not in the spec table is DENIED. Binding
fail-closed rule: on every credential sweep, a connector whose active mode is
SANDBOX or PRODUCTION with any unresolvable credential ref goes DISABLED
immediately; a connector is NEVER silently downgraded to SIMULATOR in lieu of
missing production credentials. Per-connector timeout/retry/breaker defaults
come from the spec/10 budget table (binding defaults).

Config-driven registration: deployment config names modes as
``simulator | sandbox | live | disabled`` (plus ``manual_local``);
``mode_from_config`` maps them onto the spec FSM states (``live`` ->
``PRODUCTION``). Health states (HEALTHY/DEGRADED/UNHEALTHY/UNKNOWN) advance
through ``record_health`` which appends a ``connector_health_samples`` row and
emits ``connector_registration.health_changed`` on a status delta. The
production deployment guard ``assert_simulator_allowed`` enforces the spec/10
failure-mode rule: a SIMULATOR-mode row with ``ALLOW_SIMULATOR=false`` refuses
to boot — fake rails can never serve production traffic.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum

from bdpay.connectors.breaker import BreakerConfig
from bdpay.connectors.ids_ext import make_connector_id
from bdpay.connectors.ports import (
    AuditSink,
    CredentialResolver,
    EventSink,
    InMemoryAuditSink,
    InMemoryEventSink,
    StaticCredentialResolver,
)
from bdpay.connectors.stores import (
    HealthSampleRecord,
    HealthSampleStore,
    InMemoryHealthSampleStore,
    InMemoryModeChangeStore,
    ModeChangeRecord,
    ModeChangeStore,
)
from bdpay.platform.clock import Clock
from bdpay.platform.errors import ConflictError, InvalidRequestError, NotFoundError

__all__ = [
    "BudgetDefaults",
    "ConnectorMode",
    "ConnectorRegistration",
    "ConnectorRegistry",
    "DEFAULT_BUDGETS",
    "GENERIC_BUDGET",
    "SimulatorForbiddenError",
    "TransitionDeniedError",
    "UnknownConnectorError",
    "breaker_config_for",
    "budget_for",
    "mode_from_config",
]

_SWEEP_ACTOR = "system:fail-closed-sweep"


class UnknownConnectorError(NotFoundError):
    default_code = "unknown_connector"


class TransitionDeniedError(ConflictError):
    """Refusal-first denial: the requested mode transition is not in the table."""

    default_code = "mode_transition_denied"


class SimulatorForbiddenError(ConflictError):
    """A SIMULATOR-mode connector is active in a deployment that forbids it."""

    default_code = "simulator_forbidden_in_production"


class ConnectorMode(StrEnum):
    MANUAL_LOCAL = "MANUAL_LOCAL"
    SIMULATOR = "SIMULATOR"
    SANDBOX = "SANDBOX"
    PRODUCTION = "PRODUCTION"
    DISABLED = "DISABLED"


#: Deployment-config mode names -> spec/10 FSM states (``live`` = PRODUCTION).
_MODE_BY_CONFIG_STRING: dict[str, ConnectorMode] = {
    "manual_local": ConnectorMode.MANUAL_LOCAL,
    "manual": ConnectorMode.MANUAL_LOCAL,
    "simulator": ConnectorMode.SIMULATOR,
    "sandbox": ConnectorMode.SANDBOX,
    "live": ConnectorMode.PRODUCTION,
    "production": ConnectorMode.PRODUCTION,
    "disabled": ConnectorMode.DISABLED,
}


def mode_from_config(value: str) -> ConnectorMode:
    """Map a config mode string (``simulator|sandbox|live|disabled``) to the FSM."""
    if not isinstance(value, str):
        raise InvalidRequestError(
            f"connector mode must be a string, got {type(value).__name__}",
            code="invalid_connector_mode",
        )
    mode = _MODE_BY_CONFIG_STRING.get(value.strip().lower())
    if mode is None:
        raise InvalidRequestError(
            f"unknown connector mode {value!r}; allowed: "
            + ", ".join(sorted(_MODE_BY_CONFIG_STRING)),
            code="invalid_connector_mode",
        )
    return mode


#: promote is strictly one rung up the ladder (spec/10 FSM table).
_PROMOTION_TARGET: dict[ConnectorMode, ConnectorMode] = {
    ConnectorMode.MANUAL_LOCAL: ConnectorMode.SIMULATOR,
    ConnectorMode.SIMULATOR: ConnectorMode.SANDBOX,
    ConnectorMode.SANDBOX: ConnectorMode.PRODUCTION,
}

#: demote is legal only from PRODUCTION (de-risking is always allowed).
_DEMOTION_TARGETS = frozenset(
    {ConnectorMode.SANDBOX, ConnectorMode.SIMULATOR, ConnectorMode.MANUAL_LOCAL}
)

_CREDENTIALED_MODES = frozenset({ConnectorMode.SANDBOX, ConnectorMode.PRODUCTION})


@dataclass(frozen=True)
class BudgetDefaults:
    """Binding per-connector defaults from the spec/10 budget table."""

    timeout_ms: int
    retry_budget: dict
    breaker: BreakerConfig


GENERIC_BUDGET = BudgetDefaults(
    timeout_ms=30_000,
    retry_budget={"submit_retries": 0, "poll_attempts": 3, "poll_interval_ms": 10_000},
    breaker=BreakerConfig(),
)

DEFAULT_BUDGETS: dict[str, BudgetDefaults] = {
    "npsb_iso8583_v28": BudgetDefaults(
        timeout_ms=30_000,
        retry_budget={
            "submit_retries": 0,
            "poll_attempts": 3,
            "poll_interval_ms": 10_000,
            "reversal_policy": "auto_reverse_after_poll_exhaustion",
        },
        breaker=BreakerConfig(failure_threshold=5, window_s=60),
    ),
    "beftn_batch_v2": BudgetDefaults(
        timeout_ms=120_000,
        retry_budget={
            "submit_retries": 2,
            "retry_scope": "transport_only_same_file_same_batch_id",
            "poll_attempts": 1,
            "poll_interval_ms": 0,
        },
        breaker=BreakerConfig(failure_threshold=3, window_s=300),
    ),
    "rtgs_iso20022_v1": BudgetDefaults(
        timeout_ms=60_000,
        retry_budget={
            "submit_retries": 0,
            "poll_attempts": 3,
            "poll_interval_ms": 30_000,
            "reversal_policy": "manual_ops_queue",
        },
        breaker=BreakerConfig(failure_threshold=3, window_s=120),
    ),
    "card_acquirer_v1": BudgetDefaults(
        timeout_ms=30_000,
        retry_budget={
            "submit_retries": 0,
            "poll_attempts": 3,
            "poll_interval_ms": 10_000,
            "reversal_policy": "auto_reverse_per_scheme_rules",
        },
        breaker=BreakerConfig(failure_threshold=5, window_s=60),
    ),
    "bkash_pgw_v2": BudgetDefaults(
        timeout_ms=15_000,
        retry_budget={"submit_retries": 0, "poll_attempts": 3, "poll_interval_ms": 5_000},
        breaker=BreakerConfig(failure_threshold=5, window_s=60),
    ),
    "nagad_pgw_v33": BudgetDefaults(
        timeout_ms=15_000,
        retry_budget={"submit_retries": 0, "poll_attempts": 3, "poll_interval_ms": 5_000},
        breaker=BreakerConfig(failure_threshold=5, window_s=60),
    ),
    "rocket_aggregator_v1": BudgetDefaults(
        timeout_ms=20_000,
        retry_budget={"submit_retries": 0, "poll_attempts": 3, "poll_interval_ms": 10_000},
        breaker=BreakerConfig(failure_threshold=5, window_s=60),
    ),
    "upay_rest_v1": BudgetDefaults(
        timeout_ms=15_000,
        retry_budget={"submit_retries": 0, "poll_attempts": 3, "poll_interval_ms": 5_000},
        breaker=BreakerConfig(failure_threshold=5, window_s=60),
    ),
    "porichoy_ekyc_v1": BudgetDefaults(
        timeout_ms=10_000,
        retry_budget={"submit_retries": 2, "retry_scope": "idempotent_read"},
        breaker=BreakerConfig(failure_threshold=5, window_s=60),
    ),
    "goaml_reporter_v1": BudgetDefaults(
        timeout_ms=60_000,
        retry_budget={
            "submit_retries": 0,
            "queue_retry": "unbounded_exp_backoff",
            "backoff_initial_ms": 60_000,
            "backoff_cap_ms": 3_600_000,
            "never_drop": True,
        },
        breaker=BreakerConfig(informational=True),
    ),
    "sanctions_feed_v1": BudgetDefaults(
        timeout_ms=300_000,
        retry_budget={"submit_retries": 3, "staleness_alarm_h": 24},
        breaker=BreakerConfig(failure_threshold=5, window_s=60),
    ),
    "hsm_thales_v1": BudgetDefaults(
        timeout_ms=5_000,
        retry_budget={"submit_retries": 0},
        breaker=BreakerConfig(failure_threshold=2, window_s=30),
    ),
    "sms_otp_v1": BudgetDefaults(
        timeout_ms=10_000,
        retry_budget={"submit_retries": 3, "retry_scope": "idempotent_per_event_recipient"},
        breaker=BreakerConfig(failure_threshold=10, window_s=60),
    ),
    "veridyn_p2_compliance_v1": BudgetDefaults(
        # spec/12 §H budget: 20s timeout, 0 retries (the caller re-asks),
        # breaker 5/60s informational — nothing waits on it synchronously.
        timeout_ms=20_000,
        retry_budget={"submit_retries": 0, "reask_policy": "caller_reasks"},
        breaker=BreakerConfig(failure_threshold=5, window_s=60, informational=True),
    ),
    "email_smtp_v1": BudgetDefaults(
        timeout_ms=30_000,
        retry_budget={"submit_retries": 3},
        breaker=BreakerConfig(failure_threshold=10, window_s=60),
    ),
}


def budget_for(connector_id: str) -> BudgetDefaults:
    return DEFAULT_BUDGETS.get(connector_id, GENERIC_BUDGET)


def breaker_config_for(connector_id: str) -> BreakerConfig:
    return budget_for(connector_id).breaker


@dataclass(frozen=True)
class ConnectorRegistration:
    """One ``connector_registry`` row (fields per the spec/10 DDL)."""

    registration_id: str
    connector_id: str
    display_name: str
    protocol: str
    capabilities: tuple[str, ...]
    supported_methods: tuple[str, ...]
    active_mode: ConnectorMode
    previous_mode: ConnectorMode | None
    enabled: bool
    adapter_version: str
    sdk_version: int
    health_status: str
    last_health_at: datetime | None
    certification_status: str
    last_certified_at: datetime | None
    config: dict
    config_schema: dict
    timeout_ms: int
    retry_budget: dict
    created_at: datetime
    updated_at: datetime
    schema_version: int = 1


_PROTOCOLS = frozenset(
    {
        "PAYMENT",
        "IDENTITY",
        "SANCTIONS",
        "AML_FILING",
        "SETTLEMENT_FILE",
        "HSM",
        "NOTIFICATION",
        # spec/12 §H "bespoke sidecar surface" (veridyn_p2_compliance_v1):
        # advisory/evidence tier only — never a payment protocol (LB12).
        "COMPLIANCE_SIDECAR",
    }
)


class ConnectorRegistry:
    """Authoritative connector catalogue with the refusal-first mode FSM."""

    def __init__(
        self,
        *,
        clock: Clock,
        credential_resolver: CredentialResolver | None = None,
        mode_changes: ModeChangeStore | None = None,
        health_samples: HealthSampleStore | None = None,
        audit: AuditSink | None = None,
        events: EventSink | None = None,
    ) -> None:
        self._clock = clock
        self._resolver = (
            credential_resolver if credential_resolver is not None else StaticCredentialResolver()
        )
        self._mode_changes = mode_changes if mode_changes is not None else InMemoryModeChangeStore()
        self._health = (
            health_samples if health_samples is not None else InMemoryHealthSampleStore()
        )
        self._audit = audit if audit is not None else InMemoryAuditSink()
        self._events = events if events is not None else InMemoryEventSink()
        self._registrations: dict[str, ConnectorRegistration] = {}

    # -- registration ----------------------------------------------------------

    def register(
        self,
        connector_id: str,
        *,
        display_name: str,
        protocol: str,
        capabilities: tuple[str, ...],
        supported_methods: tuple[str, ...] = (),
        adapter_version: str = "1.0.0",
        active_mode: ConnectorMode = ConnectorMode.MANUAL_LOCAL,
        config: dict | None = None,
        config_schema: dict | None = None,
        timeout_ms: int | None = None,
        retry_budget: dict | None = None,
        certification_status: str = "NEVER_RUN",
    ) -> ConnectorRegistration:
        if protocol not in _PROTOCOLS:
            raise ValueError(f"unknown protocol {protocol!r}")
        if connector_id in self._registrations:
            raise ConflictError(f"connector {connector_id} already registered")
        budget = budget_for(connector_id)
        now = self._clock.now()
        registration = ConnectorRegistration(
            registration_id=make_connector_id("creg", {"connector_id": connector_id}),
            connector_id=connector_id,
            display_name=display_name,
            protocol=protocol,
            capabilities=tuple(capabilities),
            supported_methods=tuple(supported_methods),
            active_mode=active_mode,
            previous_mode=None,
            enabled=active_mode is not ConnectorMode.DISABLED,
            adapter_version=adapter_version,
            sdk_version=1,
            health_status="UNKNOWN",
            last_health_at=None,
            certification_status=certification_status,
            last_certified_at=None,
            config=dict(config or {}),
            config_schema=dict(config_schema or {}),
            timeout_ms=timeout_ms if timeout_ms is not None else budget.timeout_ms,
            retry_budget=dict(retry_budget) if retry_budget is not None else dict(
                budget.retry_budget
            ),
            created_at=now,
            updated_at=now,
        )
        self._registrations[connector_id] = registration
        return registration

    def register_from_config(self, connector_id: str, config: dict) -> ConnectorRegistration:
        """Register from a deployment-config document; ``mode`` is mandatory.

        Mode strings are the deployment vocabulary (``simulator | sandbox |
        live | disabled``, plus ``manual_local``); everything else falls back
        to the spec/10 budget defaults when absent.
        """
        if "mode" not in config:
            raise InvalidRequestError(
                f"connector config for {connector_id} is missing 'mode'",
                code="invalid_connector_mode",
            )
        mode = mode_from_config(config["mode"])
        return self.register(
            connector_id,
            display_name=str(config.get("display_name", connector_id)),
            protocol=str(config.get("protocol", "PAYMENT")),
            capabilities=tuple(
                config.get("capabilities", ("submit", "query_status", "reverse", "health_check"))
            ),
            supported_methods=tuple(config.get("supported_methods", ())),
            adapter_version=str(config.get("adapter_version", "1.0.0")),
            active_mode=mode,
            config=config.get("config"),
            config_schema=config.get("config_schema"),
            timeout_ms=config.get("timeout_ms"),
            retry_budget=config.get("retry_budget"),
            certification_status=str(config.get("certification_status", "NEVER_RUN")),
        )

    def get(self, connector_id: str) -> ConnectorRegistration:
        registration = self._registrations.get(connector_id)
        if registration is None:
            raise UnknownConnectorError(f"connector {connector_id} is not registered")
        return registration

    def list_all(self) -> list[ConnectorRegistration]:
        return [self._registrations[k] for k in sorted(self._registrations)]

    def set_certification(
        self, connector_id: str, status: str, *, certified_at: datetime | None = None
    ) -> ConnectorRegistration:
        if status not in {"NEVER_RUN", "PASSED", "FAILED", "STALE"}:
            raise ValueError(f"unknown certification_status {status!r}")
        registration = self.get(connector_id)
        updated = replace(
            registration,
            certification_status=status,
            last_certified_at=certified_at,
            updated_at=self._clock.now(),
        )
        self._registrations[connector_id] = updated
        return updated

    def bump_adapter_version(
        self, connector_id: str, version: str, *, actor_id: str
    ) -> ConnectorRegistration:
        """A new adapter_version resets certification to STALE (spec/10)."""
        registration = self.get(connector_id)
        if version == registration.adapter_version:
            return registration
        now = self._clock.now()
        updated = replace(
            registration,
            adapter_version=version,
            certification_status="STALE",
            updated_at=now,
        )
        self._registrations[connector_id] = updated
        self._audit.record(
            "CONNECTOR_ADAPTER_VERSION_CHANGED",
            actor_id=actor_id,
            occurred_at=now,
            detail={
                "connector_id": connector_id,
                "from_version": registration.adapter_version,
                "to_version": version,
                "certification_status": "STALE",
            },
        )
        return updated

    # -- health states -----------------------------------------------------------

    def record_health(
        self,
        connector_id: str,
        *,
        healthy: bool,
        latency_ms: int | None = None,
        detail_code: str | None = None,
        degraded: bool = False,
    ) -> ConnectorRegistration:
        """Append a health sample and advance ``health_status``.

        ``degraded=True`` with ``healthy=True`` marks DEGRADED (responding but
        impaired); a status delta emits ``connector_registration.health_changed``.
        """
        registration = self.get(connector_id)
        if degraded and not healthy:
            raise InvalidRequestError(
                "degraded health requires healthy=True (DEGRADED is a responsive state)",
                code="invalid_health_sample",
            )
        status = "DEGRADED" if (healthy and degraded) else ("HEALTHY" if healthy else "UNHEALTHY")
        now = self._clock.now()
        self._health.append(
            HealthSampleRecord(
                sample_id=make_connector_id(
                    "chlth", {"connector_id": connector_id, "sampled_at": now}
                ),
                connector_id=connector_id,
                healthy=healthy,
                latency_ms=latency_ms,
                detail_code=detail_code,
                sampled_at=now,
            )
        )
        if status != registration.health_status:
            self._events.emit(
                "connector_registration.health_changed",
                {
                    "connector_id": connector_id,
                    "from": registration.health_status,
                    "to": status,
                    "latency_ms": latency_ms,
                },
            )
        updated = replace(
            registration, health_status=status, last_health_at=now, updated_at=now
        )
        self._registrations[connector_id] = updated
        return updated

    def health_log(self, connector_id: str) -> list[HealthSampleRecord]:
        return self._health.list_for(connector_id)

    # -- production deployment guard ----------------------------------------------

    def assert_simulator_allowed(self, *, allow_simulator: bool) -> None:
        """Refuse to serve when a SIMULATOR row is active and the deployment
        forbids it (``ALLOW_SIMULATOR=false`` — spec/10 failure-mode table)."""
        if allow_simulator:
            return
        offenders = [
            connector_id
            for connector_id in sorted(self._registrations)
            if self._registrations[connector_id].active_mode is ConnectorMode.SIMULATOR
            and self._registrations[connector_id].enabled
        ]
        if offenders:
            raise SimulatorForbiddenError(
                "simulator mode is forbidden in this deployment; offending connectors: "
                + ", ".join(offenders)
            )

    # -- credential resolution -------------------------------------------------

    def _credential_refs(self, registration: ConnectorRegistration, mode: ConnectorMode) -> list:
        by_mode = registration.config.get("credential_refs_by_mode", {})
        return list(by_mode.get(mode.value, []))

    def _credentials_resolve(
        self, registration: ConnectorRegistration, mode: ConnectorMode
    ) -> bool:
        for ref in self._credential_refs(registration, mode):
            try:
                self._resolver.resolve(ref)
            except Exception:
                return False
        return True

    # -- FSM core ----------------------------------------------------------------

    def _apply(
        self,
        registration: ConnectorRegistration,
        to_mode: ConnectorMode,
        *,
        trigger: str,
        actor_id: str,
        reason: str,
        approval_request_id: str | None = None,
        audit_action: str = "CONNECTOR_MODE_CHANGED",
        enabled: bool | None = None,
        previous_mode: ConnectorMode | None = None,
    ) -> ConnectorRegistration:
        now = self._clock.now()
        from_mode = registration.active_mode
        updated = replace(
            registration,
            active_mode=to_mode,
            previous_mode=previous_mode if previous_mode is not None else from_mode,
            enabled=registration.enabled if enabled is None else enabled,
            updated_at=now,
        )
        self._registrations[registration.connector_id] = updated
        self._mode_changes.append(
            ModeChangeRecord(
                mode_change_id=make_connector_id(
                    "cmode",
                    {
                        "connector_id": registration.connector_id,
                        "from_mode": from_mode.value,
                        "to_mode": to_mode.value,
                        "occurred_at": now,
                    },
                ),
                connector_id=registration.connector_id,
                from_mode=from_mode.value,
                to_mode=to_mode.value,
                reason=reason,
                actor_id=actor_id,
                approval_request_id=approval_request_id,
                occurred_at=now,
            )
        )
        self._audit.record(
            audit_action,
            actor_id=actor_id,
            occurred_at=now,
            detail={
                "connector_id": registration.connector_id,
                "trigger": trigger,
                "from_mode": from_mode.value,
                "to_mode": to_mode.value,
                "reason": reason,
            },
        )
        self._events.emit(
            "connector_registration.mode_changed",
            {
                "connector_id": registration.connector_id,
                "from_mode": from_mode.value,
                "to_mode": to_mode.value,
                "actor_id": actor_id,
                "approval_request_id": approval_request_id,
            },
        )
        return updated

    # -- transitions (the only legal ones; everything else is DENIED) ------------

    def promote(
        self,
        connector_id: str,
        *,
        actor_id: str,
        reason: str,
        approval_request_id: str | None = None,
    ) -> ConnectorRegistration:
        registration = self.get(connector_id)
        target = _PROMOTION_TARGET.get(registration.active_mode)
        if target is None:
            raise TransitionDeniedError(
                f"promote from {registration.active_mode.value} is not in the transition table"
            )
        if registration.certification_status != "PASSED":
            raise TransitionDeniedError(
                "promotion guard failed: latest certification verdict is "
                f"{registration.certification_status}, requires PASSED"
            )
        if target in _CREDENTIALED_MODES and not self._credentials_resolve(registration, target):
            raise TransitionDeniedError(
                f"promotion guard failed: {target.value} credential refs do not all resolve"
            )
        if target is ConnectorMode.PRODUCTION and not approval_request_id:
            raise TransitionDeniedError(
                "promotion to PRODUCTION requires an APPROVED two-eyes approval_request_id"
            )
        return self._apply(
            registration,
            target,
            trigger="promote",
            actor_id=actor_id,
            reason=reason,
            approval_request_id=approval_request_id,
        )

    def demote(
        self, connector_id: str, to_mode: ConnectorMode, *, actor_id: str, reason: str
    ) -> ConnectorRegistration:
        registration = self.get(connector_id)
        if registration.active_mode is not ConnectorMode.PRODUCTION:
            raise TransitionDeniedError(
                f"demote from {registration.active_mode.value} is not in the transition table"
            )
        if to_mode not in _DEMOTION_TARGETS:
            raise TransitionDeniedError(f"demote target {to_mode.value} is not in the table")
        return self._apply(
            registration, to_mode, trigger="demote", actor_id=actor_id, reason=reason
        )

    def disable(self, connector_id: str, *, actor_id: str, reason: str) -> ConnectorRegistration:
        registration = self.get(connector_id)
        if registration.active_mode is ConnectorMode.DISABLED:
            raise TransitionDeniedError(f"connector {connector_id} is already DISABLED")
        return self._apply(
            registration,
            ConnectorMode.DISABLED,
            trigger="operator_disable",
            actor_id=actor_id,
            reason=reason,
            enabled=False,
        )

    def re_enable(self, connector_id: str, *, actor_id: str, reason: str) -> ConnectorRegistration:
        registration = self.get(connector_id)
        if registration.active_mode is not ConnectorMode.DISABLED:
            raise TransitionDeniedError(
                f"re_enable from {registration.active_mode.value} is not in the transition table"
            )
        previous = registration.previous_mode
        if previous is None or previous is ConnectorMode.DISABLED:
            raise TransitionDeniedError("no stored previous mode to restore")
        if previous in _CREDENTIALED_MODES and not self._credentials_resolve(
            registration, previous
        ):
            raise TransitionDeniedError(
                f"re_enable guard failed: {previous.value} credential refs do not all resolve"
            )
        return self._apply(
            registration,
            previous,
            trigger="re_enable",
            actor_id=actor_id,
            reason=reason,
            enabled=True,
            previous_mode=ConnectorMode.DISABLED,
        )

    # -- fail-closed credential sweep --------------------------------------------

    def sweep_credentials(self) -> list[str]:
        """Resolve credential refs for every credentialed-mode connector.

        Any miss disables the connector immediately (NEVER a silent downgrade
        to SIMULATOR). Returns the connector_ids disabled by this sweep.
        """
        disabled: list[str] = []
        for connector_id in sorted(self._registrations):
            registration = self._registrations[connector_id]
            if registration.active_mode not in _CREDENTIALED_MODES:
                continue
            if self._credentials_resolve(registration, registration.active_mode):
                continue
            self._apply(
                registration,
                ConnectorMode.DISABLED,
                trigger="credentials_missing_at_load",
                actor_id=_SWEEP_ACTOR,
                reason="credential ref unresolvable at sweep (fail-closed)",
                audit_action="CONNECTOR_FAIL_CLOSED",
                enabled=False,
            )
            self._events.emit(
                "connector_registration.disabled",
                {"connector_id": connector_id, "reason": "credentials_missing_at_load"},
            )
            disabled.append(connector_id)
        return disabled

    def mode_change_log(self, connector_id: str) -> list[ModeChangeRecord]:
        return self._mode_changes.list_for(connector_id)

"""Composition root — wires real implementations into the service container.

``build_services(settings, clock)`` returns a typed :class:`ServiceContainer`
with every platform / ledger / kernel / compliance / gateway dependency wired.
``create_app(settings, clock)`` mounts the gateway FastAPI application with
``/healthz`` and ``/metrics`` on the same prefix and returns it.

Storage strategy (IMPLEMENTATION.md):

- ``settings.database_url == "memory://"`` → all stores are in-memory
  (deterministic, no infrastructure required; the unit smoke test uses this).
- Any other ``database_url`` → psycopg3 Postgres stores (the integration
  variant; the compose target spins Postgres on port 5440).

The composition root is the ONLY place allowed to instantiate real stores or
reach into the environment. Domain packages never call ``get_settings()``
from the composition root path — they receive the injected value.

Activation-pending surfaces:
- KYC ekyc service: in-memory pass-through (lane-B ekyc merge required).
- Postgres stores: wired but skipped when ``database_url == "memory://"``.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI

# Compliance — real sanctions screener + AML transaction monitor.
from bdpay.compliance.alerts import AmlAlertService, InMemoryAlertStore, RecordingSubjectControl
from bdpay.compliance.ctr import CtrBatch, CtrStore, InMemoryCtrStore, dhaka_date
from bdpay.compliance.dossier import (
    ChainReplayAdapter,
    DossierExportService,
    InMemoryDossierExportStore,
    InMemoryObjectStore,
    PostgresDossierExportStore,
)
from bdpay.compliance.dossier_wiring import (
    AuditTrailCaseEvidenceAdapter,
    CertificationRunReadAdapter,
    FactCorrectionRegisterAdapter,
    LedgerChainVerifyAdapter,
    LedgerReplayReadAdapter,
    LedgerTrialBalanceAdapter,
    TrackedSponsorPackAdapter,
)
from bdpay.compliance.fact_corrections import (
    FactCorrectionService,
    InMemoryFactCorrectionStore,
    PostgresFactCorrectionStore,
)
from bdpay.compliance.monitor import InMemoryMonitoringEventStore, TransactionMonitor
from bdpay.compliance.packs import InMemoryRulePackStore, RulePackLoader
from bdpay.compliance.sanctions.feed import load_sanctions_feed
from bdpay.compliance.sanctions.screening import (
    InMemorySanctionsStore,
    ListEntryInput,
    SanctionsScreener,
    SanctionsStore,
)
from bdpay.compliance.signing import InMemorySigningKeyStore, SigningKeyRegistry
from bdpay.compliance.store_pg import (
    PostgresAlertStore,
    PostgresCtrStore,
    PostgresMonitoringEventStore,
    PostgresRulePackStore,
    PostgresSanctionsStore,
    PostgresSigningKeyStore,
    PostgresStrStore,
)
from bdpay.compliance.str_workflow import InMemoryStrStore, StrStore, StrWorkflow
from bdpay.compliance.subject_control import PostgresSubjectControl
from bdpay.compliance.thresholds import load_thresholds
from bdpay.connectors.breaker import CircuitBreaker
from bdpay.connectors.idempotency import (
    InMemoryIdempotencyStore as ConnectorInMemoryIdempotencyStore,
)
from bdpay.connectors.idempotency import (
    PostgresIdempotencyStore as ConnectorPostgresIdempotencyStore,
)
from bdpay.connectors.identity.goaml import (
    CONNECTOR_ID as GOAML_CONNECTOR_ID,
)
from bdpay.connectors.identity.goaml import (
    GoamlReporterConnector,
    HttpGoamlPortal,
    InMemoryGoamlFilingStore,
)
from bdpay.connectors.identity.goaml_pg_store import PostgresGoamlFilingStore
from bdpay.connectors.identity.hsm import AsyncioTcpHostTransport, HsmThalesConnector
from bdpay.connectors.identity.sanctions import (
    CONNECTOR_ID as SANCTIONS_FEED_CONNECTOR_ID,
)
from bdpay.connectors.identity.sanctions import (
    STALENESS_ALARM_H,
    SanctionsFeedConnector,
    parse_domestic_list,
    parse_un_consolidated,
)
from bdpay.connectors.mfs.bkash import (
    BkashIpnWebhookHandler,
    BkashPgwConnector,
    BkashSyncStatusTruth,
)
from bdpay.connectors.mfs.handles import InMemoryMfsHandleStore, PostgresMfsHandleStore
from bdpay.connectors.mfs.ipn import QueryConfirmedIpnHandler
from bdpay.connectors.mfs.nagad import NagadPgwConnector, NagadSyncStatusTruth
from bdpay.connectors.mfs.registration import SPEC12_CONNECTOR_SPECS, register_spec12_connectors
from bdpay.connectors.mfs.rocket import RocketAggregatorConnector, RocketSyncStatusTruth
from bdpay.connectors.mfs.sms import (
    CONNECTOR_ID as SMS_CONNECTOR_ID,
)
from bdpay.connectors.mfs.sms import (
    DeterministicSmsGateway,
    HttpSmsGateway,
    SmsOtpConnector,
    TwilioSmsGateway,
)
from bdpay.connectors.mfs.token_state import InMemoryTokenStateStore, PostgresTokenStateStore
from bdpay.connectors.mfs.wire import HttpxTransport, PinnedHttpxTransport
from bdpay.connectors.ports import (
    CompositeCredentialResolver,
    CredentialResolver,
    EnvCredentialResolver,
    OpenBaoKvCredentialResolver,
)
from bdpay.connectors.ports import (
    InMemoryObjectStore as ConnectorInMemoryObjectStore,
)
from bdpay.connectors.rails.beftn import (
    CONNECTOR_ID as BEFTN_CONNECTOR_ID,
)
from bdpay.connectors.rails.beftn import (
    AsyncsshSftpTransport,
    BeftnBatchConnector,
    PgpFileSigner,
)
from bdpay.connectors.rails.beftn_layout import BeftnFileConfig, routing_check_digit
from bdpay.connectors.rails.card import (
    CardAcquirerConnector,
    CardWebhookHandler,
    HttpAcquirerTransport,
    HttpVaultProxy,
)
from bdpay.connectors.rails.npsb import (
    NpsbConnector,
    NpsbWebhookHandler,
    PostgresSafStore,
    PostgresStanStore,
    TcpNpsbTransport,
)
from bdpay.connectors.rails.ports import HmacMacService
from bdpay.connectors.rails.registration import (
    RAIL_CONNECTOR_IDS,
    rail_registration_config,
    register_rail_connectors,
)
from bdpay.connectors.rails.rtgs import HttpRtgsTransport, RtgsConnector, RtgsWebhookHandler
from bdpay.connectors.registry import (
    ConnectorMode,
    ConnectorRegistry,
    SimulatorForbiddenError,
    UnknownConnectorError,
    budget_for,
    mode_from_config,
)
from bdpay.connectors.runner import ConnectorRunner
from bdpay.connectors.scenarios import build_baseline_scenarios, build_extended_scenarios
from bdpay.connectors.simulator import ScenarioEngine, SimulatorConnector
from bdpay.connectors.stores import (
    CertificationRunStore,
    InMemoryBreakerStateStore,
    InMemoryCertificationRunStore,
    InMemoryConnectorResultStore,
    InMemoryWebhookInboundStore,
    PostgresBreakerStateStore,
    PostgresCertificationRunStore,
    PostgresConnectorResultStore,
    PostgresWebhookInboundStore,
)
from bdpay.gateway.apikeys import ApiKeyService, InMemoryApiKeyRepository
from bdpay.gateway.app import GatewayDependencies
from bdpay.gateway.app import create_app as _create_gateway_app
from bdpay.gateway.config import DEPLOY_ID_ENV_KEYS, RUNTIME_SHA_ENV_KEYS, GatewaySettings
from bdpay.gateway.credentials import encrypt_blob
from bdpay.gateway.customers import InMemoryCustomerService, PostgresCustomerService
from bdpay.gateway.event_fanout import (
    BatchMerchantSlice,
    IntentNotificationContext,
    ResilienceEventFanout,
)
from bdpay.gateway.idempotency import InMemoryIdempotencyStore
from bdpay.gateway.links import (
    InMemoryPaymentLinkStore,
    PaymentLinkEventConsumer,
    PaymentLinkService,
    PostgresPaymentLinkStore,
)
from bdpay.gateway.notification_delivery import (
    PostgresNotificationRecipientDirectory,
    SmsConnectorNotificationPort,
    platform_rendered_sms_templates,
)
from bdpay.gateway.operators import (
    InMemoryOperatorDirectory,
    InMemoryOperatorStore,
    OperatorAuthService,
    OperatorTotpEnrollment,
)
from bdpay.gateway.otp_pg_store import PostgresOtpSessionStore
from bdpay.gateway.otp_sessions import InMemoryOtpSessionStore
from bdpay.gateway.pg import (
    PostgresApiKeyRepository,
    PostgresIdempotencyStore,
    PostgresNotificationStore,
    PostgresOperatorDirectory,
    PostgresOperatorStore,
)
from bdpay.gateway.policy import build_policies
from bdpay.gateway.ports import WebhookHandlerRegistry
from bdpay.gateway.public_surfaces import PublicSurfaceService, RegistryReadAdapter
from bdpay.gateway.ratelimit import InMemoryRateLimitStore, RateLimiter, RedisRateLimitStore
from bdpay.gateway.routes_disbursements import (
    disbursement_route_policies,
    install_disbursement_routes,
)
from bdpay.gateway.routes_offers import (
    OfferRouteDependencies,
    add_offer_routes,
    offer_route_policies,
)
from bdpay.gateway.routes_participants import (
    ParticipantRoutesDependencies,
    build_participants_router,
    participant_route_policies,
)
from bdpay.gateway.routes_subscriptions import (
    SubscriptionRouteDependencies,
    add_subscription_routes,
    subscription_route_policies,
)
from bdpay.gateway.sandbox import (
    InMemorySandboxSignupStore,
    PostgresSandboxSignupStore,
    SandboxKeyMinter,
    SandboxPublicGuard,
    SandboxSignupService,
)
from bdpay.gateway.webhook_pipeline_bridge import build_gateway_webhook_pipeline
from bdpay.gateway.webhook_sink import PostgresWebhookSink
from bdpay.gateway.webhooks_out import (
    HttpxWebhookTransport,
    InMemoryWebhookEndpointStore,
    PostgresWebhookEndpointStore,
    RecordingWebhookTransport,
    WebhookDeliveryService,
    WebhookEndpointService,
)
from bdpay.kernel.connector_inbound import PostgresConnectorInboundEventStore
from bdpay.kernel.disbursement.beftn_return_consumer import BeftnReturnConsumer
from bdpay.kernel.disbursement.pg_store import PostgresDisbursementStore
from bdpay.kernel.disbursement.service import DisbursementService
from bdpay.kernel.disbursement.store import InMemoryDisbursementStore
from bdpay.kernel.kyc.kyc_repository import PostgresKycStore
from bdpay.kernel.limits.pg_limit_data import PostgresLimitDataPort
from bdpay.kernel.offers.repository import InMemoryOfferStore, PostgresOfferStore
from bdpay.kernel.offers.service import OfferService
from bdpay.kernel.onboarding.kyb import (
    KybStoreMerchantFeeProfiles,
    KybStoreMerchantGates,
    MerchantOnboardingService,
)
from bdpay.kernel.onboarding.kyb_repository import InMemoryKybStore, KybStore
from bdpay.kernel.orchestrator import PaymentOrchestrator
from bdpay.kernel.participants import (
    ACTIVATION_ACTION_TYPE,
    InMemoryNetDebitCapRegistry,
    InMemoryParticipantStore,
    InMemoryPsoBatteryGate,
    ParticipantOnboardingConfig,
    ParticipantOnboardingService,
    PostgresParticipantStore,
)
from bdpay.kernel.participants import (
    InMemoryObjectStore as InMemoryParticipantObjectStore,
)
from bdpay.kernel.payout_eta import PayoutEtaService
from bdpay.kernel.preflight import LimitEnforcer, PreflightPipeline
from bdpay.kernel.repository import InMemoryPaymentStore
from bdpay.kernel.routing import METHOD_CONNECTORS, RoutingEngine
from bdpay.kernel.subscriptions.repository import (
    InMemorySubscriptionStore,
    PostgresSubscriptionStore,
)
from bdpay.kernel.subscriptions.service import SubscriptionService
from bdpay.ledger.account_ids import merchant_settlement_id
from bdpay.ledger.chart import bootstrap_chart_of_accounts
from bdpay.ledger.errors import LedgerValidationError
from bdpay.ledger.memory_store import InMemoryLedgerStore as MemoryLedgerStore
from bdpay.ledger.service import LedgerService
from bdpay.ledger.settlement.engine import SettlementEngine
from bdpay.ledger.settlement.fees import default_fee_rules
from bdpay.ledger.settlement.pg_store import PostgresSettlementStore
from bdpay.ledger.settlement.store import InMemorySettlementStore
from bdpay.ledger.tcsa import InMemoryTcsaStore, PostgresTcsaStore, TcsaDispatchGate
from bdpay.ledger.trust_registry import build_checkpoint_trust
from bdpay.ledger.types import AccountSpec
from bdpay.platform.approval import (
    ApprovalGateway,
    ApprovalRule,
    InMemoryApprovalStore,
    PostgresApprovalStore,
    build_action_catalogue,
)
from bdpay.platform.clock import Clock, SystemClock
from bdpay.platform.config import Settings
from bdpay.platform.deployment_env import (
    DeploymentEnvironmentError,
    deployment_is_production,
)
from bdpay.platform.errors import (
    AuthorizationError,
    ConflictError,
    InvalidRequestError,
    SanctionsBlockError,
)
from bdpay.platform.eventbus import InProcessEventBus
from bdpay.platform.interfaces import (
    AmlPreflightResult,
    AuditEventSpec,
    ChainVerificationResult,
    JournalEntrySpec,
    SanctionsFreshnessVerdict,
    SanctionsScreenResult,
)
from bdpay.platform.notifications import (
    InMemoryNotificationStore,
    NotificationService,
    RecordingNotificationPort,
    TemplateRegistry,
)
from bdpay.platform.object_store import PostgresObjectStore
from bdpay.platform.observability import MetricsRegistry
from bdpay.platform.outbox import (
    InMemoryOutboxStore,
    OutboxEvent,
    OutboxWorker,
    PostgresOutboxStore,
    build_event,
)
from bdpay.platform.scheduler import (
    ALWAYS_REQUIRED_SCHEDULER_TASKS,
    CONDITIONAL_SCHEDULER_TASKS,
    ScheduledTask,
    Scheduler,
)
from bdpay.qr.config import BanglaQrConfig
from bdpay.qr.corpus import interop_cases as build_qr_interop_cases
from bdpay.qr.ports import IntentRecord as QrIntentRecord
from bdpay.qr.ports import MerchantRecord as QrMerchantRecord
from bdpay.qr.repository import InMemoryQrRepository, PostgresQrRepository
from bdpay.qr.service import QrService

__all__ = [
    "ProductionCompositionError",
    "ProductionCompositionGuard",
    "ServiceContainer",
    "build_compliance_scheduler_tasks",
    "build_compliance_stores",
    "build_services",
    "create_app",
]

def _is_production_env(env: dict[str, str] | os._Environ[str]) -> bool:
    try:
        return deployment_is_production(env)
    except DeploymentEnvironmentError as exc:
        raise ProductionCompositionError(str(exc)) from exc


class ProductionCompositionError(RuntimeError):
    """Raised when production boot would expose a dev-only critical-path port."""


_app_log = logging.getLogger("bdpay.app")


def _effective_required_scheduler_tasks(
    *, sanctions_feed_configured: bool, beftn_connector_configured: bool
) -> set[str]:
    """Resolve ALWAYS_REQUIRED_SCHEDULER_TASKS plus every CONDITIONAL_
    SCHEDULER_TASKS entry whose runtime predicate currently holds.

    A conditional task that is legitimately exempt in this mode (e.g. no
    sanctions_feed_connector configured under CONNECTOR_MODE=disabled
    dry-run) is logged, not silently dropped — the exemption stays visible
    rather than the boot guard just quietly requiring less.  Any conditional
    task name this function doesn't recognize is required by default (fail
    closed): a newly added CONDITIONAL_SCHEDULER_TASKS entry is still
    enforced even if its predicate hasn't been wired in here yet.
    """
    effective = set(ALWAYS_REQUIRED_SCHEDULER_TASKS)
    for task_name, description in CONDITIONAL_SCHEDULER_TASKS.items():
        if task_name in ("sanctions_feed_poll", "sanctions_staleness"):
            required = sanctions_feed_configured
        elif task_name == "beftn_return_poll":
            required = beftn_connector_configured
        else:
            required = True
        if required:
            effective.add(task_name)
        else:
            _app_log.info(
                "boot: scheduler task %s exempt in this mode (%s)",
                task_name,
                description,
            )
    return effective


class ProductionCompositionGuard:
    """Fail-closed production composition invariant checks."""

    def __init__(self, env: dict[str, str] | None = None) -> None:
        self._env = os.environ if env is None else env

    def is_production(self) -> bool:
        return _is_production_env(self._env)

    @staticmethod
    def _resolve_path(root: object, path: str) -> object | None:
        current: object | None = root
        for part in path.split("."):
            if current is None:
                return None
            current = getattr(current, part, None)
        return current

    def assert_boot_invariants(self, container: ServiceContainer) -> None:
        if not self.is_production():
            return
        offenders: list[str] = []
        if container.settings.database_url == "memory://":
            offenders.append("settings.database_url=memory://")
        ledger_role = container.settings.ledger_app_role.strip()
        if not ledger_role or "bypassrls" in ledger_role.lower():
            offenders.append(f"settings.ledger_app_role={ledger_role!r}")
        if isinstance(container.subject_control, RecordingSubjectControl):
            offenders.append("subject_control=RecordingSubjectControl")
        notification_port = getattr(container.notifications, "_port", None)
        if isinstance(notification_port, RecordingNotificationPort):
            offenders.append("notifications.port=RecordingNotificationPort")
        expected_components = {
            "ledger.store": {"PostgresLedgerStore"},
            "outbox_store": {"PostgresOutboxStore"},
            "approval._store": {"PostgresApprovalStore"},
            "payment_store": {"PostgresPaymentStore"},
            "kyb_store": {"PostgresKybStore"},
            "settlement_store": {"PostgresSettlementStore"},
            "goaml_connector._filings": {"PostgresGoamlFilingStore"},
            "goaml_connector.object_store": {"PostgresObjectStore"},
            "connector_inbound": {"PostgresConnectorInboundEventStore"},
            "gateway_deps.api_keys._repo": {"PostgresApiKeyRepository"},
            "gateway_deps.operators._store": {"PostgresOperatorStore"},
            "gateway_deps.operators._directory": {"PostgresOperatorDirectory"},
            "gateway_deps.idempotency": {"PostgresIdempotencyStore"},
            "gateway_deps.otp_sessions": {"PostgresOtpSessionStore"},
            "gateway_deps.rate_limiter._store": {"RedisRateLimitStore"},
            "gateway_deps.customers": {"PostgresCustomerService"},
            "notifications._store": {"PostgresNotificationStore"},
            "webhook_endpoints._store": {"PostgresWebhookEndpointStore"},
            "webhook_deliveries._store": {"PostgresWebhookEndpointStore"},
            "gateway_deps.webhook_sink": {"PostgresWebhookSink"},
            "qr._repo": {"PostgresQrRepository"},
            "offers._store": {"PostgresOfferStore"},
            "subscriptions._store": {"PostgresSubscriptionStore"},
            "disbursements._store": {"PostgresDisbursementStore"},
            # Audit P0 cab6a60: production boot fails if disbursement payout
            # recipients would not be sanctions-screened pre-rail, or if the
            # screening-block alert surface is unwired.
            "disbursements._sanctions": {"_SanctionsScreenerAdapter"},
            "disbursements._alerts": {"AmlAlertService"},
            "participants._store": {"PostgresParticipantStore"},
            # AML-H3: compliance stores must be Postgres in prod; InMemory* stores
            # wipe regulatory records on restart.
            "ctr_store": {"PostgresCtrStore"},
            "str_store": {"PostgresStrStore"},
            "sanctions_store": {"PostgresSanctionsStore"},
            "monitor_event_store": {"PostgresMonitoringEventStore"},
        }
        for path, expected_names in expected_components.items():
            component = self._resolve_path(container, path)
            actual = type(component).__name__ if component is not None else "None"
            if actual not in expected_names:
                offenders.append(
                    f"{path}={actual} (expected {'/'.join(sorted(expected_names))})"
                )
        ledger_store = container.ledger.store
        ledger_conn = getattr(ledger_store, "_conn", None)
        if ledger_conn is not None:
            try:
                row = ledger_conn.execute(
                    """
                    SELECT current_user, rolsuper, rolbypassrls
                    FROM pg_roles
                    WHERE rolname = current_user
                    """
                ).fetchone()
            except Exception as exc:  # noqa: BLE001 - production guard must fail closed
                offenders.append(f"ledger.current_user_check_failed={exc}")
            else:
                if row is None:
                    offenders.append("ledger.current_user_role_missing")
                else:
                    current_user, is_superuser, bypasses_rls = row
                    if current_user != ledger_role:
                        offenders.append(
                            f"ledger.current_user={current_user!r} "
                            f"(expected {ledger_role!r})"
                        )
                    if is_superuser or bypasses_rls:
                        offenders.append(
                            "ledger.current_user has superuser/BYPASSRLS privileges"
                        )
        try:
            container.connector_registry.assert_simulator_allowed(
                allow_simulator=False
            )
        except SimulatorForbiddenError as exc:
            offenders.append(f"connector_registry=SIMULATOR ({exc})")

        # P0 Closeout: two-site DR and external operator approvals gates.
        # In production mode, all external release evidence pointers must be present.
        # This prevents boots with missing DR topology/rehearsals or unverified approvals.
        from scripts.ops import external_release_preflight

        def _report_list(report: Mapping[str, object], key: str) -> list[object]:
            values = report.get(key, [])
            if values is None:
                return []
            if isinstance(values, list):
                return values
            return [f"malformed:{key}"]

        try:
            report = external_release_preflight.build_report(self._env)
            if not isinstance(report, Mapping):
                raise TypeError(f"expected mapping report, got {type(report).__name__}")
            status = str(report.get("status", "")).strip() or "missing"
            if status != "external_release_ready_for_operator_review":
                offenders.append(f"external_preflight.status={status}")
                for item in _report_list(report, "missing"):
                    offenders.append(f"external_preflight.missing={item}")
                for item in _report_list(report, "invalid"):
                    offenders.append(f"external_preflight.invalid={item}")
                for item in _report_list(report, "secret_like_gates"):
                    offenders.append(f"external_preflight.secret_like_rejected={item}")
        except Exception as exc:
            offenders.append(f"external_preflight.error={exc}")

        # WiringGuard: every spec-mandated scheduler task must be registered
        # at boot.  The union of task names across ALL schedulers (kernel,
        # compliance, connector-inbound) must superset the EFFECTIVE required
        # set: ALWAYS_REQUIRED_SCHEDULER_TASKS unconditionally, plus each
        # CONDITIONAL_SCHEDULER_TASKS entry only when its runtime predicate
        # holds. A missing task means a handler is implemented but never
        # scheduled — the exact gap this guard prevents. A legitimately
        # exempt conditional task (e.g. no sanctions_feed_connector under a
        # CONNECTOR_MODE=disabled dry-run boot) must not fail boot — see
        # _effective_required_scheduler_tasks.
        registered_task_names: set[str] = set()
        for tasks_field in (
            container.kernel_scheduler_tasks,
            container.compliance_scheduler_tasks,
            container.connector_inbound_scheduler_tasks,
            container.beftn_return_scheduler_tasks,
        ):
            registered_task_names.update(t.name for t in tasks_field)
        effective_required = _effective_required_scheduler_tasks(
            sanctions_feed_configured=container.sanctions_feed_connector is not None,
            beftn_connector_configured=container.beftn_return_consumer is not None,
        )
        missing_tasks = effective_required - registered_task_names
        if missing_tasks:
            offenders.append(
                f"scheduler_tasks_missing={sorted(missing_tasks)}"
            )
        if offenders:
            raise ProductionCompositionError(
                "production boot refused: dev-only critical-path implementations "
                f"are active: {', '.join(offenders)}"
            )

#: CTR daily-aggregation cadence (COMP-06, spec/06 step 1).  The
#: ``ctr_daily_aggregate`` task builds ctr_aggregations rows from the day's
#: cash events; spec/06 fires it at 23:30 BST.  The Scheduler is pure-cadence
#: (no time-of-day gate in WINDOW_GATES), so a 24h cadence fires once per day
#: phased from boot time, NOT pinned to 23:30 BST — see the caveat in
#: build_compliance_scheduler_tasks.
_CTR_AGGREGATE_CADENCE_SECONDS = 24 * 60 * 60  # one day
#: CTR filing-sweep cadence (COMP-06).  Operator-ratified DAILY filing sweep:
#: BFIU's DFS reporting directive (effective 2026-01-11) requires CTR filing
#: within <=3 working days of the cash transaction.  A DAILY sweep files every
#: threshold-crossed, unfiled aggregation, keeping the platform comfortably
#: inside the <=3-working-day BFIU SLA.  The cadence is the sweep poll interval
#: (one day); the <=3-working-day window is the regulatory SLA the daily sweep
#: keeps the platform inside, not the poll period itself.
_CTR_FILING_CADENCE_SECONDS = 24 * 60 * 60  # one day
#: Periodic sanctions rescreening cadence (spec/06): daily re-validation of
#: subjects under an open hit against the current ACTIVE list.
_SANCTIONS_RESCREEN_CADENCE_SECONDS = 24 * 60 * 60  # one day
#: Live UN sanctions-feed polling cadence (spec/12 §G connector budget):
#: re-fetch the public consolidated artifact every six hours; the connector
#: itself enforces hard timeout, retries, ETag dedupe, and staleness alarms.
_SANCTIONS_FEED_POLL_CADENCE_SECONDS = 6 * 60 * 60  # six hours
#: STR retry + goAML submit sweeps run frequently (the connector enforces its
#: own exponential backoff per row; a tight cadence only re-checks readiness).
_STR_FILING_CADENCE_SECONDS = 5 * 60  # five minutes (AML-H1: primary STR filing path)
_STR_RETRY_CADENCE_SECONDS = 5 * 60  # five minutes
_GOAML_SUBMIT_CADENCE_SECONDS = 5 * 60  # five minutes
#: Kernel TTL sweep cadence (spec/02): the ``ttl-sweep`` task moves every
#: expired intent per the spec/02 TTL table (30-min payment TTL, 7-day auth
#: hold TTL for REQUIRES_CAPTURE).  60 s matches the spec.
_KERNEL_TTL_SWEEP_CADENCE_SECONDS = 60
#: Reversal dispatch cadence: drive every REVERSAL_INITIATED intent through
#: ``execute_reversal`` so the hold/charge is actually released.  Without this
#: the TTL sweep moves intents to REVERSAL_INITIATED but nothing executes the
#: rail reversal.
_KERNEL_REVERSAL_DISPATCH_CADENCE_SECONDS = 60
#: Settlement confirmation resume cadence (spec/04): re-enter confirm() for
#: batches stranded in crash window A (close JE posted, batch still DISPATCHED)
#: or crash window B (batch CONFIRMED, instructions still SUBMITTED).
_KERNEL_SETTLEMENT_CONFIRM_RESUME_CADENCE_SECONDS = 60
#: Sanctions feed staleness alarm cadence (spec/12 §G): 15-min sweep that
#: raises a CRITICAL alarm for any source whose newest INGESTED feed is >24h
#: old.  Cut-last #5: a stale feed means sanctions checks run against an
#: obsolete watchlist.
_SANCTIONS_STALENESS_CADENCE_SECONDS = 15 * 60
#: STR filing-ack confirmation cadence: poll for acked goAML STR filings and
#: drive the matching STR to FILING_CONFIRMED.  Matches the goAML submit sweep
#: cadence so an ack is confirmed in the same tick window.
_STR_ACK_CONFIRM_CADENCE_SECONDS = 5 * 60


# ---------------------------------------------------------------------------
# Adapters: translate platform-interface types -> ledger-service types
# ---------------------------------------------------------------------------


class _LedgerPortAdapter:
    """Adapts bdpay.ledger.LedgerService to bdpay.platform.interfaces.LedgerPort.

    The platform LedgerPort (platform/interfaces.py) signature carries
    ``clock`` and ``conn`` kwargs that the composition root wires on
    construction; the underlying LedgerService has simpler signatures.

    JournalEntrySpec translation: platform.interfaces.PostingSpec / JournalEntrySpec
    -> ledger.types.PostingSpec / JournalEntrySpec.
    """

    def __init__(self, svc: LedgerService, clock: Clock) -> None:
        self._svc = svc
        self._clock = clock

    # -- helpers -------------------------------------------------------------

    @staticmethod
    def _translate_spec(spec: JournalEntrySpec) -> Any:
        from bdpay.ledger.types import (
            JournalEntrySpec as LSpec,
        )
        from bdpay.ledger.types import (
            PostingSpec as LPosting,
        )

        return LSpec(
            reference_id=spec.reference_id,
            reference_type=spec.reference_type,
            entry_type=spec.entry_type,
            description=spec.description,
            produced_by=spec.produced_by,
            idempotency_key=spec.idempotency_key,
            postings=tuple(
                LPosting(
                    account_id=p.account_id,
                    side=p.side,
                    amount_minor=p.amount_minor,
                    currency=p.currency,
                )
                for p in spec.postings
            ),
        )

    # -- LedgerPort methods --------------------------------------------------

    def post_journal_entry(
        self, spec: JournalEntrySpec, *, clock: Clock, conn: Any
    ) -> str:
        return self._svc.post_journal_entry(self._translate_spec(spec), conn=conn)

    def open_hold(
        self,
        account_id: str,
        amount_minor: int,
        hold_reason: str,
        reference_id: str,
        *,
        source_account_id: str | None = None,
        hold_reserve_account_id: str | None = None,
        hold_expires_at: Any = None,
        clock: Clock,
        conn: Any,
    ) -> str:
        return self._svc.open_hold(
            account_id,
            amount_minor,
            hold_reason,
            reference_id,
            source_account_id=source_account_id,
            hold_reserve_account_id=hold_reserve_account_id,
            hold_expires_at=hold_expires_at,
            conn=conn,
        )

    def release_hold(
        self,
        hold_id: str,
        outcome: str,
        *,
        amount_minor: int | None = None,
        clock: Clock,
        conn: Any,
    ) -> None:
        self._svc.release_hold(hold_id, outcome, amount_minor=amount_minor, conn=conn)

    def get_open_hold_id(self, reference_id: str, *, conn: Any) -> str | None:
        return self._svc.get_open_hold_id(reference_id, conn=conn)

    def get_balance(
        self, account_id: str, *, as_of: Any = None, conn: Any, lock: bool = False
    ) -> int:
        return self._svc.get_balance(account_id, as_of=as_of, conn=conn, lock=lock)

    def trial_balance_assertion(self, *, conn: Any) -> None:
        self._svc.trial_balance_assertion()

    def verify_chain(
        self, *, from_index: int = 0, to_index: Any = None, conn: Any
    ) -> ChainVerificationResult:
        result = self._svc.verify_chain(from_index=from_index, to_index=to_index)
        # LedgerService returns its own ChainVerificationResult; structural match.
        return ChainVerificationResult(
            ok=result.ok,
            status=result.status,
            entries_checked=result.entries_checked,
            first_broken_index=result.first_broken_index,
            detail=result.detail,
        )


class _AuditPortAdapter:
    """Adapts bdpay.ledger.LedgerService.write_audit_event to AuditPort.

    The platform AuditEventSpec has no actor_type; the ledger type requires it.
    We default to "SERVICE" for kernel/compliance callers.
    """

    def __init__(self, svc: LedgerService) -> None:
        self._svc = svc

    def append(self, spec: AuditEventSpec, *, clock: Clock, conn: Any = None) -> str:
        from bdpay.ledger.types import AuditEventSpec as LSpec

        return self._svc.write_audit_event(
            LSpec(
                event_type=spec.event_type,
                actor_id=spec.actor_id,
                actor_type="SERVICE",
                subject_type=spec.subject_type,
                subject_id=spec.subject_id,
                from_state=spec.from_state,
                to_state=spec.to_state,
                payload=dict(spec.payload) if spec.payload else {},
            ),
            conn=conn,
        )


class _OutboxPortAdapter:
    """Adapts InMemoryOutboxStore to OutboxPort (enqueue with clock from ctor)."""

    def __init__(self, store: InMemoryOutboxStore) -> None:
        self._store = store

    def enqueue(self, event: OutboxEvent, *, conn: Any = None) -> str:
        return self._store.enqueue(event, conn=conn)


class _PostgresParticipantNdcAdapter:
    """Reads active PSO net-debit caps from the ledger-owned PG table."""

    def __init__(self, connection_factory: Any) -> None:
        self._connect = connection_factory

    def active_cap_minor(self, participant_id: str) -> int | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT cap_amount_minor
                FROM ledger.net_debit_caps
                WHERE participant_id = %s AND superseded_at IS NULL
                ORDER BY effective_from DESC
                LIMIT 1
                """,
                (participant_id,),
            ).fetchone()
        return None if row is None else int(row[0])


class _ParticipantLedgerAdapter:
    """Opens the ledger participant-position account on activation."""

    def __init__(self, ledger: LedgerService) -> None:
        self._ledger = ledger

    def open_participant_accounts(
        self, participant_id: str, *, cap_minor: int, conn: Any | None = None
    ) -> None:
        try:
            self._ledger.create_account(
                AccountSpec(
                    account_type="LIABILITY",
                    account_subtype="PARTICIPANT_POSITION",
                    owner_id=participant_id,
                    owner_type="PARTICIPANT",
                )
            )
        except LedgerValidationError as exc:
            if "already exists" not in str(exc):
                raise


class _MerchantLedgerAdapter:
    """Opens the ledger merchant settlement accounts on activation."""

    def __init__(self, ledger: LedgerService) -> None:
        self._ledger = ledger

    def open_merchant_accounts(
        self, merchant_id: str, *, conn: Any | None = None
    ) -> None:
        from bdpay.ledger.chart import open_merchant_accounts as _omacc

        _omacc(self._ledger, merchant_id, conn=conn)


class _GatewayKeyAdapter:
    """Mints a live bcrypt gateway credential on merchant activation.

    Bridges the KYB kernel (``kyb_api_key_issuances``, sha256 audit record)
    and the gateway authentication store (``api_keys``, bcrypt hashed).  The
    real ``ApiKeyService.create_key`` generates its own secret; the returned
    plaintext becomes the merchant's live bearer credential.

    ``conn`` is threaded through so the gateway api-key insert runs on the
    KYB activation transaction (atomic commit-or-rollback); a ``None`` conn
    makes the port run on the repository's own connection (standalone use).
    """

    def __init__(self, api_keys: ApiKeyService) -> None:
        self._api_keys = api_keys

    def issue_live_key(
        self,
        merchant_id: str,
        key_name: str,
        *,
        scopes: tuple[str, ...],
        conn: Any | None = None,
    ) -> tuple[str, str]:
        """Create a live gateway credential; return (gateway_key_id, plaintext)."""
        created = self._api_keys.create_key(
            merchant_id=merchant_id,
            key_name=key_name,
            scopes=scopes,
            env="live",
            conn=conn,
        )
        return created.record.key_id, created.secret


# ---------------------------------------------------------------------------
# Connector runner (SIMULATOR mode — real registry/runner, in-process rails)
# ---------------------------------------------------------------------------


async def _instant_sleep(_seconds: float) -> None:
    """Deterministic no-wait sleep for in-memory mode (no real backoff/polls)."""
    return None


def _build_simulator_connector_runner(
    clock: Clock,
    *,
    simulator_only: bool = False,
    object_store: Any | None = None,
) -> tuple[ConnectorRunner, ConnectorRegistry]:
    """Wire the real ConnectorRunner backed by in-process simulators.

    Every connector the RoutingEngine can select (METHOD_CONNECTORS) is
    registered in SIMULATOR mode with the same deterministic ScenarioEngine /
    SimulatorConnector bench the certification suite uses
    (bdpay.connectors.certification.build_simulator_environment). All
    branching derives from sha256(connector_ref) buckets and explicit
    scenario hints; sleeps are instant so smoke tests stay fast.

    Returns the runner AND its registry: the registry is the read surface
    the spec/16 dossier exporter uses to define "every registered connector"
    for the CERT-01..10 coverage gate.
    """
    registry = ConnectorRegistry(clock=clock)
    breaker = CircuitBreaker(clock=clock)
    runner = ConnectorRunner(
        registry=registry,
        breaker=breaker,
        clock=clock,
        sleep=_instant_sleep,
        object_store=object_store,
        # spec/16 §API F inv 2 (SANDBOX_PUBLIC): the runner itself refuses any
        # non-SIMULATOR adapter or dispatch — defense-in-depth under the guard.
        simulator_only=simulator_only,
    )
    signing_key = b"bdpay-in-memory-simulator-key"
    methods_by_connector: dict[str, tuple[str, ...]] = {}
    for method, candidates in METHOD_CONNECTORS.items():
        for connector_id in candidates:
            methods_by_connector[connector_id] = (
                *methods_by_connector.get(connector_id, ()), method
            )
    for connector_id, methods in sorted(methods_by_connector.items()):
        scenarios = build_baseline_scenarios(connector_id, created_at=clock.now())
        scenarios += build_extended_scenarios(connector_id, created_at=clock.now())
        engine = ScenarioEngine(
            connector_id,
            scenarios,
            clock=clock,
            signing_key=signing_key,
            sleep=_instant_sleep,
            object_store=object_store,
        )
        adapter = SimulatorConnector(connector_id, engine, supported_methods=methods)
        registry.register(
            connector_id,
            display_name=f"{connector_id} (in-memory simulator)",
            protocol="PAYMENT",
            capabilities=("submit", "query_status", "reverse", "health_check"),
            supported_methods=methods,
            active_mode=ConnectorMode.SIMULATOR,
        )
        runner.register_adapter(connector_id, ConnectorMode.SIMULATOR, adapter)
    return runner, registry


def _build_disabled_connector_runner(
    clock: Clock,
    *,
    object_store: Any | None = None,
) -> tuple[ConnectorRunner, ConnectorRegistry]:
    """Wire a runner whose known connector catalogue is explicitly disabled.

    ``CONNECTOR_MODE=disabled`` is a legitimate deployment posture for dry-run
    app boot and non-rail surfaces. It must not silently leave simulator rows
    active, because later dispatch would move money through the simulator while
    operators believe external connectors are off.
    """
    registry = ConnectorRegistry(clock=clock)
    breaker = CircuitBreaker(clock=clock)
    runner = ConnectorRunner(
        registry=registry,
        breaker=breaker,
        clock=clock,
        sleep=_instant_sleep,
        object_store=object_store,
    )
    register_spec12_connectors(
        registry,
        {connector_id: {"mode": "disabled"} for connector_id in SPEC12_CONNECTOR_SPECS},
    )
    register_rail_connectors(registry, mode=ConnectorMode.DISABLED)
    return runner, registry


class _ConnectorRunnerHealth:
    """Routing health view backed by the runner's registered payment adapters."""

    def __init__(self, runner: ConnectorRunner, registry: ConnectorRegistry) -> None:
        self._runner = runner
        self._registry = registry

    def is_dispatchable(self, connector_id: str, *, clock: Clock) -> bool:
        del clock
        try:
            registration = self._registry.get(connector_id)
        except UnknownConnectorError:
            return False
        if registration.active_mode is ConnectorMode.DISABLED or not registration.enabled:
            return False
        return (
            connector_id,
            registration.active_mode.value,
        ) in self._runner._adapters  # noqa: SLF001


_PAYMENT_LIVE_ADAPTERS: dict[str, Any] = {
    "bkash_pgw_v2": BkashPgwConnector,
    "nagad_pgw_v33": NagadPgwConnector,
    "rocket_aggregator_v1": RocketAggregatorConnector,
    "npsb_iso8583_v28": NpsbConnector,
    "rtgs_iso20022_v1": RtgsConnector,
    "card_acquirer_v1": CardAcquirerConnector,
}

_SETTLEMENT_FILE_LIVE_CONNECTORS = frozenset({BEFTN_CONNECTOR_ID})

_NON_PAYMENT_DEPLOYMENT_CONNECTORS = frozenset(
    {GOAML_CONNECTOR_ID, SANCTIONS_FEED_CONNECTOR_ID, SMS_CONNECTOR_ID}
)

_HEADER_SIGNED_WEBHOOK_CONNECTORS = frozenset(
    {
        "bkash_pgw_v2",
        "nagad_pgw_v33",
        "rocket_aggregator_v1",
        "rtgs_iso20022_v1",
        "card_acquirer_v1",
    }
)

_MFS_WEBHOOK_REF_FIELDS: dict[str, tuple[str, ...]] = {
    "nagad_pgw_v33": ("connector_ref", "orderId", "order_id", "paymentReferenceId"),
    "rocket_aggregator_v1": ("connector_ref", "tran_id", "merchantInvoiceNumber"),
}


def _configured_connectors(deployment: dict[str, Any]) -> dict[str, dict[str, Any]]:
    connectors = deployment.get("connectors", deployment)
    if not isinstance(connectors, dict):
        raise RuntimeError("connector deployment config must contain a connectors object")
    result: dict[str, dict[str, Any]] = {}
    for connector_id, entry in connectors.items():
        if not isinstance(connector_id, str) or not connector_id:
            raise RuntimeError("connector deployment config contains a blank connector id")
        if not isinstance(entry, dict):
            raise RuntimeError(f"connector config for {connector_id!r} must be an object")
        result[connector_id] = dict(entry)
    return result


def _webhook_verify_ref(adapter_config: dict[str, Any], mode: ConnectorMode) -> str | None:
    refs_by_mode = dict(adapter_config.get("webhook_verify_ref_by_mode") or {})
    ref = refs_by_mode.get(mode.value) or adapter_config.get("webhook_verify_ref")
    if ref:
        return str(ref)
    credential_refs = adapter_config.get("credential_refs")
    if isinstance(credential_refs, dict):
        for key in ("webhook_verify_key", "webhook_verify_ref", "ipn_verify"):
            ref = credential_refs.get(key)
            if ref:
                return str(ref)
    return None


def _live_registration_config(
    connector_id: str,
    entry: dict[str, Any],
    *,
    mode: ConnectorMode,
) -> dict[str, Any]:
    adapter_config = dict(entry.get("config") or {})
    credential_refs = adapter_config.get("credential_refs")
    if not isinstance(credential_refs, dict) or not credential_refs:
        raise RuntimeError(
            f"{connector_id} activation requires non-empty config.credential_refs"
        )
    refs_by_mode = dict(adapter_config.get("credential_refs_by_mode") or {})
    refs_by_mode.setdefault(mode.value, list(credential_refs.values()))
    if not refs_by_mode.get(mode.value):
        raise RuntimeError(
            f"{connector_id} activation requires credential_refs_by_mode[{mode.value}]"
        )
    adapter_config["credential_refs_by_mode"] = refs_by_mode
    if connector_id == "npsb_iso8583_v28":
        host = str(adapter_config.get("host", "")).strip()
        if not host:
            raise RuntimeError("npsb_iso8583_v28 activation requires config.host")
        try:
            port = int(adapter_config["port"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(
                "npsb_iso8583_v28 activation requires integer config.port"
            ) from exc
        if port <= 0:
            raise RuntimeError("npsb_iso8583_v28 activation requires integer config.port")
        adapter_config["port"] = port
        if mode is ConnectorMode.SANDBOX:
            if not str(credential_refs.get("mac_key", "")).strip():
                raise RuntimeError(
                    "npsb_iso8583_v28 sandbox activation requires credential_refs.mac_key"
                )
        else:
            if not str(adapter_config.get("hsm_host", "")).strip():
                raise RuntimeError(
                    "npsb_iso8583_v28 live activation requires config.hsm_host"
                )
            try:
                hsm_port = int(adapter_config["hsm_port"])
            except (KeyError, TypeError, ValueError) as exc:
                raise RuntimeError(
                    "npsb_iso8583_v28 live activation requires integer config.hsm_port"
                ) from exc
            if hsm_port <= 0:
                raise RuntimeError(
                    "npsb_iso8583_v28 live activation requires integer config.hsm_port"
                )
            if not str(adapter_config.get("mac_key_label", "")).strip():
                raise RuntimeError(
                    "npsb_iso8583_v28 live activation requires config.mac_key_label"
                )
            adapter_config["hsm_port"] = hsm_port
    elif connector_id == "card_acquirer_v1":
        if not str(adapter_config.get("vault_proxy_base_url", "")).strip():
            raise RuntimeError(
                "card_acquirer_v1 activation requires config.vault_proxy_base_url"
            )
        if not str(adapter_config.get("acquirer_base_url", "")).strip():
            raise RuntimeError(
                "card_acquirer_v1 activation requires config.acquirer_base_url"
            )
        if not str(credential_refs.get("acquirer_hmac_key", "")).strip():
            raise RuntimeError(
                "card_acquirer_v1 activation requires credential_refs.acquirer_hmac_key"
            )
    elif connector_id == BEFTN_CONNECTOR_ID:
        host = str(adapter_config.get("host", adapter_config.get("sftp_host", ""))).strip()
        if not host:
            raise RuntimeError("beftn_batch_v2 activation requires config.host")
        adapter_config["host"] = host
        try:
            port = int(adapter_config["port"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(
                "beftn_batch_v2 activation requires integer config.port"
            ) from exc
        if port <= 0:
            raise RuntimeError("beftn_batch_v2 activation requires integer config.port")
        adapter_config["port"] = port
        if not str(adapter_config.get("username", "")).strip():
            raise RuntimeError("beftn_batch_v2 activation requires config.username")
        for key in (
            "sftp_private_key",
            "sftp_known_hosts",
            "pgp_signing_key",
            "detokenizer_bearer_token",
        ):
            if not str(credential_refs.get(key, "")).strip():
                raise RuntimeError(
                    f"beftn_batch_v2 activation requires credential_refs.{key}"
                )
        detokenizer_url = str(adapter_config.get("detokenizer_base_url", "")).strip()
        if not detokenizer_url.startswith("https://"):
            raise RuntimeError(
                "beftn_batch_v2 activation requires HTTPS config.detokenizer_base_url"
            )
        _validate_beftn_routing_config(
            "config.immediate_destination", adapter_config.get("immediate_destination")
        )
        _validate_beftn_routing_config(
            "config.immediate_origin", adapter_config.get("immediate_origin")
        )
        if not str(adapter_config.get("destination_name", "")).strip():
            raise RuntimeError("beftn_batch_v2 activation requires config.destination_name")
        originating_dfi = str(adapter_config.get("originating_dfi", "11223344")).strip()
        if len(originating_dfi) != 8 or not originating_dfi.isdigit():
            raise RuntimeError(
                "beftn_batch_v2 activation requires 8-digit config.originating_dfi"
            )
        adapter_config["originating_dfi"] = originating_dfi
    elif not str(adapter_config.get("base_url", "")).strip():
        raise RuntimeError(f"{connector_id} activation requires config.base_url")
    if connector_id in _HEADER_SIGNED_WEBHOOK_CONNECTORS:
        webhook_ref = _webhook_verify_ref(adapter_config, mode)
        if not webhook_ref or not webhook_ref.strip():
            raise RuntimeError(
                f"{connector_id} activation requires config.webhook_verify_ref "
                "or credential_refs.webhook_verify_key"
            )
        refs = list(refs_by_mode.get(mode.value) or [])
        if webhook_ref not in refs:
            refs.append(webhook_ref)
        refs_by_mode[mode.value] = refs
        adapter_config["credential_refs_by_mode"] = refs_by_mode
        adapter_config["webhook_verify_ref"] = webhook_ref
    certification_status = str(entry.get("certification_status", "NEVER_RUN"))
    return {
        "mode": "live" if mode is ConnectorMode.PRODUCTION else "sandbox",
        "certification_status": certification_status,
        "config": adapter_config,
    }


def _register_rail_connectors_for_deployment(
    registry: ConnectorRegistry,
    *,
    configured: dict[str, dict[str, Any]],
    active_connector_ids: set[str],
    mode: ConnectorMode,
) -> None:
    for connector_id in RAIL_CONNECTOR_IDS:
        config = rail_registration_config(connector_id)
        if connector_id in active_connector_ids:
            live_config = _live_registration_config(
                connector_id,
                configured[connector_id],
                mode=mode,
            )
            registry.register(
                connector_id,
                display_name=config["display_name"],
                protocol=config["protocol"],
                capabilities=config["capabilities"],
                supported_methods=config["supported_methods"],
                active_mode=mode,
                config=live_config["config"],
                certification_status=live_config["certification_status"],
            )
        else:
            registry.register(
                connector_id,
                display_name=config["display_name"],
                protocol=config["protocol"],
                capabilities=config["capabilities"],
                supported_methods=config["supported_methods"],
                active_mode=ConnectorMode.DISABLED,
            )


def _validate_beftn_routing_config(label: str, value: object) -> str:
    routing = str(value or "").strip()
    if len(routing) != 9 or not routing.isdigit():
        raise RuntimeError(f"beftn_batch_v2 activation requires 9-digit {label}")
    if routing_check_digit(routing[:8]) != int(routing[8]):
        raise RuntimeError(f"beftn_batch_v2 activation requires valid {label}")
    return routing


def _default_rtgs_transport(registration: Any, timeout_s: float) -> HttpRtgsTransport:
    import httpx

    config = registration.config
    client = httpx.AsyncClient(
        base_url=str(config["base_url"]).rstrip("/"),
        timeout=timeout_s,
    )
    return HttpRtgsTransport(
        client,
        submit_path=str(config.get("submit_path", "/v1/pacs008")),
        status_path=str(config.get("status_path", "/v1/status")),
    )


def _default_card_ports(
    registration: Any,
    timeout_s: float,
    credential_resolver: CredentialResolver,
) -> tuple[HttpVaultProxy, HttpAcquirerTransport]:
    import httpx

    config = registration.config
    credential_refs = config.get("credential_refs", {})
    hmac_ref = credential_refs["acquirer_hmac_key"]
    hmac_key = credential_resolver.resolve(str(hmac_ref)).encode("utf-8")
    vault_client = httpx.AsyncClient(
        base_url=str(config["vault_proxy_base_url"]).rstrip("/"),
        timeout=timeout_s,
    )
    acquirer_client = httpx.AsyncClient(
        base_url=str(config["acquirer_base_url"]).rstrip("/"),
        timeout=timeout_s,
    )
    return (
        HttpVaultProxy(
            vault_client,
            path=str(config.get("vault_proxy_path", "/detokenize-and-forward")),
        ),
        HttpAcquirerTransport(
            acquirer_client,
            hmac_key=hmac_key,
            path_by_op=config.get("acquirer_paths"),
        ),
    )


class _HsmBackedRailMacService:
    """Synchronous rail MAC port backed by the async HSM connector boundary."""

    def __init__(
        self,
        hsm: HsmThalesConnector,
        *,
        key_label: str,
        timeout_s: float,
    ) -> None:
        import asyncio
        import threading

        self._hsm = hsm
        self._key_label = key_label
        self._timeout_s = timeout_s
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()

    def _run(self, coro: Any) -> Any:
        import asyncio
        import concurrent.futures

        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(timeout=self._timeout_s)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise

    def generate_mac(self, preimage: bytes) -> str:
        mac = self._run(self._hsm.generate_mac(self._key_label, preimage))
        return bytes(mac).hex()[:16].upper()

    def verify_mac(self, preimage: bytes, mac: str) -> bool:
        import hmac

        if not isinstance(mac, str) or len(mac) != 16:
            return False
        expected = self.generate_mac(preimage)
        return hmac.compare_digest(expected, mac.upper())


def _default_npsb_ports(
    registration: Any,
    timeout_s: float,
    mode: ConnectorMode,
    clock: Clock,
    credential_resolver: CredentialResolver,
) -> tuple[TcpNpsbTransport, Any]:
    config = registration.config
    transport = TcpNpsbTransport(str(config["host"]), int(config["port"]))
    if mode is ConnectorMode.SANDBOX:
        credential_refs = config.get("credential_refs", {})
        mac_ref = credential_refs["mac_key"]
        mac_key = credential_resolver.resolve(str(mac_ref)).encode("utf-8")
        return transport, HmacMacService(mac_key)

    hsm = HsmThalesConnector(
        mode=ConnectorMode.PRODUCTION,
        clock=clock,
        credentials=credential_resolver,
        config={"host_commands": config.get("hsm_host_commands", {})},
        host_transport=AsyncioTcpHostTransport(
            str(config["hsm_host"]),
            int(config["hsm_port"]),
        ),
        timeout_s=min(timeout_s, 5.0),
    )
    return (
        transport,
        _HsmBackedRailMacService(
            hsm,
            key_label=str(config["mac_key_label"]),
            timeout_s=min(timeout_s, 5.0),
        ),
    )


class _ConfiguredAccountDetokenizer:
    """Synchronous account-token detokenizer for BEFTN file rendering."""

    def __init__(
        self,
        *,
        base_url: str,
        bearer_token: str,
        timeout_s: float,
        path: str = "/v1/account-tokens/{token}/detokenize",
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._bearer_token = bearer_token
        self._timeout_s = timeout_s
        self._path = path

    def detokenize(self, token: str) -> str:
        return str(self.detokenize_account(token)["account_number"])

    def detokenize_account(self, token: str) -> dict[str, str]:
        from urllib.parse import quote

        import httpx

        clean_token = str(token or "").strip()
        if not clean_token:
            raise LookupError("account token missing")
        path = self._path.format(token=quote(clean_token, safe=""))
        try:
            with httpx.Client(
                base_url=self._base_url,
                timeout=httpx.Timeout(self._timeout_s),
            ) as client:
                response = client.post(
                    path,
                    json={"token": clean_token},
                    headers={"Authorization": f"Bearer {self._bearer_token}"},
                )
        except httpx.HTTPError as exc:
            raise LookupError("account detokenizer unavailable") from exc
        if response.status_code >= 300:
            raise LookupError("account detokenizer refused token")
        try:
            payload = response.json()
        except ValueError as exc:
            raise LookupError("account detokenizer returned non-json") from exc
        data = payload.get("data") if isinstance(payload, dict) else None
        detail = data if isinstance(data, dict) else payload
        if not isinstance(detail, dict):
            raise LookupError("account detokenizer returned invalid payload")
        account_number = str(detail.get("account_number") or detail.get("account") or "")
        routing = str(
            detail.get("receiving_routing")
            or detail.get("routing_number")
            or detail.get("routing")
            or ""
        )
        if not account_number.strip() or not routing.strip():
            raise LookupError("account detokenizer returned incomplete account details")
        return {"account_number": account_number, "receiving_routing": routing}


def _beftn_file_config(adapter_config: dict[str, Any]) -> BeftnFileConfig:
    return BeftnFileConfig(
        immediate_destination=str(adapter_config["immediate_destination"]).strip(),
        immediate_origin=str(adapter_config["immediate_origin"]).strip(),
        destination_name=str(adapter_config["destination_name"]).strip(),
        origin_name=str(adapter_config.get("origin_name", "BD-PAY")).strip() or "BD-PAY",
        company_name=str(adapter_config.get("company_name", "BD-PAY")).strip() or "BD-PAY",
        company_id=str(adapter_config.get("company_id", "BDPAY00001")).strip()
        or "BDPAY00001",
        originating_dfi=str(adapter_config.get("originating_dfi", "11223344")).strip(),
        service_class=str(adapter_config.get("service_class", "220")).strip() or "220",
        standard_entry_class=str(adapter_config.get("standard_entry_class", "CCD")).strip()
        or "CCD",
        entry_description=str(adapter_config.get("entry_description", "MERCHPAYOUT")).strip()
        or "MERCHPAYOUT",
    )


def _default_beftn_transport(
    adapter_config: dict[str, Any],
    *,
    credential_resolver: CredentialResolver,
) -> AsyncsshSftpTransport:
    credential_refs = adapter_config.get("credential_refs", {})
    return AsyncsshSftpTransport(
        host=str(adapter_config["host"]),
        port=int(adapter_config["port"]),
        username=str(adapter_config["username"]),
        credential_resolver=credential_resolver,
        private_key_ref=str(credential_refs["sftp_private_key"]),
        known_hosts_ref=str(credential_refs["sftp_known_hosts"]),
    )


def _resolve_webhook_key(
    registration: Any,
    *,
    mode: ConnectorMode,
    credential_resolver: CredentialResolver,
) -> bytes:
    ref = _webhook_verify_ref(registration.config, mode)
    if not ref:
        raise RuntimeError(
            f"{registration.connector_id} has no configured webhook verification ref"
        )
    return credential_resolver.resolve(ref).encode("utf-8")


def _build_live_webhook_handler(
    connector_id: str,
    adapter: Any,
    registration: Any,
    *,
    mode: ConnectorMode,
    clock: Clock,
    credential_resolver: CredentialResolver,
) -> Any | None:
    if connector_id == "bkash_pgw_v2":
        key = _resolve_webhook_key(
            registration,
            mode=mode,
            credential_resolver=credential_resolver,
        )
        # Prefer query-confirmed live truth (CERT-M1) when the adapter can build it.
        sync_truth = (
            adapter.sync_status_truth()
            if hasattr(adapter, "sync_status_truth")
            else None
        )
        truth_for = (
            sync_truth
            if isinstance(sync_truth, BkashSyncStatusTruth)
            else adapter.truth_for
        )
        return BkashIpnWebhookHandler(
            key,
            clock=clock,
            truth_for=truth_for,
            signature_header=str(
                registration.config.get("webhook_signature_header", "x-sim-signature")
            ),
            timestamp_header=str(
                registration.config.get("webhook_timestamp_header", "x-sim-timestamp")
            ),
        )
    if connector_id in {"nagad_pgw_v33", "rocket_aggregator_v1"}:
        key = _resolve_webhook_key(
            registration,
            mode=mode,
            credential_resolver=credential_resolver,
        )
        configured_ref_fields = registration.config.get("webhook_ref_fields")
        ref_fields = (
            tuple(str(field) for field in configured_ref_fields)
            if isinstance(configured_ref_fields, list)
            else _MFS_WEBHOOK_REF_FIELDS[connector_id]
        )
        # Prefer query-confirmed live truth (CERT-M1) when the adapter can build it.
        sync_truth = (
            adapter.sync_status_truth()
            if hasattr(adapter, "sync_status_truth")
            else None
        )
        truth_for = (
            sync_truth
            if isinstance(sync_truth, (NagadSyncStatusTruth, RocketSyncStatusTruth))
            else adapter.truth_for
        )
        return QueryConfirmedIpnHandler(
            connector_id,
            key,
            clock=clock,
            truth_for=truth_for,
            ref_fields=ref_fields,
            signature_header=str(
                registration.config.get("webhook_signature_header", "x-sim-signature")
            ),
            timestamp_header=str(
                registration.config.get("webhook_timestamp_header", "x-sim-timestamp")
            ),
        )
    if connector_id == "rtgs_iso20022_v1":
        key = _resolve_webhook_key(
            registration,
            mode=mode,
            credential_resolver=credential_resolver,
        )
        return RtgsWebhookHandler(adapter, key=key, clock=clock)
    if connector_id == "card_acquirer_v1":
        key = _resolve_webhook_key(
            registration,
            mode=mode,
            credential_resolver=credential_resolver,
        )
        return CardWebhookHandler(adapter, key=key, clock=clock)
    if connector_id == "npsb_iso8583_v28":
        return NpsbWebhookHandler(adapter, mac=adapter._mac)  # noqa: SLF001
    return None


def _register_live_connector_webhook_handlers(
    webhook_registry: WebhookHandlerRegistry,
    runner: ConnectorRunner,
    registry: ConnectorRegistry,
    *,
    clock: Clock,
    credential_resolver: CredentialResolver,
) -> None:
    for registration in registry.list_all():
        if registration.active_mode not in (
            ConnectorMode.SANDBOX,
            ConnectorMode.PRODUCTION,
        ):
            continue
        adapter = runner._adapters.get(  # noqa: SLF001
            (registration.connector_id, registration.active_mode.value)
        )
        if adapter is None:
            continue
        handler = _build_live_webhook_handler(
            registration.connector_id,
            adapter,
            registration,
            mode=registration.active_mode,
            clock=clock,
            credential_resolver=credential_resolver,
        )
        if handler is not None:
            webhook_registry.register(handler)


def _build_live_payment_adapter(
    connector_id: str,
    registration: Any,
    *,
    mode: ConnectorMode,
    clock: Clock,
    credential_resolver: CredentialResolver,
    object_store: Any | None,
    pg_connection_factory: Any | None,
    transport_factory: Any | None,
):
    timeout_s = max(registration.timeout_ms / 1000.0, 0.001)
    if connector_id in {"bkash_pgw_v2", "nagad_pgw_v33", "rocket_aggregator_v1"}:
        transport = (
            HttpxTransport(timeout_s=timeout_s)
            if transport_factory is None
            else transport_factory(connector_id, registration, timeout_s)
        )
        handle_store = (
            PostgresMfsHandleStore(pg_connection_factory)
            if pg_connection_factory is not None
            else InMemoryMfsHandleStore()
        )
        idem_store = (
            ConnectorPostgresIdempotencyStore(pg_connection_factory, clock=clock)
            if pg_connection_factory is not None
            else ConnectorInMemoryIdempotencyStore()
        )
        kwargs = dict(
            mode=mode,
            clock=clock,
            transport=transport,
            credentials=credential_resolver,
            config=registration.config,
            object_store=object_store,
            handle_store=handle_store,
            idempotency=idem_store,
        )
        if connector_id == "bkash_pgw_v2":
            kwargs["token_store"] = (
                PostgresTokenStateStore(pg_connection_factory)
                if pg_connection_factory is not None
                else InMemoryTokenStateStore()
            )
        return _PAYMENT_LIVE_ADAPTERS[connector_id](**kwargs)
    if connector_id == "rtgs_iso20022_v1":
        transport = (
            _default_rtgs_transport(registration, timeout_s)
            if transport_factory is None
            else transport_factory(connector_id, registration, timeout_s)
        )
        return RtgsConnector(
            transport=transport,
            clock=clock,
            object_store=object_store,
            debtor_name=str(registration.config.get("debtor_name", "BD-PAY SETTLEMENT TSA")),
            debtor_account=str(registration.config.get("debtor_account", "TSA-BDPAY-01")),
            debtor_agent_bic=str(registration.config.get("debtor_agent_bic", "110000007")),
        )
    if connector_id == "card_acquirer_v1":
        if transport_factory is None:
            vault_proxy, acquirer = _default_card_ports(
                registration, timeout_s, credential_resolver
            )
        else:
            vault_proxy, acquirer = transport_factory(connector_id, registration, timeout_s)
        return CardAcquirerConnector(
            vault_proxy=vault_proxy,
            acquirer=acquirer,
            clock=clock,
            object_store=object_store,
        )
    if connector_id == "npsb_iso8583_v28":
        if transport_factory is None:
            transport, mac = _default_npsb_ports(
                registration,
                timeout_s,
                mode,
                clock,
                credential_resolver,
            )
        else:
            transport, mac = transport_factory(connector_id, registration, timeout_s)
        stan_store = (
            PostgresStanStore(pg_connection_factory)
            if pg_connection_factory is not None
            else None
        )
        saf_store = (
            PostgresSafStore(pg_connection_factory)
            if pg_connection_factory is not None
            else None
        )
        return NpsbConnector(
            transport=transport,
            clock=clock,
            mac=mac,
            object_store=object_store,
            stan_store=stan_store,
            saf_store=saf_store,
            acquiring_inst_id=str(
                registration.config.get("acquiring_inst_id", "11223344")
            ),
            terminal_id=str(registration.config.get("terminal_id", "BDPAY001")),
            nm_timeout_s=float(registration.config.get("nm_timeout_s", 30.0)),
        )
    raise RuntimeError(f"payment connector runner has no live adapter wiring for: {connector_id}")


def _build_configured_live_payment_connector_runner(
    clock: Clock,
    *,
    mode: ConnectorMode,
    connector_config: dict[str, Any],
    credential_resolver: CredentialResolver,
    object_store: Any | None = None,
    pg_connection_factory: Any | None = None,
    transport_factory: Any | None = None,
) -> tuple[ConnectorRunner, ConnectorRegistry]:
    configured_all = _configured_connectors(connector_config)
    unknown = sorted(
        connector_id
        for connector_id in configured_all
        if connector_id not in SPEC12_CONNECTOR_SPECS
        and connector_id not in RAIL_CONNECTOR_IDS
        and connector_id not in _NON_PAYMENT_DEPLOYMENT_CONNECTORS
    )
    if unknown:
        raise RuntimeError(
            "connector deployment config names unknown connector ids: " + ", ".join(unknown)
        )
    configured = {
        connector_id: entry
        for connector_id, entry in configured_all.items()
        if connector_id not in _NON_PAYMENT_DEPLOYMENT_CONNECTORS
    }
    unsupported = sorted(
        connector_id
        for connector_id in configured
        if connector_id not in _PAYMENT_LIVE_ADAPTERS
        and connector_id not in _SETTLEMENT_FILE_LIVE_CONNECTORS
    )
    if unsupported:
        raise RuntimeError(
            "payment connector runner has no live adapter wiring for: "
            + ", ".join(unsupported)
        )

    active_connector_ids = set(configured)
    if not active_connector_ids:
        raise RuntimeError(
            "sandbox/live payment connector runner requires at least one configured "
            "live payment or settlement-file connector"
        )

    registry = ConnectorRegistry(clock=clock, credential_resolver=credential_resolver)
    if pg_connection_factory is not None:
        breaker_store = PostgresBreakerStateStore(pg_connection_factory)
        result_store = PostgresConnectorResultStore(pg_connection_factory)
        webhook_inbound_store: object = PostgresWebhookInboundStore(pg_connection_factory)
    else:
        breaker_store = InMemoryBreakerStateStore()
        result_store = InMemoryConnectorResultStore()
        webhook_inbound_store = InMemoryWebhookInboundStore()
    breaker = CircuitBreaker(store=breaker_store, clock=clock)
    runner_idem = (
        ConnectorPostgresIdempotencyStore(pg_connection_factory, clock=clock)
        if pg_connection_factory is not None
        else ConnectorInMemoryIdempotencyStore()
    )
    runner = ConnectorRunner(
        registry=registry,
        breaker=breaker,
        clock=clock,
        result_store=result_store,
        object_store=object_store,
        idempotency=runner_idem,
    )
    # Durable webhook_inbound store for live composition (WebhookPipeline /
    # CERT inject this; gateway verify+sink path is unchanged).
    runner.webhook_inbound_store = webhook_inbound_store  # noqa: SLF001 — composition bag

    spec12_configs = {
        connector_id: {"mode": "disabled"} for connector_id in SPEC12_CONNECTOR_SPECS
    }
    for connector_id in sorted(active_connector_ids):
        if connector_id not in SPEC12_CONNECTOR_SPECS:
            continue
        spec12_configs[connector_id] = _live_registration_config(
            connector_id,
            configured[connector_id],
            mode=mode,
        )
    register_spec12_connectors(registry, spec12_configs)
    _register_rail_connectors_for_deployment(
        registry,
        configured=configured,
        active_connector_ids=active_connector_ids,
        mode=mode,
    )

    not_certified: list[str] = []
    for connector_id in sorted(active_connector_ids):
        registration = registry.get(connector_id)
        if registration.certification_status != "PASSED":
            not_certified.append(
                f"{connector_id} certification_status={registration.certification_status}"
            )
    if not_certified:
        raise RuntimeError(
            "sandbox/live connector activation requires certification_status=PASSED: "
            + ", ".join(not_certified)
        )

    disabled = registry.sweep_credentials()
    disabled_active = sorted(active_connector_ids.intersection(disabled))
    if disabled_active:
        raise RuntimeError(
            "sandbox/live connector credential refs do not resolve: "
            + ", ".join(disabled_active)
        )

    for connector_id in sorted(active_connector_ids):
        if connector_id not in _PAYMENT_LIVE_ADAPTERS:
            continue
        registration = registry.get(connector_id)
        adapter = _build_live_payment_adapter(
            connector_id,
            registration,
            mode=mode,
            clock=clock,
            credential_resolver=credential_resolver,
            object_store=object_store,
            pg_connection_factory=pg_connection_factory,
            transport_factory=transport_factory,
        )
        runner.register_adapter(connector_id, mode, adapter)

    return runner, registry


def _credential_refs_for_mode(
    connector_id: str,
    adapter_config: dict[str, Any],
    *,
    mode: ConnectorMode,
) -> list[str]:
    refs_by_mode = dict(adapter_config.get("credential_refs_by_mode") or {})
    raw_refs = refs_by_mode.get(mode.value)
    if not isinstance(raw_refs, list | tuple) or not raw_refs:
        raise RuntimeError(
            f"{connector_id} activation requires credential_refs_by_mode[{mode.value}]"
        )
    refs: list[str] = []
    for raw_ref in raw_refs:
        ref = str(raw_ref).strip()
        if not ref:
            raise RuntimeError(
                f"{connector_id} activation has a blank credential ref for {mode.value}"
            )
        refs.append(ref)
    return refs


def _assert_credential_refs_resolve(
    connector_id: str,
    refs: list[str],
    *,
    credential_resolver: CredentialResolver,
) -> None:
    unresolved: list[str] = []
    for ref in refs:
        try:
            credential_resolver.resolve(ref)
        except Exception:
            unresolved.append(ref)
    if unresolved:
        raise RuntimeError(
            f"{connector_id} credential refs do not resolve: " + ", ".join(unresolved)
        )


def _build_beftn_connector_from_config(
    *,
    mode: ConnectorMode,
    clock: Clock,
    connector_config: dict[str, Any] | None,
    credential_resolver: CredentialResolver | None,
    object_store: Any | None = None,
    transport_factory: Any | None = None,
) -> BeftnBatchConnector | None:
    if mode in (ConnectorMode.SIMULATOR, ConnectorMode.DISABLED):
        return None
    if connector_config is None:
        return None
    configured = _configured_connectors(connector_config)
    entry = configured.get(BEFTN_CONNECTOR_ID)
    if entry is None:
        return None
    if credential_resolver is None:
        raise RuntimeError(
            f"{BEFTN_CONNECTOR_ID} requested in {mode.value} mode but no "
            "credential resolver is available"
        )
    live_config = _live_registration_config(BEFTN_CONNECTOR_ID, entry, mode=mode)
    if live_config["certification_status"] != "PASSED":
        raise RuntimeError(
            f"{BEFTN_CONNECTOR_ID} activation requires certification_status=PASSED, "
            f"got {live_config['certification_status']}"
        )
    adapter_config = live_config["config"]
    refs = _credential_refs_for_mode(BEFTN_CONNECTOR_ID, adapter_config, mode=mode)
    _assert_credential_refs_resolve(
        BEFTN_CONNECTOR_ID,
        refs,
        credential_resolver=credential_resolver,
    )
    timeout_s = max(budget_for(BEFTN_CONNECTOR_ID).timeout_ms / 1000.0, 0.001)
    transport = (
        _default_beftn_transport(adapter_config, credential_resolver=credential_resolver)
        if transport_factory is None
        else transport_factory(BEFTN_CONNECTOR_ID, adapter_config, timeout_s)
    )
    credential_refs = adapter_config.get("credential_refs", {})
    detokenizer = _ConfiguredAccountDetokenizer(
        base_url=str(adapter_config["detokenizer_base_url"]),
        bearer_token=credential_resolver.resolve(
            str(credential_refs["detokenizer_bearer_token"])
        ),
        timeout_s=timeout_s,
        path=str(
            adapter_config.get(
                "detokenizer_path", "/v1/account-tokens/{token}/detokenize"
            )
        ),
    )
    signer = PgpFileSigner(
        credential_resolver,
        key_ref=str(credential_refs["pgp_signing_key"]),
    )
    return BeftnBatchConnector(
        transport=transport,
        clock=clock,
        detokenizer=detokenizer,
        signer=signer,
        config=_beftn_file_config(adapter_config),
        object_store=object_store,
    )


def _build_goaml_portal_from_config(
    *,
    mode: ConnectorMode,
    connector_config: dict[str, Any] | None,
    credential_resolver: CredentialResolver | None,
    transport_factory: Any | None = None,
) -> tuple[HttpGoamlPortal | None, dict[str, Any]]:
    if mode in (ConnectorMode.SIMULATOR, ConnectorMode.DISABLED):
        return None, {}
    if connector_config is None:
        raise RuntimeError(
            f"goAML reporter requested in {mode.value} mode but no BFIU goAML "
            "portal is configured: CONNECTOR_CONFIG_PATH does not name a connector "
            f"deployment config with a {GOAML_CONNECTOR_ID} entry. Configure portal "
            "base_url, config.rentity_id, credential_refs.session_token, and "
            "certification_status=PASSED, or set CONNECTOR_MODE=simulator|disabled."
        )
    if credential_resolver is None:
        raise RuntimeError(
            f"goAML reporter requested in {mode.value} mode but no credential "
            "resolver is available for the configured BFIU goAML portal"
        )

    configured = _configured_connectors(connector_config)
    entry = configured.get(GOAML_CONNECTOR_ID)
    if entry is None:
        raise RuntimeError(
            f"goAML reporter requested in {mode.value} mode but no BFIU goAML "
            f"portal is configured: connector deployment config has no "
            f"{GOAML_CONNECTOR_ID} entry."
        )

    entry_for_live = dict(entry)
    adapter_config = dict(entry.get("config") or {})
    credential_refs = adapter_config.get("credential_refs")
    session_ref_from_config = str(adapter_config.get("session_ref", "")).strip()
    if (
        (not isinstance(credential_refs, dict) or not credential_refs)
        and session_ref_from_config
    ):
        adapter_config["credential_refs"] = {"session_token": session_ref_from_config}
        entry_for_live["config"] = adapter_config

    live_config = _live_registration_config(GOAML_CONNECTOR_ID, entry_for_live, mode=mode)
    certification_status = str(live_config["certification_status"])
    if certification_status != "PASSED":
        raise RuntimeError(
            f"{GOAML_CONNECTOR_ID} activation requires certification_status=PASSED: "
            f"certification_status={certification_status}"
        )

    adapter_config = dict(live_config["config"])
    rentity_id = str(adapter_config.get("rentity_id", "")).strip()
    if not rentity_id or rentity_id == "BFIU-RE-0000":
        raise RuntimeError(
            f"{GOAML_CONNECTOR_ID} activation requires config.rentity_id with the "
            "real BFIU reporting entity id"
        )

    credential_refs = adapter_config.get("credential_refs")
    if not isinstance(credential_refs, dict):
        raise RuntimeError(
            f"{GOAML_CONNECTOR_ID} activation requires non-empty config.credential_refs"
        )
    session_ref = str(
        adapter_config.get("session_ref")
        or credential_refs.get("session_token")
        or credential_refs.get("session_ref")
        or ""
    ).strip()
    if not session_ref:
        raise RuntimeError(
            f"{GOAML_CONNECTOR_ID} activation requires credential_refs.session_token "
            "or config.session_ref"
        )
    adapter_config["session_ref"] = session_ref

    refs_by_mode = dict(adapter_config.get("credential_refs_by_mode") or {})
    refs = _credential_refs_for_mode(GOAML_CONNECTOR_ID, adapter_config, mode=mode)
    if session_ref not in refs:
        refs.append(session_ref)
    refs_by_mode[mode.value] = refs
    adapter_config["credential_refs_by_mode"] = refs_by_mode
    _assert_credential_refs_resolve(
        GOAML_CONNECTOR_ID,
        refs,
        credential_resolver=credential_resolver,
    )

    timeout_s = max(budget_for(GOAML_CONNECTOR_ID).timeout_ms / 1000.0, 0.001)
    transport = (
        HttpxTransport(timeout_s=timeout_s)
        if transport_factory is None
        else transport_factory(GOAML_CONNECTOR_ID, adapter_config, timeout_s)
    )
    return (
        HttpGoamlPortal(
            transport=transport,
            credentials=credential_resolver,
            config=adapter_config,
        ),
        adapter_config,
    )


def _load_connector_deployment_config(path: str) -> dict[str, Any] | None:
    if not path:
        return None
    try:
        raw = Path(path).read_text(encoding="utf-8")
        data = json.loads(raw)
    except OSError as exc:
        raise RuntimeError(f"cannot read connector deployment config {path!r}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"connector deployment config {path!r} is not valid JSON") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"connector deployment config {path!r} must be a JSON object")
    return data


def _connector_credential_resolver_from_env() -> CredentialResolver:
    resolvers: list[CredentialResolver] = [EnvCredentialResolver(os.environ)]
    openbao = OpenBaoKvCredentialResolver.from_env(os.environ)
    if openbao is not None:
        resolvers.append(openbao)
    return CompositeCredentialResolver(tuple(resolvers))


def _optional_credential_refs_for_mode(
    adapter_config: dict[str, Any], *, mode: ConnectorMode
) -> list[str]:
    refs_by_mode = dict(adapter_config.get("credential_refs_by_mode") or {})
    raw_refs = refs_by_mode.get(mode.value)
    if raw_refs is None:
        credential_refs = adapter_config.get("credential_refs")
        raw_refs = (
            list(credential_refs.values()) if isinstance(credential_refs, dict) else []
        )
    if not isinstance(raw_refs, list | tuple):
        raise RuntimeError(
            f"{SANCTIONS_FEED_CONNECTOR_ID} activation requires "
            f"credential_refs_by_mode[{mode.value}] to be a list when present"
        )
    refs = [str(ref).strip() for ref in raw_refs if str(ref).strip()]
    refs_by_mode[mode.value] = refs
    adapter_config["credential_refs_by_mode"] = refs_by_mode
    return refs


def _build_sanctions_feed_connector_from_config(
    *,
    mode: ConnectorMode,
    connector_config: dict[str, Any] | None,
    clock: Clock,
    object_store: Any | None = None,
    credential_resolver: CredentialResolver | None = None,
    transport_factory: Any | None = None,
) -> tuple[SanctionsFeedConnector | None, Any | None]:
    if mode in (ConnectorMode.SIMULATOR, ConnectorMode.DISABLED):
        return None, None
    if connector_config is None:
        return None, None

    configured = _configured_connectors(connector_config)
    entry = configured.get(SANCTIONS_FEED_CONNECTOR_ID)
    if entry is None:
        return None, None
    certification_status = str(entry.get("certification_status", "NEVER_RUN"))
    if certification_status != "PASSED":
        raise RuntimeError(
            f"{SANCTIONS_FEED_CONNECTOR_ID} activation requires "
            f"certification_status=PASSED: certification_status={certification_status}"
        )

    adapter_config = dict(entry.get("config") or {})
    # The connector consumes ``un_list_url``; deployment documents often use
    # ``base_url`` uniformly across connectors. Treat it as the full public feed
    # endpoint for this connector.
    base_url = str(adapter_config.get("base_url", "")).strip()
    if base_url and not str(adapter_config.get("un_list_url", "")).strip():
        adapter_config["un_list_url"] = base_url

    refs = _optional_credential_refs_for_mode(adapter_config, mode=mode)
    if refs:
        if credential_resolver is None:
            raise RuntimeError(
                f"{SANCTIONS_FEED_CONNECTOR_ID} activation has credential refs "
                "but no credential resolver is available"
            )
        _assert_credential_refs_resolve(
            SANCTIONS_FEED_CONNECTOR_ID,
            refs,
            credential_resolver=credential_resolver,
        )

    timeout_s = max(budget_for(SANCTIONS_FEED_CONNECTOR_ID).timeout_ms / 1000.0, 0.001)
    # Dynamic egress (redirect following): the pinned transport validates and
    # pins every destination address through the connection — never the plain
    # HttpxTransport used by the operator-fixed connectors.
    transport = (
        PinnedHttpxTransport(timeout_s=timeout_s)
        if transport_factory is None
        else transport_factory(SANCTIONS_FEED_CONNECTOR_ID, adapter_config, timeout_s)
    )
    feed_object_store = (
        object_store if object_store is not None else ConnectorInMemoryObjectStore()
    )
    return (
        SanctionsFeedConnector(
            mode=mode,
            clock=clock,
            transport=transport,
            config=adapter_config,
            object_store=feed_object_store,
        ),
        feed_object_store,
    )


def _build_connector_runner_for_settings(
    settings: Settings,
    clock: Clock,
    *,
    object_store: Any | None = None,
    connector_config: dict[str, Any] | None = None,
    credential_resolver: CredentialResolver | None = None,
    transport_factory: Any | None = None,
    pg_connection_factory: Any | None = None,
) -> tuple[ConnectorRunner, ConnectorRegistry]:
    """Select payment connector runner wiring from ``CONNECTOR_MODE``.

    Sandbox/live payment adapters exist in package modules, but this
    composition root still has no deployment config parser, credential
    resolver, base URLs, webhook URLs, or certification-activation sweep to
    safely instantiate them. Refuse those modes here so a later boot guard
    cannot be removed while this runner keeps registering simulators.
    """
    mode = mode_from_config(settings.connector_mode)
    if mode is ConnectorMode.SIMULATOR:
        return _build_simulator_connector_runner(
            clock,
            simulator_only=settings.sandbox_public,
            object_store=object_store,
        )
    if mode is ConnectorMode.DISABLED:
        return _build_disabled_connector_runner(clock, object_store=object_store)
    if connector_config is not None and credential_resolver is not None:
        return _build_configured_live_payment_connector_runner(
            clock,
            mode=mode,
            connector_config=connector_config,
            credential_resolver=credential_resolver,
            object_store=object_store,
            pg_connection_factory=pg_connection_factory,
            transport_factory=transport_factory,
        )
    raise RuntimeError(
        f"payment connector runner requested in {mode.value} mode "
        f"(CONNECTOR_MODE={settings.connector_mode!r}) but app.py has no connector "
        "deployment config or credential resolver wiring for live adapters. Refusing "
        "to register simulator adapters under a sandbox/live deployment. Configure "
        "per-connector base URLs, credential refs, certification activation, and "
        "real adapter registration here, or set CONNECTOR_MODE=simulator|disabled."
    )


# ---------------------------------------------------------------------------
# Sanctions / AML — real implementations behind the Port interfaces
# ---------------------------------------------------------------------------


class _SanctionsScreenerAdapter:
    """Wraps ``SanctionsScreener`` to satisfy ``SanctionsPort``.

    Wiring rule (binding; mirrors the compliance package's own semantics):
    - Empty watchlist → ``SanctionsBlockError`` from ``screen_subject`` →
      caught by ``PreflightPipeline`` as "unavailable" → fail-closed refusal.
      This is deliberate: an unloaded screener is a decline signal, never a
      pass (ATA 2009 freeze obligation; arch cut-last #5).
    - ``check_sync`` uses the supplied IDs as opaque identifiers (identifier-
      exact matching in the screener); name-based fuzzy matching activates only
      when a name is available via ``screen_entity``.
    """

    def __init__(
        self,
        screener: Any,  # SanctionsScreener
    ) -> None:
        self._screener = screener

    def _to_result(self, record: Any) -> SanctionsScreenResult:
        """Translate a ScreeningRecord to the platform SanctionsScreenResult."""
        hit = record.decision in ("HIT", "POTENTIAL_MATCH")
        return SanctionsScreenResult(
            hit=hit,
            hit_id=record.sanctions_hit_id if hit else None,
            match_score=int(record.top_score * 100) if record.top_score is not None else None,
            list_version_id=(
                record.list_version_ids[0] if record.list_version_ids else None
            ),
            detail=record.decision,
        )

    def check_sync(
        self, *, customer_id: str | None, merchant_id: str | None
    ) -> SanctionsScreenResult:
        """Pre-flight ID screen — fails closed when watchlist is empty.

        The subject ID is passed as an identifier (exact-match surface) so
        that an entry whose ``entry_reference`` or ``identifiers`` map contains
        the platform ID produces a HIT without requiring a name lookup.
        Subjects with no ID are screened as EXTERNAL_PARTY with a sentinel
        name (empty-watchlist guard still fires if no list is loaded).
        """
        subject_id = customer_id or merchant_id
        subject_type = "Customer" if customer_id else "Merchant"
        # Use the ID as both the screening name and the identifier value so
        # identifier-exact matching can catch a designated party by ID.
        name = subject_id or "UNKNOWN"
        identifiers: dict[str, str] = {}
        if subject_id:
            identifiers["platform_id"] = subject_id
        # screen_subject raises SanctionsBlockError on empty watchlist →
        # PreflightPipeline catches and converts to fail-closed refusal.
        record = self._screener.screen_subject(
            subject_type=subject_type,
            subject_id=subject_id,
            names=[name],
            identifiers=identifiers,
            context="PAYMENT",
        )
        return self._to_result(record)

    def screen_entity(
        self,
        *,
        entity_name: str,
        entity_type: str,
        identifiers: Any,
        context: str = "ONBOARDING",
    ) -> SanctionsScreenResult:
        """Name-based screen (KYB onboarding, disbursement recipients).

        ``context`` labels the persisted ScreeningRecord per spec/07
        (``ONBOARDING`` for the KYB path; the disbursement pre-rail gates pass
        ``PAYMENT``/``RESCREEN``); ``identifiers`` is forwarded as-is for
        exact-match augmentation.

        Defense-in-depth (audit P0 cab6a60): an empty/whitespace-only
        ``entity_name`` is refused before it ever reaches the screener rather
        than forwarded as ``names=[entity_name]`` — a blank name cannot
        honestly match anything, so silently screening it would let a caller
        that skipped its own blank-identity guard sail through as "clean".
        Mirrors the empty-watchlist fail-closed refusal below.
        """
        if not entity_name or not entity_name.strip():
            raise SanctionsBlockError(
                "sanctions screening requires a non-blank entity name; "
                "refusing to screen an empty identity",
                code="sanctions_screening_blank_identity",
            )
        # Map caller entity_type to the screener's subject_type vocabulary.
        subject_type_map = {
            "INDIVIDUAL": "Customer",
            "ENTITY": "Merchant",
            "Merchant": "Merchant",
            "Customer": "Customer",
        }
        subject_type = subject_type_map.get(entity_type, "Merchant")
        record = self._screener.screen_subject(
            subject_type=subject_type,
            subject_id=None,
            names=[entity_name],
            identifiers=dict(identifiers) if identifiers else {},
            context=context,
        )
        return self._to_result(record)


class _SanctionsFreshnessAdapter:
    """``SanctionsFreshnessPort`` over the screener's own store (BDPAY-08).

    Staleness is measured from the store the screener actually screens against
    (``SanctionsStore.active_versions()`` → ``ingested_at``), so the fail-closed
    gate and the per-recipient screen can never disagree about which list is
    active. The threshold is ``STALENESS_ALARM_H`` (24h) — the same line the
    spec/12 §G side-channel alarm draws, so the gate and the alarm share one
    definition of "stale" and the dashboard ``sanctions_feed_stale`` flag.

    No active version at all is the most obsolete book and is reported stale
    (``age_seconds=None``); the empty-list anti-false-cleared guard in the
    screener independently refuses to screen in that state.
    """

    def __init__(
        self,
        store: Any,  # SanctionsStore (active_versions())
        *,
        threshold_seconds: int = STALENESS_ALARM_H * 60 * 60,
    ) -> None:
        self._store = store
        self._threshold = threshold_seconds

    def is_stale(self, *, now: datetime) -> SanctionsFreshnessVerdict:
        versions = self._store.active_versions() or []
        ingested = [
            v.ingested_at
            for v in versions
            if isinstance(getattr(v, "ingested_at", None), datetime)
        ]
        if not ingested:
            return SanctionsFreshnessVerdict(
                is_stale=True,
                threshold_seconds=self._threshold,
                age_seconds=None,
                detail="no active sanctions list version has been ingested",
            )
        latest = max(ingested)
        age_seconds = max(0, int((now - latest).total_seconds()))
        return SanctionsFreshnessVerdict(
            is_stale=age_seconds > self._threshold,
            threshold_seconds=self._threshold,
            age_seconds=age_seconds,
            detail=(
                "stale" if age_seconds > self._threshold else "fresh"
            ),
        )


class _TransactionMonitorAdapter:
    """Wraps ``TransactionMonitor`` to satisfy ``AmlPort``.

    Pre-flight (``preflight``) is fail-closed on the signed RulePack trust root:
    no ACTIVE verified pack means no payment can move. Once the pack exists, the
    streaming monitor remains the post-event evaluator for behavioural rules.
    """

    def __init__(
        self,
        monitor: Any,  # TransactionMonitor
        *,
        audit: Any,
        clock: Clock,
        metrics: MetricsRegistry,
    ) -> None:
        self._monitor = monitor
        self._audit = audit
        self._clock = clock
        self._metrics = metrics

    def preflight(
        self,
        *,
        intent_id: str,
        customer_id: Any,
        merchant_id: str,
        amount: Any,
        method: str,
        clock: Clock,
    ) -> AmlPreflightResult:
        """Allow only when a verified ACTIVE RulePack is loaded."""
        has_active = getattr(self._monitor, "has_active_rule_pack", None)
        if not callable(has_active) or not has_active():
            self._metrics.increment(
                "aml_preflight_missing_rule_pack_total",
                labels={"method": method},
            )
            self._audit.append(
                AuditEventSpec(
                    event_type="AML_PREFLIGHT_RULE_PACK_MISSING",
                    actor_id="aml-monitor@1.0.0",
                    subject_type="PaymentIntent",
                    subject_id=intent_id,
                    payload={
                        "merchant_id": merchant_id,
                        "customer_id": customer_id,
                        "amount_minor": getattr(amount, "amount_minor", None),
                        "method": method,
                    },
                ),
                clock=clock,
            )
            return AmlPreflightResult(
                allow=False,
                defer_async=False,
                reason="aml_rule_pack_missing",
            )
        return AmlPreflightResult(allow=True, defer_async=True)

    def intake_event(self, envelope: Any) -> None:
        """Forward spec/00 §5 envelopes to the streaming evaluator.

        ``ConflictError`` for no active rule pack is audited and re-raised so the
        outbox row is retried/poisoned instead of being marked published without
        evaluation.
        """
        from bdpay.platform.errors import ConflictError

        try:
            self._monitor.intake_event(envelope)
        except ConflictError as exc:
            if exc.code != "no_active_rule_pack":
                raise
            event_type = str(envelope.get("type") or "unknown")
            subject_id = str(envelope.get("subject_id") or envelope.get("event_id") or "unknown")
            self._metrics.increment(
                "aml_monitor_missing_rule_pack_total",
                labels={"event_type": event_type},
            )
            self._audit.append(
                AuditEventSpec(
                    event_type="AML_MONITOR_RULE_PACK_MISSING",
                    actor_id="aml-monitor@1.0.0",
                    subject_type=str(envelope.get("subject_type") or "Event"),
                    subject_id=subject_id,
                    payload={
                        "event_type": event_type,
                        "error_code": exc.code,
                    },
                ),
                clock=self._clock,
            )
            raise


def _declared_rule_pack_id(artifact_bytes: bytes) -> str | None:
    try:
        artifact = json.loads(artifact_bytes.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(artifact, dict):
        return None
    manifest = artifact.get("manifest")
    if not isinstance(manifest, dict):
        return None
    pack_id = manifest.get("pack_id")
    return pack_id if isinstance(pack_id, str) else None


def _load_configured_rule_pack(
    *,
    settings: Settings,
    rule_pack_store: Any,
    signing_key_store: Any,
    audit: Any,
    outbox: Any,
    clock: Clock,
) -> None:
    """Activate a configured signed AML RulePack at boot, idempotently.

    The app never creates private key material here. Operators provide a signed
    artifact plus the public key registry fields; verification remains the same
    RulePackLoader path used by compliance tests.
    """
    if not settings.aml_rule_pack_artifact_path:
        return

    artifact_path = Path(settings.aml_rule_pack_artifact_path)
    try:
        artifact_bytes = artifact_path.read_bytes()
    except OSError as exc:
        raise RuntimeError(
            f"configured AML rule pack artifact is not readable: {artifact_path}"
        ) from exc

    signer_id = settings.aml_rule_pack_signer_id
    registry = SigningKeyRegistry(signing_key_store, env=os.environ)
    existing_key = signing_key_store.active_for_signer(signer_id)
    if existing_key is None:
        registry.register_public_key(
            signer_id=signer_id,
            public_key_b64=settings.aml_rule_pack_public_key_b64,
            registry_version=settings.aml_rule_pack_registry_version,
            now=clock.now(),
        )
    else:
        existing_key.verify_integrity()
        if (
            existing_key.public_key_b64 != settings.aml_rule_pack_public_key_b64
            or existing_key.registry_version != settings.aml_rule_pack_registry_version
        ):
            raise RuntimeError(
                f"active AML signing key for {signer_id!r} does not match configured "
                "public key/version; revoke or rotate the registry row before boot"
            )

    active = rule_pack_store.active()
    if active is not None and active.pack_id == _declared_rule_pack_id(artifact_bytes):
        return

    loader = RulePackLoader(
        rule_pack_store,
        registry,
        clock=clock,
        audit=audit,
        outbox=outbox,
    )
    loader.verify_and_load(
        artifact_bytes,
        activated_by=settings.aml_rule_pack_activated_by,
        activation_notes="boot-configured AML rule pack",
    )


# ---------------------------------------------------------------------------
# Minimal limit-data port for in-memory mode
# ---------------------------------------------------------------------------


class _InMemoryLimitDataPort:
    """All REGULAR-tier, zero history — passes every limit check."""

    def kyc_tier(self, customer_id: str) -> Any:
        from bdpay.kernel.preflight import KycTierInfo

        return KycTierInfo(tier="REGULAR", is_corporate=False)

    def wallet_balance_minor(self, customer_id: str) -> int:
        return 0

    def month_total_minor(self, customer_id: str, year_month: str) -> int:
        return 0

    def succeeded_count_24h(self, customer_id: str, method: str, now: Any) -> int:
        return 0

    def succeeded_amount_24h_minor(self, customer_id: str, method: str, now: Any) -> int:
        return 0


# ---------------------------------------------------------------------------
# Gateway port adapters
# ---------------------------------------------------------------------------


def _wire_view(value: Any) -> Any:
    """Recursively make a record dict JSON-wire-safe (conventions §7).

    ``dataclasses.asdict`` keeps ``datetime`` objects, which the gateway's
    ``JSONResponse`` cannot serialize; every timestamp crosses the wire as the
    RFC3339 ms-precision ``Z`` string (the E12 canonical form the rest of the
    edge already emits). Containers are walked; everything else passes through.
    """
    from datetime import UTC, datetime

    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    if isinstance(value, dict):
        return {k: _wire_view(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_wire_view(v) for v in value]
    return value


class _OrchestratorPaymentAdapter:
    """Adapts PaymentOrchestrator to gateway PaymentIntentPort."""

    def __init__(
        self,
        orch: PaymentOrchestrator,
        clock: Clock,
        payout_eta: PayoutEtaService | None = None,
    ) -> None:
        self._orch = orch
        self._clock = clock
        # spec/16 §G: the single-intent read gains payout_expected_by +
        # deferred_until as computed fields (additive, RFC3339 strings).
        self._payout_eta = payout_eta or PayoutEtaService()

    def create_intent(
        self, payload: Any, *, merchant_id: str, idempotency_key: str, clock: Clock
    ) -> Any:
        import dataclasses

        from bdpay.kernel.orchestrator import PaymentIntentRequest

        req = PaymentIntentRequest(
            merchant_id=merchant_id,
            amount_minor=int(payload["amount_minor"]),
            method=str(payload.get("method", "NPSB_IBFT")),
            idempotency_key=idempotency_key,
            customer_id=payload.get("customer_id"),
            currency=str(payload.get("currency", "BDT")),
            capture_method=str(payload.get("capture_method", "automatic")),
            description=payload.get("description"),
            statement_descriptor=payload.get("statement_descriptor"),
            metadata=dict(payload.get("metadata", {})),
            credits_customer_wallet=bool(payload.get("credits_customer_wallet", False)),
        )
        record = self._orch.create_intent(req)
        return _wire_view(dataclasses.asdict(record))

    def get_intent(self, payment_intent_id: str) -> Any | None:
        import dataclasses

        record = self._orch._store.get_intent(payment_intent_id)
        if record is None:
            return None
        attempts = self._orch._store.list_attempts(payment_intent_id)
        # Enrich BEFORE the wire conversion: the §G derivations branch on real
        # datetime fields (succeeded_at / deferred_until), not strings.
        return _wire_view(
            self._payout_eta.enrich_intent_view(
                dataclasses.asdict(record),
                attempt_states=tuple(attempt.status for attempt in attempts),
            )
        )

    def list_intents(
        self, *, merchant_id: str | None, filters: Any, limit: int, cursor: str | None
    ) -> Any:
        import dataclasses

        store = self._orch._store
        offset = int(cursor) if cursor else 0
        # M1 regression fix: use PaymentStore.list_intents (InMemory + Postgres)
        # instead of reaching into store._intents, which does not exist on
        # PostgresPaymentStore (it raised AttributeError in PG mode). Fetch
        # limit+1 to derive the next cursor without a separate count.
        page = store.list_intents(
            merchant_id=merchant_id,
            status=filters.get("status"),
            limit=limit + 1,
            offset=offset,
        )
        has_more = len(page) > limit
        page = page[:limit]
        nc = str(offset + limit) if has_more else None
        return [_wire_view(dataclasses.asdict(r)) for r in page], nc

    def list_intents_for_customer(
        self, *, customer_id: str, filters: Any, limit: int, cursor: str | None
    ) -> Any:
        import dataclasses

        store = self._orch._store
        offset = int(cursor) if cursor else 0
        page = store.list_intents(
            customer_id=customer_id,
            status=filters.get("status"),
            limit=limit + 1,
            offset=offset,
        )
        has_more = len(page) > limit
        page = page[:limit]
        nc = str(offset + limit) if has_more else None
        return [_wire_view(dataclasses.asdict(r)) for r in page], nc

    def confirm_intent(
        self, payment_intent_id: str, payload: Any, *, actor: str, clock: Clock
    ) -> Any:
        # confirm is async; run in a new event loop for the sync gateway boundary
        import asyncio
        import dataclasses

        outcome = asyncio.get_event_loop().run_until_complete(
            self._orch.confirm(
                payment_intent_id,
                payment_method_details=dict(payload.get("payment_method", {})),
            )
        )
        record = self._orch._store.get_intent(payment_intent_id)
        return _wire_view(dataclasses.asdict(record)) if record else {"status": outcome.status}

    async def confirm_intent_async(
        self, payment_intent_id: str, payload: Any, *, actor: str, clock: Clock
    ) -> Any:
        import dataclasses

        outcome = await self._orch.confirm(
            payment_intent_id,
            payment_method_details=dict(payload.get("payment_method", {})),
        )
        record = self._orch._store.get_intent(payment_intent_id)
        return _wire_view(dataclasses.asdict(record)) if record else {"status": outcome.status}

    def capture_intent(
        self, payment_intent_id: str, *, amount_minor: int, clock: Clock
    ) -> Any:
        import dataclasses

        record = self._orch.capture(payment_intent_id, amount_minor=amount_minor)
        return _wire_view(dataclasses.asdict(record))

    def cancel_intent(
        self, payment_intent_id: str, *, cancellation_reason: str, clock: Clock
    ) -> Any:
        import dataclasses

        record = self._orch.cancel(payment_intent_id)
        return _wire_view(dataclasses.asdict(record))

    def list_attempts(
        self, payment_intent_id: str, *, limit: int, cursor: str | None
    ) -> Any:
        import dataclasses

        attempts = self._orch._store.list_attempts(payment_intent_id)
        offset = int(cursor) if cursor else 0
        page = attempts[offset: offset + limit]
        nc = str(offset + limit) if offset + limit < len(attempts) else None
        return [_wire_view(dataclasses.asdict(a)) for a in page], nc


class _OrchestratorRefundAdapter:
    """Adapts PaymentOrchestrator to gateway RefundPort."""

    def __init__(self, orch: PaymentOrchestrator) -> None:
        self._orch = orch

    def create_refund(
        self, payload: Any, *, merchant_id: str | None, idempotency_key: str, clock: Clock
    ) -> Any:
        import dataclasses

        payment_intent_id = str(payload["payment_intent_id"])
        if merchant_id is not None:
            intent = self._orch._store.get_intent(payment_intent_id)  # noqa: SLF001
            if intent is None:
                from bdpay.platform.errors import NotFoundError

                raise NotFoundError("payment intent not found", code="payment_intent_not_found")
            if intent.merchant_id != merchant_id:
                raise AuthorizationError(
                    "payment intent belongs to a different merchant",
                    code="merchant_id_mismatch",
                )
        record = self._orch.create_refund(
            payment_intent_id,
            amount_minor=int(payload["amount_minor"]),
            reason=str(payload.get("reason", "CUSTOMER_REQUEST")),
            idempotency_key=idempotency_key,
        )
        return _wire_view(dataclasses.asdict(record))

    def get_refund(self, refund_id: str) -> Any | None:
        import dataclasses

        record = self._orch._store.get_refund(refund_id)
        return None if record is None else _wire_view(dataclasses.asdict(record))

    def list_refunds(
        self, *, merchant_id: str | None, filters: Any, limit: int, cursor: str | None
    ) -> Any:
        import dataclasses

        store = self._orch._store
        offset = int(cursor) if cursor else 0
        status = filters.get("status")
        pid = filters.get("payment_intent_id")
        # RefundRecord carries no merchant_id; ownership is always derived from
        # the parent intent (refunds -> payment_intents -> merchant_id). Both
        # branches use store methods that exist on InMemory + Postgres — never a
        # private dict, and never a non-existent r.merchant_id attribute.
        if pid is not None:
            # Intent-scoped: enforce ownership by checking the intent's merchant
            # before returning any refund for it.
            if merchant_id is not None:
                intent = store.get_intent(pid)
                if intent is None or intent.merchant_id != merchant_id:
                    return [], None
            refunds = store.list_refunds(pid)
            if status is not None:
                refunds = [r for r in refunds if r.status == status]
            page = refunds[offset: offset + limit]
            nc = str(offset + limit) if offset + limit < len(refunds) else None
        else:
            # Merchant-wide / unfiltered: JOIN refunds -> payment_intents for
            # scoping. Fetch limit+1 to derive the next cursor without a count.
            rows = store.list_refunds_for_merchant(
                merchant_id, status=status, limit=limit + 1, offset=offset
            )
            has_more = len(rows) > limit
            page = rows[:limit]
            nc = str(offset + limit) if has_more else None
        return [_wire_view(dataclasses.asdict(r)) for r in page], nc

    def list_refunds_for_customer(
        self, *, customer_id: str, filters: Any, limit: int, cursor: str | None
    ) -> Any:
        import dataclasses

        store = self._orch._store
        offset = int(cursor) if cursor else 0
        status = filters.get("status")
        # RefundRecord carries no customer_id; ownership is derived from the
        # parent intent (refunds -> payment_intents -> customer_id). Both
        # InMemoryPaymentStore and PostgresPaymentStore implement this join.
        rows = store.list_refunds_for_customer(
            customer_id, status=status, limit=limit + 1, offset=offset
        )
        has_more = len(rows) > limit
        page = rows[:limit]
        nc = str(offset + limit) if has_more else None
        return [_wire_view(dataclasses.asdict(r)) for r in page], nc


class _MerchantOnboardingAdapter:
    """Adapts MerchantOnboardingService to gateway MerchantOnboardingPort."""

    def __init__(self, svc: MerchantOnboardingService) -> None:
        self._svc = svc

    def submit_application(
        self, payload: Any, *, idempotency_key: str, clock: Clock
    ) -> Any:
        from bdpay.kernel.onboarding.kyb import MerchantApplication

        # MerchantCreate schema (gateway/schemas.py) field names differ from
        # MerchantApplication (kernel/onboarding/kyb.py) — map explicitly:
        #   schema.registration_number   → MerchantApplication.trade_license_number
        #   schema.tin                   → MerchantApplication.tin_number
        #   schema.settlement_bank_account["routing_number"]
        #                                → MerchantApplication.bank_routing_number
        #   schema.contact["phone"]      → MerchantApplication.contact_phone
        #   registration_type absent from MerchantCreate; default SOLE_PROPRIETORSHIP
        #     (matches kernel fallback at kyb.py:507; caller may pass via payload if present)
        merchant_pan = payload.get("merchant_pan", payload.get("bangla_qr_merchant_pan"))
        business_city = payload.get("business_city")
        registered_address = {"city": business_city} if business_city else {}
        settlement_bank = payload.get("settlement_bank_account") or {}
        contact = payload.get("contact") or {}
        app = MerchantApplication(
            legal_name=str(payload.get("legal_name", "")),
            registration_type=str(
                payload.get("registration_type", "SOLE_PROPRIETORSHIP")
            ),
            trade_license_number=str(
                payload.get("registration_number", payload.get("trade_license_number", ""))
            ),
            tin_number=str(payload.get("tin", payload.get("tin_number", ""))),
            primary_business_mcc=str(
                payload.get("mcc", payload.get("primary_business_mcc", ""))
            ),
            contact_phone=str(
                contact.get("phone", payload.get("contact_phone", ""))
            ),
            bank_routing_number=str(
                settlement_bank.get(
                    "routing_number", payload.get("bank_routing_number", "")
                )
            ),
            requested_services=tuple(payload.get("requested_services", ())),
            legal_name_bn=payload.get("legal_name_bn"),
            merchant_display_name=payload.get("merchant_display_name") or payload.get("trade_name"),
            business_city=business_city,
            registered_address=registered_address,
            bangla_qr_merchant_pan=merchant_pan,
            bangla_qr_enabled=bool(payload.get("bangla_qr_enabled", bool(merchant_pan))),
            website_url=payload.get("website_url"),
        )
        merchant, record = self._svc.submit_application(app, actor_id="gateway@1")
        return {
            "merchant_id": merchant.merchant_id,
            "status": merchant.kyb_status,
            "legal_name": merchant.legal_name,
        }

    def get_merchant(self, merchant_id: str) -> Any | None:
        merchant = self._svc._store.get_merchant(merchant_id)
        if merchant is None:
            return None
        return {
            "merchant_id": merchant.merchant_id,
            "status": merchant.kyb_status,
            "legal_name": merchant.legal_name,
        }

    def list_merchants(self, *, filters: Any, limit: int, cursor: str | None) -> Any:
        store = self._svc._store
        offset = int(cursor) if cursor else 0
        # M1 regression fix: use KybStore.list_merchants (InMemory + Postgres)
        # instead of reaching into store._merchants (absent on PostgresKybStore,
        # which previously made this endpoint silently return empty in PG mode).
        records = store.list_merchants(
            limit=limit + 1, offset=offset, kyb_status=filters.get("status")
        )
        has_more = len(records) > limit
        records = records[:limit]
        page = [
            {"merchant_id": m.merchant_id, "status": m.kyb_status, "legal_name": m.legal_name}
            for m in records
        ]
        nc = str(offset + limit) if has_more else None
        return page, nc

    def list_diner_merchants(self, *, filters: Any, limit: int, cursor: str | None) -> Any:
        store = self._svc._store
        offset = int(cursor) if cursor else 0
        # Public diner directory: only ACTIVE merchants, minimal PII-free fields.
        records = store.list_merchants(
            limit=limit + 1, offset=offset, kyb_status="ACTIVE"
        )
        has_more = len(records) > limit
        records = records[:limit]
        page = []
        for m in records:
            record = (
                store.get_record(m.kyb_record_id) if m.kyb_record_id else None
            )
            display_name = record.merchant_display_name if record else m.legal_name
            display_name_bn = record.legal_name_bn if record else None
            city = ""
            city_bn = ""
            if record:
                address = dict(record.registered_address or {})
                city = str(address.get("city", ""))
            cuisine, cuisine_bn = _cuisine_from_mcc(
                record.primary_business_mcc if record else None
            )
            page.append(
                {
                    "merchant_id": m.merchant_id,
                    "display_name": display_name or m.legal_name,
                    "display_name_bn": display_name_bn or "",
                    "area": city,
                    "area_bn": city_bn,
                    "cuisine": cuisine,
                    "cuisine_bn": cuisine_bn,
                    "live_offer_count": 0,
                    "best_percent_bps": None,
                }
            )
        nc = str(offset + limit) if has_more else None
        return page, nc


def _cuisine_from_mcc(mcc: str | None) -> tuple[str, str]:
    """Best-effort public cuisine label from MCC (no sensitive data)."""
    mapping: dict[str, tuple[str, str]] = {
        "5411": ("Groceries", "মুদি"),
        "5812": ("Restaurant", "রেস্তোরাঁ"),
        "5813": ("Café / Bar", "ক্যাফে / বার"),
        "5814": ("Fast Food", "ফাস্ট ফুড"),
    }
    return mapping.get(mcc or "", ("Dining", "খাবার"))


class _OfferMerchantAdapter:
    """Adapts KYB merchant state plus connector registry methods to OfferService."""

    def __init__(self, store: KybStore, registry: ConnectorRegistry) -> None:
        self._store = store
        self._registry = registry

    def is_active(self, merchant_id: str) -> bool:
        merchant = self._store.get_merchant(merchant_id)
        return merchant is not None and merchant.kyb_status == "ACTIVE"

    def supported_methods(self, merchant_id: str) -> frozenset[str]:
        if not self.is_active(merchant_id):
            return frozenset()
        methods: set[str] = set()
        for registration in self._registry.list_all():
            if registration.enabled:
                methods.update(registration.supported_methods)
        return frozenset(methods)


class _QrMerchantDirectoryAdapter:
    """Adapts the spec/08 KYB store to the qr package MerchantDirectory.

    QR issuance is only enabled once KYB has the merchant-directory facts
    required by spec/13: display name, business city, MCC, and acquirer-issued
    Bangla QR merchant PAN.
    """

    def __init__(self, store: KybStore) -> None:
        self._store = store

    @staticmethod
    def _business_city(record: Any) -> str | None:
        address = dict(getattr(record, "registered_address", {}) or {})
        for key in ("city", "business_city", "district"):
            value = address.get(key)
            if value is not None and str(value).strip():
                return str(value).strip()
        return None

    def _to_record(self, row: Any) -> QrMerchantRecord:
        record = (
            self._store.get_record(row.kyb_record_id) if row.kyb_record_id else None
        )
        if record is None:
            raise ConflictError(
                "merchant has no KYB record for QR issuance",
                code="qr_merchant_profile_missing",
            )
        if not record.bangla_qr_enabled:
            raise ConflictError(
                "BANGLA_QR is not an allowed method for this merchant",
                code="qr_method_not_allowed",
            )
        city = self._business_city(record)
        if city is None:
            raise ConflictError(
                "merchant business city is required for QR issuance",
                code="qr_merchant_city_missing",
            )
        merchant_pan = (record.bangla_qr_merchant_pan or "").strip()
        if not merchant_pan:
            raise ConflictError(
                "merchant Bangla QR PAN is required for QR issuance",
                code="qr_merchant_pan_missing",
            )
        display_name = (record.merchant_display_name or row.legal_name).strip()
        return QrMerchantRecord(
            merchant_id=row.merchant_id,
            state=row.kyb_status,
            mcc=record.primary_business_mcc if record is not None else None,
            name=display_name,
            name_bn=record.legal_name_bn,
            city=city,
            merchant_pan=merchant_pan,
            bangla_qr_allowed=record.bangla_qr_enabled,
        )

    def get(self, merchant_id: str) -> QrMerchantRecord | None:
        row = self._store.get_merchant(merchant_id)
        return None if row is None else self._to_record(row)

    def find_by_pan(self, merchant_pan: str) -> QrMerchantRecord | None:
        # Page through KybStore.list_merchants (works on InMemory + Postgres)
        # instead of reaching into store._merchants.
        offset = 0
        page_size = 500
        while True:
            rows = self._store.list_merchants(limit=page_size, offset=offset)
            for row in rows:
                record = (
                    self._store.get_record(row.kyb_record_id)
                    if row.kyb_record_id
                    else None
                )
                if (
                    record is not None
                    and record.bangla_qr_merchant_pan == merchant_pan
                ):
                    return self._to_record(row)
            if len(rows) < page_size:
                return None
            offset += page_size


class _QrIntentPortAdapter:
    """Adapts PaymentOrchestrator to the qr package PaymentIntentPort."""

    def __init__(self, orch: PaymentOrchestrator) -> None:
        self._orch = orch

    @staticmethod
    def _to_record(record: Any) -> QrIntentRecord:
        return QrIntentRecord(
            payment_intent_id=record.payment_intent_id,
            merchant_id=record.merchant_id,
            state=record.status,
            amount_minor=record.amount_minor,
            currency=record.currency,
            method=record.method,
        )

    def get(self, payment_intent_id: str) -> QrIntentRecord | None:
        record = self._orch._store.get_intent(payment_intent_id)
        return None if record is None else self._to_record(record)

    def create(
        self,
        *,
        merchant_id: str,
        amount_minor: int,
        method: str,
        metadata: dict[str, str],
    ) -> QrIntentRecord:
        from bdpay.kernel.orchestrator import PaymentIntentRequest

        # One intent per scan resolution: the resolution_id keys idempotency
        # (double-pay is already refused at the qr layer; this pins replay).
        idempotency_key = f"qr-pay:{metadata.get('resolution_id', '')}"
        record = self._orch.create_intent(
            PaymentIntentRequest(
                merchant_id=merchant_id,
                amount_minor=amount_minor,
                method=method,
                idempotency_key=idempotency_key,
                metadata=dict(metadata),
            )
        )
        return self._to_record(record)


class _QrAuditAdapter:
    """Adapts the platform AuditPort to the qr package's local AuditPort."""

    def __init__(self, audit: _AuditPortAdapter, clock: Clock) -> None:
        self._audit = audit
        self._clock = clock

    def record(
        self,
        *,
        action: str,
        subject_type: str,
        subject_id: str,
        occurred_at: Any,
        details: dict,
    ) -> None:
        self._audit.append(
            AuditEventSpec(
                event_type=action,
                actor_id="qr-service@app",
                subject_type=subject_type,
                subject_id=subject_id,
                payload=dict(details),
            ),
            clock=self._clock,
        )


class _QrEventPublisherAdapter:
    """Publishes qr.events through the shared outbox (conventions §5)."""

    def __init__(self, outbox: _OutboxPortAdapter) -> None:
        self._outbox = outbox

    def publish(
        self,
        *,
        event_type: str,
        subject_type: str,
        subject_id: str,
        occurred_at: Any,
        payload: dict,
    ) -> None:
        self._outbox.enqueue(
            build_event(
                event_type=event_type,
                subject_type=subject_type,
                subject_id=subject_id,
                producer="qr-service@app",
                topic="qr.events",
                payload=dict(payload),
                occurred_at=occurred_at,
            )
        )


class _QrIntentEventConsumer:
    """Bridges payment.events on the bus into QrService.on_intent_event."""

    def __init__(self, qr: QrService) -> None:
        self._qr = qr
        self._seen: set[str] = set()

    def consume(self, event: Any) -> None:
        event_id = event.get("event_id")
        if isinstance(event_id, str):
            if event_id in self._seen:
                return
            self._seen.add(event_id)
        event_type = str(event.get("type") or event.get("event_type") or "")
        outcome = event_type.rsplit(".", 1)[-1]
        payload = event.get("payload") or {}
        intent_id = payload.get("payment_intent_id") or event.get("subject_id")
        if outcome in ("succeeded", "failed", "cancelled") and isinstance(intent_id, str):
            self._qr.on_intent_event(intent_id, outcome)


def _cash_event_occurred_at(value: object) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise InvalidRequestError(
                "cash movement occurred_at must be timezone-aware",
                code="invalid_cash_event",
            )
        return value
    if isinstance(value, str) and value:
        text = value.removesuffix("Z") + "+00:00" if value.endswith("Z") else value
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise InvalidRequestError(
                "cash movement occurred_at must be RFC3339",
                code="invalid_cash_event",
            ) from exc
        if parsed.tzinfo is None:
            raise InvalidRequestError(
                "cash movement occurred_at must be timezone-aware",
                code="invalid_cash_event",
            )
        return parsed
    raise InvalidRequestError(
        "cash movement occurred_at is required", code="invalid_cash_event"
    )


class _CtrCashEventConsumer:
    """Bridges trusted cash movement envelopes into the CTR batch.

    This intentionally consumes only a dedicated internal event type
    (``cash_movement.recorded``). Generic payment metadata is merchant-controlled
    and must not become regulatory cash evidence.
    """

    def __init__(self, ctr_batch: CtrBatch) -> None:
        self._ctr_batch = ctr_batch

    def consume(self, event: Any) -> None:
        payload = event.get("payload") or {}
        if not isinstance(payload, dict):
            raise InvalidRequestError(
                "cash movement payload must be an object", code="invalid_cash_event"
            )
        event_id = event.get("event_id")
        account_id = payload.get("account_id")
        direction = payload.get("direction")
        channel = payload.get("channel")
        if not isinstance(event_id, str) or not event_id:
            raise InvalidRequestError(
                "cash movement event_id is required", code="invalid_cash_event"
            )
        if not isinstance(account_id, str) or not account_id:
            raise InvalidRequestError(
                "cash movement account_id is required", code="invalid_cash_event"
            )
        if not isinstance(direction, str) or not isinstance(channel, str):
            raise InvalidRequestError(
                "cash movement direction and channel are required",
                code="invalid_cash_event",
            )
        occurred_at = _cash_event_occurred_at(
            payload.get("occurred_at") or event.get("occurred_at")
        )
        self._ctr_batch.intake_cash_event(
            event_id=event_id,
            account_id=account_id,
            direction=direction,
            amount_minor=payload.get("amount_minor"),
            channel=channel,
            occurred_at=occurred_at,
        )


class _NullWebhookSink:
    def deliver(self, connector_id: str, result: Any) -> None:
        pass  # connector results go to the kernel on-demand (activation-pending)


class _IntentPartiesAdapter:
    """Adapts the kernel payment store to event_fanout.IntentPartiesPort."""

    def __init__(self, store: InMemoryPaymentStore) -> None:
        self._store = store

    def intent_parties(self, payment_intent_id: str) -> tuple[str, str | None] | None:
        record = self._store.get_intent(payment_intent_id)
        if record is None:
            return None
        return record.merchant_id, record.customer_id

    def intent_notification_context(
        self, payment_intent_id: str
    ) -> IntentNotificationContext | None:
        record = self._store.get_intent(payment_intent_id)
        if record is None:
            return None
        return IntentNotificationContext(
            merchant_id=record.merchant_id,
            customer_id=record.customer_id,
            amount_minor=record.amount_minor,
            currency=record.currency,
            merchant_display_name=record.merchant_id,
        )


class _SettlementBatchReadAdapter:
    """Adapts settlement instructions to the LR-5 merchant-slice read port."""

    def __init__(self, store: Any) -> None:
        self._store = store

    def batch_breakdown(self, batch_id: str) -> list[BatchMerchantSlice]:
        grouped: dict[str, dict[str, Any]] = {}
        for row in self._store.list_instructions(batch_id=batch_id):
            merchant_id = str(row.merchant_id)
            bucket = grouped.setdefault(
                merchant_id,
                {
                    "count": 0,
                    "net_payout_minor": 0,
                    "payout_expected_by": None,
                },
            )
            bucket["count"] += 1
            signed_amount = (
                -row.net_payout_minor
                if str(row.direction).upper() == "DEBIT"
                else row.net_payout_minor
            )
            bucket["net_payout_minor"] += signed_amount
            expected = bucket["payout_expected_by"]
            if expected is None or row.earliest_release_at > expected:
                bucket["payout_expected_by"] = row.earliest_release_at
        return [
            BatchMerchantSlice(
                merchant_id,
                int(values["count"]),
                int(values["net_payout_minor"]),
                payout_expected_by=values["payout_expected_by"],
            )
            for merchant_id, values in sorted(grouped.items())
        ]


class _RegistryHealthReadAdapter:
    """HealthSampleStore read shim over the registry's own health log
    (spec/16 §E: the public surfaces read registry health states)."""

    def __init__(self, registry: ConnectorRegistry) -> None:
        self._registry = registry

    def list_for(self, connector_id: str):  # type: ignore[no-untyped-def]
        return self._registry.health_log(connector_id)


class _SimulatorSanctionsPass:
    """SanctionsPort whose every screen is a deterministic clean pass.

    Wired ONLY into the SANDBOX_PUBLIC provisioning onboarding instance
    (spec/16 §API F: "registry/sanctions simulators return pass") so the
    UNMODIFIED spec/08 KYB FSM can reach ACTIVE without a loaded watchlist.
    The production-path merchant service keeps the real fail-closed screener;
    no regulated money ever moves in the sandbox deployment, and the payment
    pre-flight screen is NOT this object — it stays the real ``SanctionsPort``
    wired into ``PreflightPipeline`` above.
    """

    _CLEAN = SanctionsScreenResult(
        hit=False,
        match_score=0,
        list_version_id="simulator:sandbox-pass",
        detail="CLEAR",
    )

    def check_sync(
        self, *, customer_id: str | None, merchant_id: str | None
    ) -> SanctionsScreenResult:
        return self._CLEAN

    def screen_entity(
        self,
        *,
        entity_name: str,
        entity_type: str,
        identifiers: Any,
        context: str = "ONBOARDING",
    ) -> SanctionsScreenResult:
        return self._CLEAN


# ---------------------------------------------------------------------------
# Service container
# ---------------------------------------------------------------------------


@dataclass
class ServiceContainer:
    """Typed bag of fully-wired service instances.

    Returned by :func:`build_services`; the caller mounts the gateway FastAPI
    application from ``container.gateway_deps``.
    """

    settings: Settings
    clock: Clock
    ledger: LedgerService
    outbox_store: Any
    approval: ApprovalGateway
    payment_store: Any
    kyb_store: Any
    settlement_store: Any
    settlement_engine: SettlementEngine
    orchestrator: PaymentOrchestrator
    merchant_onboarding: MerchantOnboardingService
    gateway_deps: GatewayDependencies
    # spec/16 LR-5 wiring: queue-drain worker + in-process bus + consumers.
    event_bus: InProcessEventBus
    outbox_worker: OutboxWorker
    notifications: NotificationService
    webhook_endpoints: WebhookEndpointService
    webhook_deliveries: WebhookDeliveryService
    # Real compliance wiring (audit P0 fix): SanctionsScreener accessible for
    # tests and operators that need to load watchlists or query screening state.
    sanctions_screener: SanctionsScreener
    sanctions_feed_connector: SanctionsFeedConnector | None
    # Compliance subject-control side effects. Postgres mode wires the durable
    # effect/read port; memory mode keeps the recording adapter for tests.
    subject_control: Any
    # goAML filing connector — exposed so the wired mode (which follows
    # settings.connector_mode) is observable to tests and operators.
    goaml_connector: GoamlReporterConnector
    # BEFTN settlement-file connector. It is not a PaymentConnector and is kept
    # out of the payment runner; settlement/disbursement dispatch consumes this
    # typed port directly.
    beftn_connector: BeftnBatchConnector | None
    # spec/16 LR-1/LR-2/LR-3 wiring: dossier exporter + fact-correction ledger
    # + the certification-run store/registry the dossier evidence reads from.
    dossier_exports: DossierExportService
    fact_corrections: FactCorrectionService
    certification_runs: CertificationRunStore
    connector_registry: ConnectorRegistry
    # spec/16 LR-4 wiring: merchant wedge (payment links + public surfaces;
    # sandbox signup is non-None ONLY under SANDBOX_PUBLIC deployments).
    payment_links: PaymentLinkService
    public_surfaces: PublicSurfaceService
    # spec/13 Bangla QR engine (mission: QR mandate plumbing).
    qr: QrService
    # spec/18 merchant offer engine (route surface mounted by create_app).
    offers: OfferService
    # spec/17 subscription engine (route surface mounted by create_app).
    subscriptions: SubscriptionService
    # spec/17 bulk disbursement engine (route surface mounted by create_app).
    disbursements: DisbursementService
    # spec/19 participant onboarding engine (route surface mounted by create_app).
    participants: ParticipantOnboardingService
    # AML-H3 fix: compliance stores attached to the container so
    # assert_boot_invariants can verify Postgres types in prod.  Previously
    # these were local vars in build_services, invisible to the boot guard and
    # to any future refactor that might substitute InMemory* stores in prod.
    ctr_store: Any
    str_store: Any
    sanctions_store: Any
    monitor_event_store: Any
    sandbox_signups: SandboxSignupService | None = None
    metrics: MetricsRegistry = field(default_factory=MetricsRegistry)
    # Compliance scheduler (COMP-02): the registered ScheduledTask set
    # (sanctions rescreen, CTR daily aggregate, CTR filing, STR retry, goAML
    # submit sweep) plus the Scheduler that drives them.  Empty tuple / None
    # only if construction was gated off — under the standard wiring both are
    # populated.
    compliance_scheduler_tasks: tuple[ScheduledTask, ...] = ()
    compliance_scheduler: Scheduler | None = None
    # Kernel scheduler (spec/02): the TTL sweep (30-min payment TTL + 7-day
    # REQUIRES_CAPTURE auth-hold expiry) and the reversal-dispatch task that
    # drives REVERSAL_INITIATED intents through execute_reversal to release
    # held funds.  Empty tuple / None only in memory mode without a wired
    # orchestrator scheduler.
    kernel_scheduler_tasks: tuple[ScheduledTask, ...] = ()
    kernel_scheduler: Scheduler | None = None
    # Verified connector callbacks are durably accepted by the gateway and then
    # drained by this PG-only scheduler into the payment/refund state machines.
    connector_inbound: PostgresConnectorInboundEventStore | None = None
    connector_inbound_scheduler_tasks: tuple[ScheduledTask, ...] = ()
    connector_inbound_scheduler: Scheduler | None = None
    # P0 BEFTN return-file consumer: scheduled background polling of returns/.
    beftn_return_consumer: BeftnReturnConsumer | None = None
    beftn_return_scheduler_tasks: tuple[ScheduledTask, ...] = ()
    beftn_return_scheduler: Scheduler | None = None
    _owned_resources: tuple[Any, ...] = ()

    def close(self) -> None:
        for resource in reversed(self._owned_resources):
            close = getattr(resource, "close", None)
            if callable(close):
                with contextlib.suppress(Exception):
                    close()


# ---------------------------------------------------------------------------
# build_compliance_scheduler_tasks
# ---------------------------------------------------------------------------

_SANCTIONS_LIST_CODE_BY_FEED_SOURCE = {
    "UN_CONSOLIDATED": "UN_1267",
    "BFIU_DOMESTIC": "BFIU_DOMESTIC",
    "VENDOR": "BFIU_DOMESTIC",
}


def _parse_feed_dob(value: object) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _feed_entries_to_screener_inputs(
    source: str, raw: bytes
) -> list[ListEntryInput]:
    parsed = (
        parse_un_consolidated(raw)
        if source == "UN_CONSOLIDATED"
        else parse_domestic_list(raw)
    )
    entries: list[ListEntryInput] = []
    for index, item in enumerate(parsed):
        primary_name = str(item.get("primary_name", "")).strip()
        reference = str(item.get("reference_number", "")).strip()
        if not primary_name or not reference:
            raise RuntimeError(
                f"{SANCTIONS_FEED_CONNECTOR_ID} {source} entry {index} "
                "is missing primary_name or reference_number"
            )
        nationality = str(item.get("nationality", "")).strip()
        entries.append(
            ListEntryInput(
                entry_reference=reference,
                entity_kind=str(item.get("entry_type", "INDIVIDUAL")).strip().upper(),
                primary_name=primary_name,
                aliases=tuple(str(alias) for alias in item.get("aliases", ()) if alias),
                dob=_parse_feed_dob(item.get("dob")),
                nationalities=(nationality,) if nationality else (),
            )
        )
    return entries


async def _poll_sanctions_feed_into_screener(
    connector: SanctionsFeedConnector,
    screener: SanctionsScreener,
    object_store: Any,
) -> dict[str, object]:
    row = await connector.fetch_un_list()
    if row.state == "INGESTED" and row.unchanged:
        return {
            "status": "unchanged",
            "source": row.source,
            "feed_version_id": row.feed_version_id,
        }
    if row.state != "VALIDATED":
        return {
            "status": row.state.lower(),
            "source": row.source,
            "feed_version_id": row.feed_version_id,
        }

    handed_off = await connector.hand_off(row.feed_version_id)
    if handed_off.state != "HANDED_OFF":
        return {
            "status": handed_off.state.lower(),
            "source": handed_off.source,
            "feed_version_id": handed_off.feed_version_id,
        }

    raw = object_store.get(handed_off.raw_pointer)
    if raw is None:
        raise RuntimeError(
            f"{SANCTIONS_FEED_CONNECTOR_ID} validated artifact "
            f"{handed_off.raw_pointer!r} is missing from the object store"
        )
    list_code = _SANCTIONS_LIST_CODE_BY_FEED_SOURCE.get(handed_off.source)
    if list_code is None:
        raise RuntimeError(
            f"{SANCTIONS_FEED_CONNECTOR_ID} cannot map source "
            f"{handed_off.source!r} to a spec/07 list_code"
        )
    version = screener.ingest_list(
        list_code=list_code,
        entries=_feed_entries_to_screener_inputs(handed_off.source, raw),
        artifact_sha256=handed_off.content_hash,
        artifact_pointer=handed_off.raw_pointer,
        ingested_by=SANCTIONS_FEED_CONNECTOR_ID,
        connector_result_id=handed_off.feed_version_id,
    )
    connector.mark_ingested(
        handed_off.feed_version_id, spec07_list_version_id=version.list_version_id
    )
    return {
        "status": "ingested",
        "source": handed_off.source,
        "feed_version_id": handed_off.feed_version_id,
        "list_version_id": version.list_version_id,
        "entry_count": version.entry_count,
    }


def build_compliance_scheduler_tasks(
    *,
    clock: Clock,
    screener: SanctionsScreener,
    ctr_batch: CtrBatch,
    str_workflow: StrWorkflow,
    goaml_connector: GoamlReporterConnector,
    sanctions_feed_connector: SanctionsFeedConnector | None = None,
    sanctions_feed_object_store: Any | None = None,
    cash_events_enabled: bool = False,
    cash_events_wired: bool = False,
) -> tuple[ScheduledTask, ...]:
    """The spec/06 compliance scheduler task set (COMP-02).

    Each task wraps a REAL sweep over the live compliance services — no
    no-op task is registered:

    - ``sanctions_rescreen`` re-screens every subject under an open hit
      against the current ACTIVE watchlist (daily).
    - ``sanctions_feed_poll`` is registered only when ``sanctions_feed_v1`` is
      activated from deployment config; it fetches the public UN consolidated
      artifact through the connector and hands the validated artifact into the
      spec/07 screener before marking the feed row INGESTED.
    - ``ctr_daily_aggregate`` builds ctr_aggregations rows from the day's cash
      events (spec/06 step 1; spec fires this at 23:30 BST) — DAILY.  The
      current product launch surface has no CASH_AGENT / CASH_ATM movement
      producer, so ``cash_events_enabled`` defaults to False; the task remains
      registered but returns an explicit disabled status instead of emitting a
      false-red scheduler error.  If a deployment enables cash events while
      ``cash_events_wired`` is still False, the task RAISES a clear "CTR
      aggregation not wired" error every tick (surfaced via the scheduler's
      ``error_count``) rather than silently sweeping an empty bucket.
    - ``ctr_filing`` files every threshold-crossed, unfiled CTR via the goAML
      connector — DAILY, keeping the platform inside the BFIU <=3-working-day
      DFS SLA (operator-ratified daily cadence — COMP-06).
    - ``str_retry`` re-attempts every FILING_FAILED STR that still has retry
      budget, then drives the goAML submit chain forward.
    - ``goaml_submit_sweep`` drives every due QUEUED goAML filing through one
      submit attempt and polls outstanding acks (never drops a filing).

    Cadence caveat: the :class:`~bdpay.platform.scheduler.Scheduler` is
    pure-cadence (no time-of-day gate in ``WINDOW_GATES``).  A DAILY task
    therefore fires once per ~24h phased from the process boot time, NOT
    pinned to 23:30 BST; the BST anchor in spec/06 is the intended local-time
    semantics, not a guarantee the in-process cadence runner provides.
    """
    if cash_events_wired and not cash_events_enabled:
        raise ValueError("cash_events_wired requires cash_events_enabled")

    async def _poll_sanctions_feed() -> object:
        if sanctions_feed_connector is None or sanctions_feed_object_store is None:
            return {"status": "disabled", "reason": "sanctions_feed_not_configured"}
        return await _poll_sanctions_feed_into_screener(
            sanctions_feed_connector,
            screener,
            sanctions_feed_object_store,
        )

    async def _rescreen() -> object:
        return screener.rescreen_open_hits()

    async def _aggregate_ctr() -> object:
        # spec/06 step 1: build/refresh ctr_aggregations rows for today's
        # Dhaka calendar date from the day's intaken cash events.
        #
        # Launch-scope guard: the v1 assembled app has digital/card/QR rails
        # only, not a cash/agent product surface.  Keep the task registered for
        # release observability, but do not report a scheduler error for a
        # disabled product surface.
        if not cash_events_enabled:
            return {
                "status": "disabled",
                "reason": "cash_events_disabled",
                "aggregation_date": str(dhaka_date(clock.now())),
            }
        # HONESTY GUARD (no silent no-op): if cash rails are enabled but no
        # trusted producer/consumer path feeds ``intake_cash_event``, the day's
        # bucket is empty and this would build zero rows while LOOKING like a
        # working daily aggregation. Refuse: raise so the scheduler records an
        # error_count and the gap is loud.
        if not cash_events_wired:
            raise RuntimeError(
                "CTR aggregation not wired: no event-bus consumer feeds "
                "CtrBatch.intake_cash_event, so run_daily_aggregation would build "
                "ZERO ctr_aggregations rows. Prerequisite: wire a bus consumer that "
                "maps CASH_AGENT / CASH_ATM cash movements into intake_cash_event, "
                "then pass cash_events_wired=True. Refusing to run an empty "
                "aggregation that would look like a working daily sweep."
            )
        return ctr_batch.run_daily_aggregation(dhaka_date(clock.now()))

    async def _file_ctr() -> object:
        return await ctr_batch.file_pending(connector=goaml_connector)

    async def _str_retry() -> object:
        retried: list[str] = []
        now = clock.now()
        # FIX (str_retry defect): StrWorkflow has no by_status(); the FILING_FAILED
        # query lives on the underlying StrStore.  The prior code called
        # str_workflow.by_status(...) -> AttributeError every tick, swallowed by
        # the scheduler's single-tick isolation (error_count['str_retry']++), so
        # no STR was ever retried.  Read the store directly (same-package contract,
        # mirrors str_workflow_max_retry's SLF001 reach below).
        for report in str_workflow._store.by_status("FILING_FAILED"):  # noqa: SLF001
            if report.retry_count >= str_workflow_max_retry(str_workflow):
                continue
            # AR (AML-H1): honor the exponential backoff the workflow just computed.
            # str_filing runs earlier in the SAME scheduler tick and can move a
            # report to FILING_FAILED with retry_scheduled_at in the future; retrying
            # it immediately would burn the retry budget and risk a duplicate BFIU
            # filing on an ambiguous connector exception. Skip until it is due.
            if report.retry_scheduled_at is not None and report.retry_scheduled_at > now:
                continue
            str_workflow.retry_filing(report.str_id, actor_id="compliance-scheduler@app")
            await str_workflow.attempt_filing(report.str_id, connector=goaml_connector)
            retried.append(report.str_id)
        return retried

    async def _str_filing() -> object:
        # AML-H1: drive the PRIMARY STR filing path to BFIU/goAML. camlco_approve
        # leaves a report in CAMLCO_APPROVED (the 24h BFIU filing clock has started)
        # but NOTHING advanced it to FILING_PENDING (queue_filing had no caller) or
        # called attempt_filing, and _str_retry only re-files FILING_FAILED — so a
        # CAMLCO-approved STR was never filed with BFIU at all. This sweep queues
        # every approved STR and submits every pending STR. attempt_filing is
        # fail-closed (FILING_PENDING -> FILING_SUBMITTED | FILING_FAILED, never
        # drops), so a connector failure simply routes the report to the existing
        # _str_retry backoff path rather than dropping a regulatory filing.
        filed: list[str] = []
        for report in str_workflow._store.by_status("CAMLCO_APPROVED"):  # noqa: SLF001
            str_workflow.queue_filing(report.str_id)
            await str_workflow.attempt_filing(report.str_id, connector=goaml_connector)
            filed.append(report.str_id)
        for report in str_workflow._store.by_status("FILING_PENDING"):  # noqa: SLF001
            await str_workflow.attempt_filing(report.str_id, connector=goaml_connector)
            filed.append(report.str_id)
        return filed

    async def _goaml_submit() -> object:
        submitted = await goaml_connector.process_due()
        acked = await goaml_connector.poll_acks()
        return {"submitted": submitted, "acked": acked}

    async def _str_ack_confirm() -> object:
        # Bridge the goAML filing-ack to the STR FSM: poll_acks (in the
        # goaml_submit_sweep task) moves a goAML filing to ACKED, but nothing
        # consumed that to advance the STR record.  This sweep finds every
        # ACKED STR filing whose STR is still FILING_SUBMITTED and drives it
        # to FILING_CONFIRMED via confirm_filing.  Per-STR isolation: a
        # mismatched goaml_ref or a non-existent STR does not block the others.
        confirmed: list[str] = []
        # rows() returns the full append-only filing history with no
        # store-level state/report_type filter (GoamlFilingStore protocol
        # has none — see connectors/identity/goaml.py); the in-loop filter
        # below is a stopgap. A store-level filtered query (e.g. rows_by_
        # state("ACKED")) would be the proper fix to avoid an ever-growing
        # full scan on every scheduler tick.
        for row in goaml_connector.rows():
            if row.report_type != "STR" or row.state != "ACKED":
                continue
            report = str_workflow._store.get(row.report_ref)  # noqa: SLF001
            if report is None or report.status != "FILING_SUBMITTED":
                continue
            goaml_ref = row.goaml_submission_ref or row.filing_id
            try:
                str_workflow.confirm_filing(row.report_ref, goaml_ref=goaml_ref)
                confirmed.append(row.report_ref)
            except Exception:
                _app_log.warning(
                    "str_ack_confirm: confirm_filing failed for "
                    "filing_id=%s report_ref=%s",
                    row.filing_id,
                    row.report_ref,
                    exc_info=True,
                )
        return confirmed

    async def _sanctions_staleness() -> object:
        # spec/12 §G: 15-min sweep — any source whose newest INGESTED feed is
        # >24h old raises the STALE alarm (cut-last #5: a stale feed means
        # sanctions checks run against an obsolete watchlist).
        if sanctions_feed_connector is None:
            return {"status": "disabled", "reason": "sanctions_feed_not_configured"}
        return sanctions_feed_connector.staleness_check()

    tasks = [
        ScheduledTask(
            name="sanctions_rescreen",
            fn=_rescreen,
            cadence_seconds=_SANCTIONS_RESCREEN_CADENCE_SECONDS,
        ),
        ScheduledTask(
            name="ctr_daily_aggregate",
            fn=_aggregate_ctr,
            cadence_seconds=_CTR_AGGREGATE_CADENCE_SECONDS,
        ),
        ScheduledTask(
            name="ctr_filing",
            fn=_file_ctr,
            cadence_seconds=_CTR_FILING_CADENCE_SECONDS,
        ),
        ScheduledTask(
            name="str_filing",
            fn=_str_filing,
            cadence_seconds=_STR_FILING_CADENCE_SECONDS,
        ),
        ScheduledTask(
            name="str_retry",
            fn=_str_retry,
            cadence_seconds=_STR_RETRY_CADENCE_SECONDS,
        ),
        ScheduledTask(
            name="goaml_submit_sweep",
            fn=_goaml_submit,
            cadence_seconds=_GOAML_SUBMIT_CADENCE_SECONDS,
        ),
        ScheduledTask(
            name="str_ack_confirm",
            fn=_str_ack_confirm,
            cadence_seconds=_STR_ACK_CONFIRM_CADENCE_SECONDS,
        ),
    ]
    if sanctions_feed_connector is not None:
        tasks.append(
            ScheduledTask(
                name="sanctions_feed_poll",
                fn=_poll_sanctions_feed,
                cadence_seconds=_SANCTIONS_FEED_POLL_CADENCE_SECONDS,
            )
        )
        tasks.append(
            ScheduledTask(
                name="sanctions_staleness",
                fn=_sanctions_staleness,
                cadence_seconds=_SANCTIONS_STALENESS_CADENCE_SECONDS,
            )
        )
    return tuple(tasks)


# ---------------------------------------------------------------------------
# build_kernel_scheduler_tasks
# ---------------------------------------------------------------------------


def build_kernel_scheduler_tasks(
    *,
    orchestrator: PaymentOrchestrator,
    settlement_engine: SettlementEngine,
) -> tuple[ScheduledTask, ...]:
    """The spec/02 kernel scheduler task set.

    - ``ttl-sweep`` (60 s, ALWAYS) moves every TTL-expired intent per the
      spec/02 table: 30-min payment TTL states → CANCELLED / FAILED /
      REVERSAL_INITIATED; 7-day REQUIRES_CAPTURE auth-hold expiry →
      REVERSAL_INITIATED (``expires_at`` is repointed to
      ``auth_expires_at`` on entry).
    - ``reversal-dispatch`` (60 s, ALWAYS) drives every REVERSAL_INITIATED
      intent through ``execute_reversal`` so the connector reverses the rail
      instruction and the hold/charge is released.  Without this, the TTL
      sweep moves intents to REVERSAL_INITIATED but nothing executes the
      reversal — the hold stays locked forever.
    - ``settlement-confirm-resume`` (60 s, ALWAYS) re-enters
      ``SettlementEngine.confirm()`` for batches stranded in the two crash
      windows described in spec/04: close JE posted but batch still DISPATCHED,
      or batch CONFIRMED but instructions still SUBMITTED.
    """
    async def _ttl_sweep() -> object:
        return orchestrator.sweep_expired_intents()

    async def _reversal_dispatch() -> object:
        return await orchestrator.dispatch_pending_reversals()

    async def _settlement_confirm_resume() -> object:
        return settlement_engine.resume_confirmations()

    return (
        ScheduledTask(
            name="ttl-sweep",
            fn=_ttl_sweep,
            cadence_seconds=_KERNEL_TTL_SWEEP_CADENCE_SECONDS,
        ),
        ScheduledTask(
            name="reversal-dispatch",
            fn=_reversal_dispatch,
            cadence_seconds=_KERNEL_REVERSAL_DISPATCH_CADENCE_SECONDS,
        ),
        ScheduledTask(
            name="settlement-confirm-resume",
            fn=_settlement_confirm_resume,
            cadence_seconds=_KERNEL_SETTLEMENT_CONFIRM_RESUME_CADENCE_SECONDS,
        ),
    )


def str_workflow_max_retry(str_workflow: StrWorkflow) -> int:
    """The STR retry budget (mirrors the workflow's own retry guard)."""
    return str_workflow._thresholds.max_str_retry  # noqa: SLF001 - same package contract


# ---------------------------------------------------------------------------
# build_compliance_stores
# ---------------------------------------------------------------------------


def build_compliance_stores(
    settings: Settings,
) -> tuple[CtrStore, StrStore, SanctionsStore]:
    """Select the CTR / STR / sanctions filing stores by deployment mode.

    Durability fix: a prod Postgres deploy MUST get durable compliance filing
    stores — CTR aggregations, STR lifecycle rows, and the sanctions watchlist
    are regulatory records that cannot live only in process memory.  Branch on
    ``settings.database_url`` exactly like the ledger / dossier / payment-link
    stores already do:

    - ``database_url == "memory://"`` → in-memory stores (deterministic; the
      unit smoke test path; no infrastructure).
    - any other URL → the psycopg3 Postgres stores (store_pg.py), constructed
      with a lazy ``connection_factory`` lambda.  Construction is lazy (the
      Postgres store ``__init__`` only stashes the factory; ``psycopg`` is
      imported inside the methods), so this helper is callable without a live
      DB — which is what lets the durability wiring be unit-tested directly.

    Extracted from ``build_services`` so the typing can be asserted without
    booting the whole container (which eagerly connects the ledger to Postgres
    and would crash with no DB), mirroring ``build_compliance_scheduler_tasks``.
    """
    if settings.database_url == "memory://":
        return InMemoryCtrStore(), InMemoryStrStore(), InMemorySanctionsStore()

    import psycopg

    def _factory() -> Any:
        return psycopg.connect(settings.database_url)

    return (
        PostgresCtrStore(_factory),
        PostgresStrStore(_factory),
        PostgresSanctionsStore(_factory),
    )


def _sms_credential_ref(
    adapter_config: dict[str, Any],
    credential_refs: dict[str, Any],
    *,
    config_key: str,
    ref_key: str,
) -> str:
    ref = str(adapter_config.get(config_key) or credential_refs.get(ref_key) or "").strip()
    if not ref:
        raise RuntimeError(
            f"{SMS_CONNECTOR_ID} activation requires credential_refs.{ref_key} "
            f"or config.{config_key}"
        )
    adapter_config[config_key] = ref
    credential_refs[ref_key] = ref
    return ref


def _build_sms_gateway_from_config(
    *,
    mode: ConnectorMode,
    connector_config: dict[str, Any] | None,
    credential_resolver: CredentialResolver | None,
    transport_factory: Any | None = None,
) -> Any:
    if mode is ConnectorMode.SIMULATOR:
        return DeterministicSmsGateway()
    if mode is ConnectorMode.DISABLED:
        return None
    if connector_config is None:
        raise RuntimeError(
            f"sms notification transport requested in {mode.value} mode but no "
            f"{SMS_CONNECTOR_ID} deployment config is configured"
        )
    if credential_resolver is None:
        raise RuntimeError(
            f"sms notification transport requested in {mode.value} mode but no "
            "credential resolver is available"
        )

    configured = _configured_connectors(connector_config)
    entry = configured.get(SMS_CONNECTOR_ID)
    if entry is None:
        raise RuntimeError(
            f"sms notification transport requested in {mode.value} mode but "
            f"connector deployment config has no {SMS_CONNECTOR_ID} entry"
        )
    certification_status = str(entry.get("certification_status", "NEVER_RUN"))
    if certification_status != "PASSED":
        raise RuntimeError(
            f"{SMS_CONNECTOR_ID} activation requires certification_status=PASSED: "
            f"certification_status={certification_status}"
        )

    adapter_config = dict(entry.get("config") or {})
    raw_refs = adapter_config.get("credential_refs")
    credential_refs = dict(raw_refs) if isinstance(raw_refs, dict) else {}
    provider = str(adapter_config.get("provider", "twilio")).strip().lower()
    refs_for_gateway: list[str]
    if provider == "twilio":
        refs_for_gateway = [
            _sms_credential_ref(
                adapter_config,
                credential_refs,
                config_key="account_sid_ref",
                ref_key="account_sid",
            ),
            _sms_credential_ref(
                adapter_config,
                credential_refs,
                config_key="auth_token_ref",
                ref_key="auth_token",
            ),
            _sms_credential_ref(
                adapter_config,
                credential_refs,
                config_key="from_ref",
                ref_key="from",
            ),
        ]
    elif provider in {"http", "aggregator"}:
        refs_for_gateway = [
            _sms_credential_ref(
                adapter_config,
                credential_refs,
                config_key="api_key_ref",
                ref_key="api_key",
            )
        ]
        send_url = str(adapter_config.get("send_url", "")).strip()
        if not send_url:
            base_url = str(adapter_config.get("base_url", "")).rstrip("/")
            if not base_url:
                raise RuntimeError(
                    f"{SMS_CONNECTOR_ID} activation requires config.send_url "
                    "or config.base_url for provider=http"
                )
            send_path = str(adapter_config.get("send_path", "/sms/send"))
            send_url = (
                f"{base_url}{send_path if send_path.startswith('/') else '/' + send_path}"
            )
        adapter_config["send_url"] = send_url
    else:
        raise RuntimeError(
            f"{SMS_CONNECTOR_ID} activation has unsupported provider {provider!r}"
        )

    adapter_config["provider"] = provider
    adapter_config["credential_refs"] = credential_refs
    refs_by_mode = dict(adapter_config.get("credential_refs_by_mode") or {})
    refs_by_mode.setdefault(mode.value, list(credential_refs.values()))
    adapter_config["credential_refs_by_mode"] = refs_by_mode
    refs = _credential_refs_for_mode(SMS_CONNECTOR_ID, adapter_config, mode=mode)
    for ref in refs_for_gateway:
        if ref not in refs:
            refs.append(ref)
    refs_by_mode[mode.value] = refs
    _assert_credential_refs_resolve(
        SMS_CONNECTOR_ID,
        refs,
        credential_resolver=credential_resolver,
    )

    timeout_s = max(budget_for(SMS_CONNECTOR_ID).timeout_ms / 1000.0, 0.001)
    transport = (
        HttpxTransport(timeout_s=timeout_s)
        if transport_factory is None
        else transport_factory(SMS_CONNECTOR_ID, adapter_config, timeout_s)
    )
    if provider == "twilio":
        return TwilioSmsGateway(
            transport=transport,
            credentials=credential_resolver,
            config=adapter_config,
        )
    return HttpSmsGateway(
        transport=transport,
        credentials=credential_resolver,
        config=adapter_config,
    )


def _build_notification_port(
    *,
    settings: Settings,
    clock: Clock,
    connection_factory: Any,
    connector_config: dict[str, Any] | None = None,
    credential_resolver: CredentialResolver | None = None,
    transport_factory: Any | None = None,
) -> Any:
    if settings.database_url == "memory://":
        return RecordingNotificationPort()

    import asyncio

    notification_mode = mode_from_config(settings.connector_mode)
    gateway = _build_sms_gateway_from_config(
        mode=notification_mode,
        connector_config=connector_config,
        credential_resolver=credential_resolver,
        transport_factory=transport_factory,
    )
    return SmsConnectorNotificationPort(
        directory=PostgresNotificationRecipientDirectory(connection_factory),
        connector=SmsOtpConnector(
            mode=notification_mode,
            clock=clock,
            gateway=gateway,
            templates=platform_rendered_sms_templates(),
            sleep=asyncio.sleep,
        ),
    )


# ---------------------------------------------------------------------------
# build_services
# ---------------------------------------------------------------------------


def build_services(settings: Settings, clock: Clock) -> ServiceContainer:  # noqa: PLR0914
    """Wire every real implementation together.

    Parameters
    ----------
    settings:
        Platform settings.  ``settings.database_url == "memory://"`` selects
        in-memory stores for every domain; any other URL uses the Postgres
        stores (requires live DB + migrations applied).
    clock:
        Injectable clock passed into every domain that needs the current time.
        Never call ``datetime.now`` outside :mod:`bdpay.platform.clock`.

    Returns
    -------
    ServiceContainer
        Fully-wired container.  The caller (or :func:`create_app`) mounts the
        gateway FastAPI app from ``container.gateway_deps``.
    """
    owned_resources: list[Any] = []

    def _own[T](resource: T) -> T:
        owned_resources.append(resource)
        return resource

    def _pg_connection():
        import psycopg

        return psycopg.connect(settings.database_url)

    def _pg_autocommit_connection():
        import psycopg

        return psycopg.connect(settings.database_url, autocommit=True)

    # -- ledger ---------------------------------------------------------------
    # F01: the checkpoint signing key and the trusted-key set come from durable
    # custody, never from process memory. A fresh per-process
    # ``LocalEd25519Signer()`` (the previous wiring) made every restart, replica
    # and replay worker read the prior instance's historical checkpoints as
    # forged: verify_chain returned FAILED_INVALID_CHECKPOINT_SIG and engaged
    # the fail-closed write gate against valid money. Trust is supplied
    # explicitly so an UNAPPROVED signer still fails.
    signer, trusted_checkpoint_keys, _checkpoint_trust = build_checkpoint_trust(
        settings.ledger_checkpoint_trust_path
    )
    ledger_store: Any
    if settings.database_url == "memory://":
        ledger_store = MemoryLedgerStore()
    else:
        from bdpay.ledger.pg_store import PostgresLedgerStore  # type: ignore[import]

        # Deploy-rehearsal fix (2026-06-13): the constructor takes an OPEN
        # autocommit connection; passing the DSN string crashed the app tier
        # at first Postgres-backed boot ("'str' object has no attribute
        # 'autocommit'"). connect() is the class's own DSN entry point.
        ledger_store = _own(
            PostgresLedgerStore.connect(
                settings.database_url, role=settings.ledger_app_role
            )
        )

    ledger_svc = LedgerService(
        ledger_store, clock, signer, trusted_public_keys=trusted_checkpoint_keys
    )

    # Bootstrap the chain and system accounts (idempotent).
    ledger_svc.initialize_chain()
    ledger_svc.initialize_chain("AUDIT")
    chart = bootstrap_chart_of_accounts(ledger_svc)

    tcsa_store: Any
    if settings.database_url == "memory://":
        tcsa_store = InMemoryTcsaStore()
    else:
        tcsa_store = _own(
            PostgresTcsaStore.connect(
                settings.database_url, role=settings.ledger_app_role
            )
        )

    # -- adapters -------------------------------------------------------------
    ledger_port = _LedgerPortAdapter(ledger_svc, clock)
    audit_port = _AuditPortAdapter(ledger_svc)
    metrics = MetricsRegistry()
    outbox_store: Any
    if settings.database_url == "memory://":
        outbox_store = InMemoryOutboxStore()
    else:
        outbox_store = PostgresOutboxStore(_pg_connection)
    outbox_port = _OutboxPortAdapter(outbox_store)
    shared_object_store: Any | None = None
    if settings.database_url != "memory://":
        shared_object_store = PostgresObjectStore(_pg_connection)

    # -- approval -------------------------------------------------------------
    approval_store: Any
    if settings.database_url == "memory://":
        approval_store = InMemoryApprovalStore()
    else:
        approval_store = PostgresApprovalStore(_pg_connection)
    approval_gateway = ApprovalGateway(
        store=approval_store,
        audit=audit_port,  # type: ignore[arg-type]
        clock=clock,
        settings=settings,
        catalogue={
            **build_action_catalogue(settings),
            ACTIVATION_ACTION_TYPE: ApprovalRule(),
        },
    )

    # -- sanctions / AML — real ported implementations (audit P0 fix) ---------
    # The SanctionsScreener starts with an empty in-memory watchlist store.
    # An empty watchlist causes screen_subject to raise SanctionsBlockError
    # ("fail-closed" per the anti-false-cleared guard), which the
    # PreflightPipeline converts to a "sanctions screening unavailable" refusal.
    # COMP-01 fix: build_services now LOADS the configured sanctions feed at
    # boot (see load_sanctions_feed below), so a deployment with
    # SANCTIONS_FEED_SOURCE configured leaves the screener with a non-empty
    # ACTIVE list and screens for real (name + identifier-exact matching via
    # Jaro-Winkler; see bdpay/compliance/sanctions/screening.py).  With no
    # source configured the screener stays empty and fail-closed by design.
    compliance_thresholds = load_thresholds()
    # Durability fix: CTR / STR / sanctions filing stores follow database_url
    # (Postgres when set, in-memory under "memory://"), like the ledger store.
    ctr_store, str_store, sanctions_store = build_compliance_stores(settings)
    compliance_alert_store: Any
    if settings.database_url == "memory://":
        compliance_alert_store = InMemoryAlertStore()
    else:
        compliance_alert_store = PostgresAlertStore(_pg_connection)
    if settings.database_url == "memory://":
        subject_control: Any = RecordingSubjectControl()
    else:
        subject_control = PostgresSubjectControl(_pg_connection, clock=clock)
    compliance_alert_svc = AmlAlertService(
        compliance_alert_store,
        clock=clock,
        audit=audit_port,  # type: ignore[arg-type]
        outbox=outbox_port,  # type: ignore[arg-type]
        approvals=approval_gateway,  # type: ignore[arg-type]
        subject_control=subject_control,
        thresholds=compliance_thresholds,
    )
    sanctions_screener = SanctionsScreener(
        sanctions_store,
        clock=clock,
        audit=audit_port,  # type: ignore[arg-type]
        outbox=outbox_port,  # type: ignore[arg-type]
        thresholds=compliance_thresholds,
        subject_control=subject_control,
        approvals=approval_gateway,  # type: ignore[arg-type]
    )
    # COMP-01: load the configured sanctions feed so the (correctly-wired)
    # screener runs against a non-empty list.  Idempotent on the feed
    # artifact, so re-running boot does not duplicate versions.
    load_sanctions_feed(sanctions_screener, source=settings.sanctions_feed_source)
    sanctions_port = _SanctionsScreenerAdapter(sanctions_screener)
    monitor_event_store: Any
    if settings.database_url == "memory://":
        monitor_event_store = InMemoryMonitoringEventStore()
    else:
        monitor_event_store = PostgresMonitoringEventStore(_pg_connection)
    rule_pack_store: Any
    signing_key_store: Any
    if settings.database_url == "memory://":
        rule_pack_store = InMemoryRulePackStore()
        signing_key_store = InMemorySigningKeyStore()
    else:
        rule_pack_store = PostgresRulePackStore(_pg_connection)
        signing_key_store = PostgresSigningKeyStore(_pg_connection)
    _load_configured_rule_pack(
        settings=settings,
        rule_pack_store=rule_pack_store,
        signing_key_store=signing_key_store,
        audit=audit_port,
        outbox=outbox_port,
        clock=clock,
    )
    # -- COMP-02: compliance regulatory-reporting services + scheduler --------
    # CTR cash-aggregation/filing, STR lifecycle, and the goAML submit
    # connector — the services the compliance ScheduledTask set sweeps over.
    ctr_batch = CtrBatch(
        ctr_store,
        clock=clock,
        audit=audit_port,  # type: ignore[arg-type]
        outbox=outbox_port,  # type: ignore[arg-type]
        threshold_minor=settings.ctr_threshold_minor,
    )
    str_workflow = StrWorkflow(
        str_store,
        clock=clock,
        audit=audit_port,  # type: ignore[arg-type]
        outbox=outbox_port,  # type: ignore[arg-type]
        thresholds=compliance_thresholds,
        object_store=shared_object_store,
    )
    connector_config = _load_connector_deployment_config(settings.connector_config_path)
    connector_credential_resolver: CredentialResolver | None = (
        _connector_credential_resolver_from_env() if connector_config is not None else None
    )
    # goAML mode follows the deployment connector_mode.  SIMULATOR / DISABLED
    # boot in-process; SANDBOX / PRODUCTION must be backed by an operator-owned
    # connector deployment entry, credential refs, and a real BFIU reporting
    # entity id.  This keeps the portal boundary in the same activation document
    # as the payment adapters while still failing closed on missing credentials.
    _goaml_mode = mode_from_config(settings.connector_mode)
    goaml_portal, goaml_config = _build_goaml_portal_from_config(
        mode=_goaml_mode,
        connector_config=connector_config,
        credential_resolver=connector_credential_resolver,
    )
    goaml_filing_store: Any
    if settings.database_url == "memory://":
        goaml_filing_store = InMemoryGoamlFilingStore()
        if not _is_production_env(os.environ):
            goaml_config = {
                **goaml_config,
                "allow_ephemeral_filing_store": True,
            }
    else:
        goaml_filing_store = PostgresGoamlFilingStore(_pg_connection)
    goaml_connector = GoamlReporterConnector(
        mode=_goaml_mode,
        clock=clock,
        portal=goaml_portal,
        filing_store=goaml_filing_store,
        object_store=shared_object_store,
        config=goaml_config,
    )
    sanctions_feed_connector, sanctions_feed_object_store = (
        _build_sanctions_feed_connector_from_config(
            mode=_goaml_mode,
            connector_config=connector_config,
            credential_resolver=connector_credential_resolver,
            clock=clock,
            object_store=shared_object_store,
        )
    )
    compliance_scheduler_tasks = build_compliance_scheduler_tasks(
        clock=clock,
        screener=sanctions_screener,
        ctr_batch=ctr_batch,
        str_workflow=str_workflow,
        goaml_connector=goaml_connector,
        sanctions_feed_connector=sanctions_feed_connector,
        sanctions_feed_object_store=sanctions_feed_object_store,
        # The current app launch surface exposes no CASH_AGENT / CASH_ATM rail.
        # If an operator enables cash scope, require a trusted producer/consumer
        # path before the daily aggregate is allowed to sweep.
        cash_events_enabled=settings.cash_events_enabled,
        cash_events_wired=settings.cash_events_wired,
    )
    compliance_scheduler = Scheduler(compliance_scheduler_tasks, clock=clock)

    # -- kernel stores and preflight ------------------------------------------
    # M1 production-wiring: the kernel money + onboarding stores follow
    # database_url like the ledger/compliance/qr stores (mirrors the guarded
    # pattern at the api-key / monitoring / qr stores below). Before this, the
    # InMemory stores were wired UNCONDITIONALLY, so a Postgres deployment ran
    # payments + KYB on volatile in-process memory and persisted nothing.
    payment_store: Any
    kyb_store: Any
    if settings.database_url == "memory://":
        payment_store = InMemoryPaymentStore()
        kyb_store = InMemoryKybStore()
    else:
        from bdpay.kernel.onboarding.kyb_repository import PostgresKybStore
        from bdpay.kernel.repository import PostgresPaymentStore

        payment_store = PostgresPaymentStore(lambda: psycopg.connect(settings.database_url))
        kyb_store = PostgresKybStore(lambda: psycopg.connect(settings.database_url))
    merchant_gate_port = KybStoreMerchantGates(kyb_store)
    limit_data_port: Any
    if settings.database_url == "memory://":
        limit_data_port = _InMemoryLimitDataPort()
    else:
        import psycopg

        limit_data_port = PostgresLimitDataPort(
            lambda: psycopg.connect(settings.database_url)
        )
    # AML-H2 (path d): TransactionMonitor constructed AFTER payment_store so it
    # can rehydrate its rolling-window history from the last 72 h of charged
    # attempts without requiring a new table.  Moved from its original position
    # before the kernel store block — see the block comment in __init__.
    transaction_monitor = TransactionMonitor(
        alerts=compliance_alert_svc,
        packs=rule_pack_store,
        events=monitor_event_store,
        clock=clock,
        audit=audit_port,  # type: ignore[arg-type]
        thresholds=compliance_thresholds,
        # AML-H2: rehydrate rolling-window history from durable payment data on
        # startup (path d — no new table; direction/sender/beneficiary all
        # derivable from payment_intents + payment_attempts).
        payment_store=payment_store,
    )
    aml_port = _TransactionMonitorAdapter(
        transaction_monitor,
        audit=audit_port,
        clock=clock,
        metrics=metrics,
    )
    limit_enforcer = LimitEnforcer(
        merchants=merchant_gate_port,
        data=limit_data_port,
        settings=settings,
        subject_controls=subject_control,
    )
    preflight = PreflightPipeline(
        limits=limit_enforcer,
        sanctions=sanctions_port,  # type: ignore[arg-type]
        aml=aml_port,  # type: ignore[arg-type]
    )

    # -- connector runner + routing -------------------------------------------
    connector_runner, connector_registry = _build_connector_runner_for_settings(
        settings,
        clock,
        object_store=shared_object_store,
        connector_config=connector_config,
        credential_resolver=connector_credential_resolver,
        pg_connection_factory=(
            None if settings.database_url == "memory://" else _pg_connection
        ),
    )
    beftn_connector = _build_beftn_connector_from_config(
        mode=mode_from_config(settings.connector_mode),
        clock=clock,
        connector_config=connector_config,
        credential_resolver=connector_credential_resolver,
        object_store=shared_object_store,
    )

    routing = RoutingEngine(
        health=_ConnectorRunnerHealth(connector_runner, connector_registry),
    )

    # -- payment orchestrator -------------------------------------------------
    orchestrator = PaymentOrchestrator(
        store=payment_store,
        ledger=ledger_port,  # type: ignore[arg-type]
        audit=audit_port,  # type: ignore[arg-type]
        outbox=outbox_port,  # type: ignore[arg-type]
        runner=connector_runner,
        routing=routing,
        preflight=preflight,
        clock=clock,
        # FC-02 is VERIFY-BEFORE-EXTERNAL: until verified, only the spec/04
        # baseline rules apply and every class resolves at the 115 bps ceiling
        # (bit-identical to the tiered-rows-absent DB state).
        fee_rules=default_fee_rules(),
        merchant_profiles=KybStoreMerchantFeeProfiles(kyb_store),
        # COMP-08: the PSP-side merchant-of-record / fund-aggregation gate.
        # Env-driven (PSP_LICENSE_ACTIVE), default false — capture refuses to
        # book a MERCHANT_SETTLEMENT obligation until the platform's PSP licence
        # is live.  Mirrors the PSO pso_license_active activation gate.
        psp_license_active=settings.psp_license_active,
        transaction_factory=(
            None if settings.database_url == "memory://" else _pg_connection
        ),
    )

    # -- spec/04 settlement engine -------------------------------------------
    settlement_store: Any
    if settings.database_url == "memory://":
        settlement_store = InMemorySettlementStore()
    else:
        settlement_store = _own(
            PostgresSettlementStore.connect(
                settings.database_url, role=settings.ledger_app_role
            )
        )

    settlement_engine = SettlementEngine(
        store=settlement_store,
        ledger=ledger_svc,
        clock=clock,
        tcsa_gate=TcsaDispatchGate(tcsa_store=tcsa_store, clock=clock),
        transit_accounts={
            "beftn_batch_v2": chart[("IN_TRANSIT", "beftn_transit")],
            "npsb_iso8583_v28": chart[("IN_TRANSIT", "npsb_transit")],
            "rtgs_iso20022_v1": chart[("IN_TRANSIT", "rtgs_transit")],
        },
        merchant_settlement_resolver=merchant_settlement_id,
        tcsa_account_id=chart[("SPONSOR_BANK_TCSA", "tcsa_main")],
    )

    # -- kernel scheduler (spec/02 TTL sweep + reversal dispatch + settlement resume)
    kernel_scheduler_tasks = build_kernel_scheduler_tasks(
        orchestrator=orchestrator,
        settlement_engine=settlement_engine,
    )
    kernel_scheduler = Scheduler(kernel_scheduler_tasks, clock=clock)

    # -- spec/18 merchant offers ---------------------------------------------
    offer_store: Any
    if settings.database_url == "memory://":
        offer_store = InMemoryOfferStore()
    else:
        offer_store = PostgresOfferStore(_pg_connection)
    offers = OfferService(
        store=offer_store,
        merchants=_OfferMerchantAdapter(kyb_store, connector_registry),
        audit=audit_port,  # type: ignore[arg-type]
        outbox=outbox_port,  # type: ignore[arg-type]
        clock=clock,
    )

    # -- spec/17 subscriptions -----------------------------------------------
    subscription_store: Any
    if settings.database_url == "memory://":
        subscription_store = InMemorySubscriptionStore()
    else:
        subscription_store = PostgresSubscriptionStore(_pg_connection)
    subscriptions = SubscriptionService(
        store=subscription_store,
        payment_orchestrator=orchestrator,
        audit=audit_port,  # type: ignore[arg-type]
        outbox=outbox_port,  # type: ignore[arg-type]
    )

    # -- spec/17 bulk disbursements ------------------------------------------
    disbursement_store: Any
    if settings.database_url == "memory://":
        disbursement_store = InMemoryDisbursementStore()
    else:
        disbursement_store = PostgresDisbursementStore(_pg_connection)
    disbursements = DisbursementService(
        store=disbursement_store,
        ledger=ledger_port,  # type: ignore[arg-type]
        audit=audit_port,  # type: ignore[arg-type]
        outbox=outbox_port,  # type: ignore[arg-type]
        approvals=approval_gateway,  # type: ignore[arg-type]
        clock=clock,
        connector_runner=connector_runner,
        beftn_connector=beftn_connector,
        # Audit P0 cab6a60: payout recipients screen fail-closed against the
        # same real screener as payment pre-flight — never a simulator.
        sanctions=sanctions_port,
        # BDPAY-08: opt-in fail-closed sanctions-list freshness gate. The
        # adapter reads the screener's own store; the flag (default OFF)
        # preserves the legacy alarm-only posture.
        sanctions_freshness=_SanctionsFreshnessAdapter(sanctions_store),
        stale_sanctions_fail_closed=settings.sanctions_stale_fail_closed,
        alerts=compliance_alert_svc,
        transaction_factory=(None if settings.database_url == "memory://" else _pg_connection),
    )

    def _default_beftn_return_ops_alert(summary: str, details: dict[str, Any]) -> None:
        logging.getLogger("bdpay.kernel.disbursement.beftn_return_consumer").warning(
            "%s: %s", summary, details
        )

    beftn_return_consumer: BeftnReturnConsumer | None = None
    beftn_return_scheduler_tasks: tuple[ScheduledTask, ...] = ()
    beftn_return_scheduler: Scheduler | None = None
    if beftn_connector is not None:
        beftn_return_consumer = BeftnReturnConsumer(
            connector=beftn_connector,
            disbursement_service=disbursements,
            clock=clock,
            metrics=metrics,
            ops_alert=_default_beftn_return_ops_alert,
        )
        beftn_return_scheduler_tasks = (
            ScheduledTask(
                name="beftn_return_poll",
                fn=beftn_return_consumer.process_once,
                cadence_seconds=60,
            ),
        )
        beftn_return_scheduler = Scheduler(beftn_return_scheduler_tasks, clock=clock)

    # -- spec/19 participant onboarding --------------------------------------
    participant_store: Any
    if settings.database_url == "memory://":
        participant_store = InMemoryParticipantStore()
        participant_object_store = InMemoryParticipantObjectStore()
        participant_ndc = InMemoryNetDebitCapRegistry()
    else:
        participant_store = PostgresParticipantStore(_pg_connection)
        participant_object_store = shared_object_store
        participant_ndc = _PostgresParticipantNdcAdapter(_pg_connection)
    participants = ParticipantOnboardingService(
        participant_store,
        clock=clock,
        audit=audit_port,  # type: ignore[arg-type]
        outbox=outbox_port,  # type: ignore[arg-type]
        approvals=approval_gateway,  # type: ignore[arg-type]
        settings=settings,
        config=ParticipantOnboardingConfig.from_env(os.environ),
        ndc=participant_ndc,
        ledger=_ParticipantLedgerAdapter(ledger_svc),
        battery_gate=InMemoryPsoBatteryGate(fresh_and_passed=False),
        object_store=participant_object_store,
    )

    # -- gateway settings + API key service (needed by merchant onboarding) ----
    # Built here (ahead of the full gateway block) so _GatewayKeyAdapter can
    # be wired into merchant_svc at construction time.
    gateway_env = {
        "GATEWAY_JWT_SECRET": os.environ.get("GATEWAY_JWT_SECRET", "dev-jwt-secret-32chars!!"),
        "GATEWAY_HMAC_MASTER_KEY_HEX": os.environ.get(
            "GATEWAY_HMAC_MASTER_KEY_HEX", "ab" * 32
        ),
        # Pass the deployment env so GatewaySettings fails closed on the dev
        # default secrets in production (audit P0 bdpay-weak-creds-boot).
        "BDPAY_ENV": os.environ.get("BDPAY_ENV", ""),
        "VAULT_ENV": os.environ.get("VAULT_ENV", ""),
    }
    for _env_name in (
        "GATEWAY_BCRYPT_ROUNDS",
        "GATEWAY_RATE_LIMIT_MODE",
        # Informational labels for the GET / service card (no secrets).
        "CONNECTOR_MODE",
        "BDPAY_BUILD_COMMIT",
        "BDPAY_PORTAL_URL",
        *RUNTIME_SHA_ENV_KEYS,
        *DEPLOY_ID_ENV_KEYS,
    ):
        if _env_name in os.environ:
            gateway_env[_env_name] = os.environ[_env_name]
    gw_settings = GatewaySettings.from_env(gateway_env)
    api_key_repo: Any
    if settings.database_url == "memory://":
        api_key_repo = InMemoryApiKeyRepository()
    else:
        api_key_repo = _own(PostgresApiKeyRepository(_pg_connection()))
    api_keys_svc = ApiKeyService(api_key_repo, clock=clock, settings=gw_settings)

    # -- merchant onboarding --------------------------------------------------
    merchant_svc = MerchantOnboardingService(
        store=kyb_store,
        sanctions=sanctions_port,  # type: ignore[arg-type]
        approvals=approval_gateway,  # type: ignore[arg-type]
        audit=audit_port,  # type: ignore[arg-type]
        outbox=outbox_port,  # type: ignore[arg-type]
        clock=clock,
        ledger=_MerchantLedgerAdapter(ledger_svc),
        gateway_keys=_GatewayKeyAdapter(api_keys_svc),
    )

    # -- customer intake / eKYC read surface ----------------------------------
    if settings.database_url == "memory://":
        customer_svc = InMemoryCustomerService(
            balance_cap_minor=settings.ekyc_simplified.wallet_balance_cap_minor
        )
    else:
        kyc_store = PostgresKycStore(_pg_connection)
        customer_svc = PostgresCustomerService(
            _pg_connection,
            kyc_store=kyc_store,
            balance_cap_minor=settings.ekyc_simplified.wallet_balance_cap_minor,
        )

    # -- spec/16 LR-1/LR-2/LR-3: fact corrections + dossier exporter ------------
    fact_correction_store: Any
    if settings.database_url == "memory://":
        fact_correction_store = InMemoryFactCorrectionStore()
    else:
        fact_correction_store = PostgresFactCorrectionStore(_pg_connection)
    fact_corrections = FactCorrectionService(
        fact_correction_store,
        audit=audit_port,  # type: ignore[arg-type]
        outbox=outbox_port,  # type: ignore[arg-type]
        clock=clock,
    )
    certification_runs: CertificationRunStore
    if settings.database_url == "memory://":
        certification_runs = InMemoryCertificationRunStore()
    else:
        certification_runs = PostgresCertificationRunStore(
            _pg_connection,
            registration_lookup=connector_registry.get,
        )
    dossier_store: Any
    if settings.database_url == "memory://":
        dossier_store = InMemoryDossierExportStore()
    else:
        import psycopg

        dossier_store = PostgresDossierExportStore(
            lambda: psycopg.connect(settings.database_url)
        )
    dossier_exports = DossierExportService(
        dossier_store,
        clock=clock,
        audit=audit_port,  # type: ignore[arg-type]
        outbox=outbox_port,  # type: ignore[arg-type]
        object_store=(
            InMemoryObjectStore() if shared_object_store is None else shared_object_store
        ),
        certification=CertificationRunReadAdapter(certification_runs, connector_registry),
        chain_verify=LedgerChainVerifyAdapter(ledger_svc),
        trial_balance=LedgerTrialBalanceAdapter(ledger_svc),
        replay=ChainReplayAdapter(LedgerReplayReadAdapter(ledger_svc)),
        fact_register=FactCorrectionRegisterAdapter(fact_correction_store),
        case_evidence=AuditTrailCaseEvidenceAdapter(ledger_svc),
        sponsor_pack=TrackedSponsorPackAdapter(),
    )

    # -- LR-5: event bus, outbox worker, notifications, outbound webhooks -----
    event_bus = InProcessEventBus()
    outbox_worker = OutboxWorker(
        outbox_store,
        event_bus,
        clock=clock,
        worker_id="outbox-worker@app",
    )
    if settings.cash_events_wired:
        cash_event_consumer = _CtrCashEventConsumer(ctr_batch)
        event_bus.subscribe(
            "cash_movement.recorded",
            cash_event_consumer.consume,
            name="ctr-cash-intake:cash_movement.recorded",
        )
    event_bus.subscribe(
        "payment_attempt.charged",
        aml_port.intake_event,
        name="aml-monitor:payment_attempt.charged",
    )
    template_registry = TemplateRegistry()
    notification_store: Any
    if settings.database_url == "memory://":
        notification_store = InMemoryNotificationStore()
    else:
        notification_store = _own(PostgresNotificationStore(_pg_connection()))
    notifications = NotificationService(
        notification_store,
        template_registry,
        _build_notification_port(
            settings=settings,
            clock=clock,
            connection_factory=_pg_connection,
            connector_config=connector_config,
            credential_resolver=connector_credential_resolver,
        ),
        clock=clock,
    )
    webhook_store: Any
    if settings.database_url == "memory://":
        webhook_store = InMemoryWebhookEndpointStore()
    else:
        webhook_store = _own(PostgresWebhookEndpointStore(_pg_connection()))
    webhook_endpoints_svc = WebhookEndpointService(
        webhook_store,
        clock=clock,
        master_key=gw_settings.hmac_master_key,
        grace_hours=settings.webhook_secret_grace_hours,
    )
    webhook_transport: Any
    if settings.database_url == "memory://":
        webhook_transport = RecordingWebhookTransport()
    else:
        webhook_transport = HttpxWebhookTransport()
    webhook_deliveries_svc = WebhookDeliveryService(
        webhook_store,
        webhook_endpoints_svc,
        webhook_transport,
        clock=clock,
        outbox=outbox_port,
    )
    fanout = ResilienceEventFanout(
        webhooks=webhook_deliveries_svc,
        notifications=notifications,
        intents=_IntentPartiesAdapter(payment_store),
        settlement=_SettlementBatchReadAdapter(settlement_store),
    )
    for event_type, handler in fanout.handlers().items():
        event_bus.subscribe(event_type, handler, name=f"lr5-fanout:{event_type}")

    # -- gateway dependencies -------------------------------------------------
    idem_store: Any
    if settings.database_url == "memory://":
        idem_store = InMemoryIdempotencyStore(
            ttl_seconds=gw_settings.idempotency_ttl_seconds,
            max_stored_response_bytes=gw_settings.max_stored_response_bytes,
        )
    else:
        idem_store = _own(
            PostgresIdempotencyStore(
                _pg_connection(),
                ttl_seconds=gw_settings.idempotency_ttl_seconds,
                max_stored_response_bytes=gw_settings.max_stored_response_bytes,
            )
        )
    if settings.database_url == "memory://":
        rate_store = InMemoryRateLimitStore()
    else:
        import redis

        rate_store = _own(RedisRateLimitStore(redis.Redis.from_url(settings.redis_url)))
    if settings.database_url == "memory://":
        otp_sessions = InMemoryOtpSessionStore(ttl_seconds=gw_settings.otp_session_ttl_seconds)
    else:
        otp_sessions = PostgresOtpSessionStore(
            _pg_connection,
            ttl_seconds=gw_settings.otp_session_ttl_seconds,
        )
    webhook_registry = WebhookHandlerRegistry()
    if connector_credential_resolver is not None:
        _register_live_connector_webhook_handlers(
            webhook_registry,
            connector_runner,
            connector_registry,
            clock=clock,
            credential_resolver=connector_credential_resolver,
        )

    payment_adapter = _OrchestratorPaymentAdapter(orchestrator, clock, PayoutEtaService())
    refund_adapter = _OrchestratorRefundAdapter(orchestrator)
    merchant_adapter = _MerchantOnboardingAdapter(merchant_svc)

    # -- spec/16 LR-4: payment links + public surfaces + sandbox signup --------
    links_store: Any
    if settings.database_url == "memory://":
        links_store = InMemoryPaymentLinkStore()
    else:
        import psycopg

        links_store = PostgresPaymentLinkStore(
            lambda: psycopg.connect(settings.database_url)
        )
    payment_links = PaymentLinkService(
        links_store,
        audit=audit_port,  # type: ignore[arg-type]
        outbox=outbox_port,  # type: ignore[arg-type]
        intents=payment_adapter,  # type: ignore[arg-type]
        public_host=os.environ.get("PAYMENT_LINK_PUBLIC_HOST", "bdpay.example"),
        default_ttl_hours=settings.payment_link_default_ttl_hours,
    )
    link_consumer = PaymentLinkEventConsumer(payment_links, clock=clock)
    for event_type in (
        "payment_intent.succeeded",  # ACTIVE -> PAID (single_use)
        "payment_intent.failed",  # release the open checkout claim
        "payment_intent.cancelled",
        "payment_intent.expired",
    ):
        event_bus.subscribe(
            event_type, link_consumer.consume, name=f"lr4-links:{event_type}"
        )

    public_surfaces = PublicSurfaceService(
        RegistryReadAdapter(
            connector_registry,
            _RegistryHealthReadAdapter(connector_registry),  # type: ignore[arg-type]
            certification_runs,
        ),
        clock,
    )
    for event_type in (
        "certification_run.completed",
        "connector_registration.health_changed",
        "connector_registration.circuit_opened",
        "connector_registration.circuit_half_opened",
        "connector_registration.circuit_closed",
    ):
        event_bus.subscribe(
            event_type, public_surfaces.consume, name=f"lr4-public:{event_type}"
        )

    sandbox_signups: SandboxSignupService | None = None
    if settings.sandbox_public:
        # §API F boot invariants (release-blocking): refuse boot on any
        # non-SIMULATOR connector, then prove the live-key mint path raises.
        sandbox_key_minter = SandboxKeyMinter(api_keys_svc)
        SandboxPublicGuard().assert_boot_invariants(
            connector_registry, key_minter=sandbox_key_minter
        )
        # Provisioning drives the UNMODIFIED spec/08 KYB FSM against
        # simulator-backed checks ("registry/sanctions simulators return
        # pass", spec/16 §F) — a dedicated onboarding instance so the
        # production-path merchant_svc keeps the fail-closed real screener.
        sandbox_onboarding = MerchantOnboardingService(
            store=kyb_store,
            sanctions=_SimulatorSanctionsPass(),  # type: ignore[arg-type]
            approvals=approval_gateway,  # type: ignore[arg-type]
            audit=audit_port,  # type: ignore[arg-type]
            outbox=outbox_port,  # type: ignore[arg-type]
            clock=clock,
            ledger=_MerchantLedgerAdapter(ledger_svc),
        )
        sandbox_signups = SandboxSignupService(
            (
                InMemorySandboxSignupStore()
                if settings.database_url == "memory://"
                else PostgresSandboxSignupStore(_pg_connection)
            ),
            onboarding=sandbox_onboarding,
            key_minter=sandbox_key_minter,
            notifications=notifications,
            template_registry=template_registry,
            audit=audit_port,  # type: ignore[arg-type]
            outbox=outbox_port,  # type: ignore[arg-type]
            token_secret=os.environ.get("SANDBOX_TOKEN_SECRET", gw_settings.jwt_secret),
            sandbox_base_url=os.environ.get(
                "SANDBOX_BASE_URL", "https://api.sandbox.bdpay.example"
            ),
            docs_url=os.environ.get(
                "SANDBOX_DOCS_URL", "https://docs.bdpay.example/sandbox"
            ),
            signup_ttl_hours=settings.sandbox_signup_ttl_hours,
        )

    # -- spec/13 Bangla QR: engine wired to kernel/KYB/audit/outbox -------------
    qr_config = BanglaQrConfig(
        acquirer_id=settings.qr_acquirer_id,
        psp_sub_id=settings.qr_psp_sub_id,
        template_root_id=settings.qr_template_root_id,
        scheme_guid=settings.qr_scheme_guid,
    )
    qr_service = QrService(
        config=qr_config,
        repository=(
            InMemoryQrRepository()
            if settings.database_url == "memory://"
            else PostgresQrRepository(_pg_autocommit_connection())
        ),
        merchants=_QrMerchantDirectoryAdapter(kyb_store),
        intents=_QrIntentPortAdapter(orchestrator),
        audit=_QrAuditAdapter(audit_port, clock),
        events=_QrEventPublisherAdapter(outbox_port),
        clock=clock,
        scan_only=settings.qr_scan_only,
    )
    # Interop matrix seed rows (spec/13 §F qtest_ corpus) — idempotent upserts.
    qr_service.load_interop_cases(build_qr_interop_cases(qr_config, clock.now()))
    # payment.events drive the dynamic-payload FSM (ISSUED/SCANNED -> PAID/...).
    qr_intent_consumer = _QrIntentEventConsumer(qr_service)
    for event_type in (
        "payment_intent.succeeded",
        "payment_intent.failed",
        "payment_intent.cancelled",
    ):
        event_bus.subscribe(
            event_type, qr_intent_consumer.consume, name=f"qr-fsm:{event_type}"
        )

    operator_store: Any
    operator_directory: Any
    if settings.database_url == "memory://":
        operator_store = InMemoryOperatorStore()
        operator_directory = InMemoryOperatorDirectory()
        if settings.sandbox_public:
            operator_id = "you@bdpay.example"
            operator_directory.register(operator_id, "demo", ("READ_ONLY",))
            operator_store.save_enrollment(
                OperatorTotpEnrollment(
                    operator_id=operator_id,
                    totp_secret_enc=encrypt_blob(
                        gw_settings.hmac_master_key, b"JBSWY3DPEHPK3PXP"
                    ),
                    backup_code_hashes=(),
                    enrolled_at=clock.now(),
                )
            )
    else:
        operator_store = _own(PostgresOperatorStore(_pg_connection()))
        operator_directory = _own(PostgresOperatorDirectory(_pg_connection()))

    webhook_sink: Any
    connector_inbound: PostgresConnectorInboundEventStore | None = None
    connector_inbound_scheduler_tasks: tuple[ScheduledTask, ...] = ()
    connector_inbound_scheduler: Scheduler | None = None
    if settings.database_url == "memory://":
        webhook_sink = _NullWebhookSink()
    else:
        webhook_sink = PostgresWebhookSink(_pg_connection, clock=clock)
        connector_inbound = PostgresConnectorInboundEventStore(_pg_connection, clock=clock)

        async def _connector_inbound_drain() -> object:
            return await connector_inbound.drain_once(orchestrator, limit=100)

        connector_inbound_scheduler_tasks = (
            ScheduledTask(
                name="connector_inbound_drain",
                fn=_connector_inbound_drain,
                cadence_seconds=5,
            ),
        )
        connector_inbound_scheduler = Scheduler(
            connector_inbound_scheduler_tasks, clock=clock
        )

    webhook_pipeline = None
    if settings.gateway_webhook_pipeline:
        # BP-G4d gated CERT path: WebhookPipeline + runner.webhook_inbound_store,
        # dual-writing accepted results into webhook_sink (money-path drain intact).
        webhook_pipeline, _handoff = build_gateway_webhook_pipeline(
            clock=clock,
            webhook_registry=webhook_registry,
            webhook_sink=webhook_sink,
            connector_runner=connector_runner,
            connector_mode=settings.connector_mode,
        )

    gw_deps = GatewayDependencies(
        settings=gw_settings,
        clock=clock,
        api_keys=api_keys_svc,
        policies=(
            *build_policies(),
            *offer_route_policies(),
            *subscription_route_policies(),
            *disbursement_route_policies(),
            *participant_route_policies(),
        ),
        operators=OperatorAuthService(
            operator_store,
            operator_directory,
            clock=clock,
            settings=gw_settings,
        ),
        otp_sessions=otp_sessions,
        idempotency=idem_store,
        rate_limiter=RateLimiter(
            rate_store,
            clock=clock,
            mode=gw_settings.rate_limit_mode,
            fail_open=gw_settings.rate_limit_fail_open,
            public_ip_limit_per_minute=gw_settings.public_ip_limit_per_minute,
        ),
        payment_intents=payment_adapter,  # type: ignore[arg-type]
        refunds=refund_adapter,  # type: ignore[arg-type]
        merchants=merchant_adapter,  # type: ignore[arg-type]
        customers=customer_svc,  # type: ignore[arg-type]
        webhook_registry=webhook_registry,
        webhook_sink=webhook_sink,  # type: ignore[arg-type]
        webhook_pipeline=webhook_pipeline,
        metrics=metrics,
        # Deploy-rehearsal fix (2026-06-13): this was a constant ``lambda:
        # True`` — during the DR drill /readyz reported "ready, ledger ok"
        # with the postgres container STOPPED, so a load balancer would keep
        # routing traffic to a node that cannot reach the money ledger.
        # Probe the live ledger store instead: the Postgres store's ping()
        # round-trips its own connection (raises on a dead DB or on the
        # stale connection a DB bounce leaves behind); the in-memory store
        # has no ping() and is always ready.
        readiness_checks={
            "ledger": (
                ledger_store.ping
                if hasattr(ledger_store, "ping")
                else (lambda: True)
            )
        },
        webhook_endpoints=webhook_endpoints_svc,
        webhook_deliveries=webhook_deliveries_svc,
        dossier_exports=dossier_exports,
        payment_links=payment_links,
        public_surfaces=public_surfaces,
        sandbox_signups=sandbox_signups,
        sandbox_demo_seed_enabled=(
            settings.sandbox_public
            and settings.database_url == "memory://"
            and settings.connector_mode == "simulator"
            and not _is_production_env(os.environ)
        ),
        qr=qr_service,
        ledger=ledger_svc,
        tcsa_store=tcsa_store,
        settlement_store=settlement_store,
        connector_registry=connector_registry,
        aml_alert_store=compliance_alert_store,
        str_store=str_store,
        ctr_store=ctr_store,
        sanctions_store=sanctions_store,
        goaml_connector=goaml_connector,
    )

    container = ServiceContainer(
        settings=settings,
        clock=clock,
        ledger=ledger_svc,
        outbox_store=outbox_store,
        approval=approval_gateway,
        payment_store=payment_store,
        kyb_store=kyb_store,
        settlement_store=settlement_store,
        settlement_engine=settlement_engine,
        orchestrator=orchestrator,
        merchant_onboarding=merchant_svc,
        gateway_deps=gw_deps,
        event_bus=event_bus,
        outbox_worker=outbox_worker,
        notifications=notifications,
        webhook_endpoints=webhook_endpoints_svc,
        webhook_deliveries=webhook_deliveries_svc,
        sanctions_screener=sanctions_screener,
        sanctions_feed_connector=sanctions_feed_connector,
        subject_control=subject_control,
        goaml_connector=goaml_connector,
        beftn_connector=beftn_connector,
        dossier_exports=dossier_exports,
        fact_corrections=fact_corrections,
        certification_runs=certification_runs,
        connector_registry=connector_registry,
        payment_links=payment_links,
        public_surfaces=public_surfaces,
        qr=qr_service,
        offers=offers,
        subscriptions=subscriptions,
        disbursements=disbursements,
        participants=participants,
        # AML-H3: expose compliance stores so boot guard can assert Postgres types.
        ctr_store=ctr_store,
        str_store=str_store,
        sanctions_store=sanctions_store,
        monitor_event_store=monitor_event_store,
        sandbox_signups=sandbox_signups,
        metrics=metrics,
        compliance_scheduler_tasks=compliance_scheduler_tasks,
        compliance_scheduler=compliance_scheduler,
        kernel_scheduler_tasks=kernel_scheduler_tasks,
        kernel_scheduler=kernel_scheduler,
        connector_inbound=connector_inbound,
        connector_inbound_scheduler_tasks=connector_inbound_scheduler_tasks,
        connector_inbound_scheduler=connector_inbound_scheduler,
        beftn_return_consumer=beftn_return_consumer,
        beftn_return_scheduler_tasks=beftn_return_scheduler_tasks,
        beftn_return_scheduler=beftn_return_scheduler,
        _owned_resources=tuple(owned_resources),
    )
    try:
        ProductionCompositionGuard().assert_boot_invariants(container)
    except Exception:
        container.close()
        raise
    return container


# ---------------------------------------------------------------------------
# create_app
# ---------------------------------------------------------------------------


def create_app(
    settings: Settings | None = None,
    clock: Clock | None = None,
) -> FastAPI:
    """Create and return the gateway FastAPI application.

    Suitable for ``uvicorn bdpay.app:create_app --factory``.
    ``/healthz`` and ``/metrics`` are mounted by the gateway router.

    The outbox queue-drain worker runs as an application background task
    (porting-audit fix: ``OutboxWorker.run()`` was never scheduled, so
    events accumulated in the store and never reached any consumer). It
    starts on application startup and is cancelled on shutdown; the
    in-process event bus carries drained envelopes to the spec/16 LR-5
    consumers (merchant webhooks + notifications).

    COMP-02: the compliance scheduler (sanctions rescreen, CTR daily aggregate,
    CTR filing, STR retry, goAML submit sweep) runs as a second background task
    started on startup and cancelled on shutdown — without this its poll loop
    never launches and the registered tasks never fire. Verified connector
    callback rows are drained by a separate PG-only scheduler into the owning
    payment/refund state machines. The kernel scheduler (spec/02 TTL sweep +
    reversal dispatch) runs as a third background task. ``Scheduler.run_due``
    has single-tick exception isolation, so a failing sweep never crashes the
    app.

    P0 BEFTN returns: a dedicated scheduler polls the SFTP ``returns/``
    directory, ingests return/confirmation CSVs, and drives matched
    disbursement items to ``RETURNED``.  It is cancelled on shutdown.
    """
    if settings is None:
        settings = Settings.from_env(os.environ)
    if clock is None:
        clock = SystemClock()
    container = build_services(settings, clock)
    app = _create_gateway_app(container.gateway_deps)
    add_offer_routes(
        app,
        OfferRouteDependencies(service=container.offers, clock=container.clock),
    )
    add_subscription_routes(
        app,
        SubscriptionRouteDependencies(
            service=container.subscriptions, clock=container.clock
        ),
    )
    app.state.gateway_clock = container.clock
    app.state.disbursements = container.disbursements
    install_disbursement_routes(app)
    app.include_router(
        build_participants_router(
            ParticipantRoutesDependencies(
                settings=container.settings,
                service=container.participants,
            )
        )
    )
    app.state.container = container

    async def _start_background_workers() -> None:
        import asyncio

        app.state.outbox_worker_task = asyncio.create_task(
            container.outbox_worker.run(), name="outbox-worker"
        )
        if container.compliance_scheduler is not None:
            app.state.compliance_scheduler_task = asyncio.create_task(
                container.compliance_scheduler.run(), name="compliance-scheduler"
            )
        if container.kernel_scheduler is not None:
            app.state.kernel_scheduler_task = asyncio.create_task(
                container.kernel_scheduler.run(), name="kernel-scheduler"
            )
        if container.connector_inbound_scheduler is not None:
            app.state.connector_inbound_scheduler_task = asyncio.create_task(
                container.connector_inbound_scheduler.run(),
                name="connector-inbound-scheduler",
            )
        if container.beftn_return_scheduler is not None:
            app.state.beftn_return_scheduler_task = asyncio.create_task(
                container.beftn_return_scheduler.run(), name="beftn-return-scheduler"
            )

    async def _stop_background_workers() -> None:
        import asyncio
        import contextlib

        for attr in (
            "outbox_worker_task",
            "compliance_scheduler_task",
            "kernel_scheduler_task",
            "connector_inbound_scheduler_task",
            "beftn_return_scheduler_task",
        ):
            task = getattr(app.state, attr, None)
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        container.close()

    app.on_event("startup")(_start_background_workers)
    app.on_event("shutdown")(_stop_background_workers)
    return app

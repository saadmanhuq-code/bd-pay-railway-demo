"""Cross-package contracts — the ONLY way bdpay packages talk to each other.

Binding boundary rule (arch ARCHITECTURE-DECISION §(b)): packages communicate
only through the typed in-process interfaces declared here; no package reaches
into another's tables; the ``ledger`` package is the SOLE writer of postings.
When a package is later extracted to a service, the in-process interface
becomes the network interface unchanged.

Conventions (binding, apply to every port in this module):

- **Money** — every amount is integer paisa as ``amount_minor: int`` (spec/00
  §6); the :class:`bdpay.platform.money.Money` value object appears only where
  arithmetic crosses the boundary (AML pre-flight, connector reversal). Floats
  never appear.
- **Time** — in-process timestamps are timezone-aware UTC ``datetime`` values
  produced by an injected :class:`~bdpay.platform.clock.Clock`; wire/JSON
  timestamps are RFC3339 ``Z`` strings (millisecond precision, errata E12).
  A port method that needs the current time takes a ``clock`` parameter —
  never a wall-clock read.
- **Transactions** — a ``conn`` parameter is a psycopg3 connection (typed
  ``Any`` so this module stays import-light) and the call MUST run inside the
  caller's transaction. This is how the atomic ledger+outbox rule composes:
  journal entry, chain row, state update, audit row, and outbox row commit or
  roll back together (spec/00 §8).
- **Refusal-first** — ports raise the :mod:`bdpay.platform.errors` hierarchy;
  a port never silently substitutes a default for a denied operation.

Method shapes are derived from the binding extracts in spec/03 (LedgerService
interface), spec/01 §J (notification dispatch), spec/02 §Pre-flight pipeline
(sanctions / AML / limits), spec/06 (event intake), spec/10 (ConnectorRunner
dispatch), and spec/15 (ApprovalRequest FSM).

Repository protocols that are platform-owned live next to their stores:
:class:`bdpay.platform.outbox.OutboxStore`,
:class:`bdpay.platform.approval.ApprovalStore`, and
:class:`bdpay.platform.notifications.NotificationStore`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:  # annotation-only imports; no runtime package coupling
    from bdpay.connectors.sdk import ConnectorResult, Money, PaymentInstruction
    from bdpay.platform.approval import ApprovalRecord
    from bdpay.platform.clock import Clock
    from bdpay.platform.outbox import OutboxEvent

__all__ = [
    "AmlPort",
    "AmlPreflightResult",
    "ApprovalPort",
    "AuditEventSpec",
    "AuditPort",
    "ChainVerificationResult",
    "ConnectorRunnerPort",
    "IdempotencyPort",
    "IdempotencyReservation",
    "JournalEntrySpec",
    "LedgerPort",
    "NotificationDeliveryResult",
    "NotificationPort",
    "OutboxPort",
    "PostingSpec",
    "SanctionsFreshnessPort",
    "SanctionsFreshnessVerdict",
    "SanctionsPort",
    "SanctionsScreenResult",
]

_SIDES = ("DEBIT", "CREDIT")


# ---------------------------------------------------------------------------
# Ledger specs (binding shapes from spec/03 §Internal interface extract)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PostingSpec:
    """One leg of a journal entry. Validated at construction (fails closed)."""

    account_id: str  # acct_<...>; must exist in accounts
    side: str  # "DEBIT" | "CREDIT"
    amount_minor: int  # paisa; > 0; NEVER float
    currency: str = "BDT"

    def __post_init__(self) -> None:
        if self.side not in _SIDES:
            raise ValueError(f"side must be one of {_SIDES}, got {self.side!r}")
        if isinstance(self.amount_minor, bool) or not isinstance(self.amount_minor, int):
            raise ValueError(
                f"amount_minor must be int paisa, got {type(self.amount_minor).__name__}"
            )
        if self.amount_minor <= 0:
            raise ValueError(f"amount_minor must be > 0, got {self.amount_minor}")
        if self.currency != "BDT":
            raise ValueError(f"unsupported currency {self.currency!r}; v1 is BDT only")
        if not self.account_id:
            raise ValueError("account_id must be a non-empty string")


@dataclass(frozen=True)
class JournalEntrySpec:
    """A balanced journal entry to append (spec/03 binding shape).

    ``postings`` MUST sum to zero (debits == credits) with at least two legs;
    the ledger implementation re-verifies and fails closed on imbalance.
    """

    reference_id: str  # pi_<...> | sbatch_<...> | rfnd_<...> ...
    reference_type: str  # PAYMENT | SETTLEMENT | REFUND | FEE | REVERSAL | ADJUSTMENT
    entry_type: str  # canonical ledger entry type (arch §(c))
    description: str  # human-readable; PII-free
    produced_by: str  # "<service>@<version>"
    idempotency_key: str  # make_id("je", {...}) content-addressed dedupe key
    postings: Sequence[PostingSpec]

    def __post_init__(self) -> None:
        if len(self.postings) < 2:
            raise ValueError("a journal entry needs at least two postings")
        debit = sum(p.amount_minor for p in self.postings if p.side == "DEBIT")
        credit = sum(p.amount_minor for p in self.postings if p.side == "CREDIT")
        if debit != credit:
            raise ValueError(
                f"postings do not balance: DEBIT {debit} != CREDIT {credit} (paisa)"
            )


@dataclass(frozen=True)
class ChainVerificationResult:
    """Outcome of a ledger hash-chain walk (spec/03 verify_chain)."""

    ok: bool
    status: str  # COMPLETED | FAILED_BROKEN_CHAIN | FAILED_INVALID_CHECKPOINT_SIG | ...
    entries_checked: int
    first_broken_index: int | None = None
    detail: str | None = None


@runtime_checkable
class LedgerPort(Protocol):
    """The ledger package's in-process surface — sole writer of postings.

    Implemented by ``bdpay.ledger`` (spec/03). Every mutating method appends
    journal + postings + chain + audit rows inside the CALLER's transaction.
    """

    def post_journal_entry(
        self, spec: JournalEntrySpec, *, clock: Clock, conn: Any
    ) -> str: ...

    def open_hold(
        self,
        account_id: str,
        amount_minor: int,
        hold_reason: str,
        reference_id: str,
        *,
        source_account_id: str | None = None,
        hold_reserve_account_id: str | None = None,
        hold_expires_at: datetime | None = None,
        clock: Clock,
        conn: Any,
    ) -> str: ...

    def release_hold(
        self,
        hold_id: str,
        outcome: str,
        *,
        amount_minor: int | None = None,
        clock: Clock,
        conn: Any,
    ) -> None: ...

    def get_open_hold_id(self, reference_id: str, *, conn: Any) -> str | None: ...

    def get_balance(
        self,
        account_id: str,
        *,
        as_of: datetime | None = None,
        conn: Any,
        lock: bool = False,
    ) -> int: ...

    def trial_balance_assertion(self, *, conn: Any) -> None: ...

    def verify_chain(
        self, *, from_index: int = 0, to_index: int | None = None, conn: Any
    ) -> ChainVerificationResult: ...


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AuditEventSpec:
    """One audit_events row to append (spec/03 audit journal).

    ``payload`` MUST already be PII-redacted; the audit implementation stores
    only ``sha256(canonical_json(payload))`` + a pointer in the chain.
    """

    event_type: str  # e.g. PAYMENT_STATE_TRANSITION | APPROVAL_GRANTED | ...
    actor_id: str  # operator / service principal; PII-redacted
    subject_type: str
    subject_id: str
    from_state: str | None = None
    to_state: str | None = None
    payload: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("event_type", "actor_id", "subject_type", "subject_id"):
            if not getattr(self, name):
                raise ValueError(f"{name} must be a non-empty string")


@runtime_checkable
class AuditPort(Protocol):
    """Append-only audit journal writer (hash-chained; spec/03).

    Returns the created ``aud_<...>`` event id. When ``conn`` is provided the
    write joins the caller's transaction (every FSM transition writes exactly
    one audit row in the same transaction, spec/00 §8).
    """

    def append(
        self, spec: AuditEventSpec, *, clock: Clock, conn: Any | None = None
    ) -> str: ...


# ---------------------------------------------------------------------------
# Outbox
# ---------------------------------------------------------------------------


@runtime_checkable
class OutboxPort(Protocol):
    """Enqueue an event within the caller's transaction context.

    Domain packages depend on this narrow port; draining/publishing is the
    outbox-worker's job (:class:`bdpay.platform.outbox.OutboxWorker`).
    Idempotent on the content-addressed ``event_id``.
    """

    def enqueue(self, event: OutboxEvent, *, conn: Any | None = None) -> str: ...


# ---------------------------------------------------------------------------
# Idempotency (spec/01 data model + spec/02 claim algorithm)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IdempotencyReservation:
    """Result of reserving a client Idempotency-Key.

    ``outcome`` is one of:

    - ``NEW`` — first sighting; caller proceeds and must ``complete``.
    - ``REPLAY`` — COMPLETED record with the same request hash; caller returns
      the stored response verbatim.
    - ``IN_FLIGHT`` — another worker holds the claim; caller returns 409.
    - ``CONFLICT`` — same key, different request body hash; caller returns 422
      (spec/00 §4 replay-with-mismatched-params rule).
    """

    outcome: str  # NEW | REPLAY | IN_FLIGHT | CONFLICT
    idem_id: str
    response_status: int | None = None
    response_body: Mapping[str, object] | None = None


@runtime_checkable
class IdempotencyPort(Protocol):
    """Durable idempotency record store (idempotency_keys, migration 0002).

    Keys are scoped per (idempotency_key, scope_id, request_path) — never a bare
    unique on the key alone. ``scope_id`` is the authenticated principal's
    stable identity (api key id for merchant keys, customer_id for customer
    JWTs, operator_id for operator sessions, None for unauthenticated/public
    callers). ``key_id`` is retained only for the deferred FK to api_keys.
    ``claim`` is the kernel's conditional ``locked_at`` update (spec/02): True
    means this worker owns processing.
    """

    def reserve(
        self,
        *,
        idempotency_key: str,
        key_id: str | None,
        scope_id: str | None,
        customer_id: str | None,
        http_method: str,
        request_path: str,
        request_body_hash: str,
        now: datetime,
    ) -> IdempotencyReservation: ...

    def claim(
        self,
        *,
        idempotency_key: str,
        key_id: str | None,
        scope_id: str | None,
        request_path: str,
        now: datetime,
    ) -> bool: ...

    def complete(
        self,
        *,
        idempotency_key: str,
        key_id: str | None,
        scope_id: str | None,
        request_path: str,
        response_status: int,
        response_body: Mapping[str, object],
        now: datetime,
    ) -> None: ...


# ---------------------------------------------------------------------------
# Approval (two-eyes; spec/15 FSM, platform-owned engine)
# ---------------------------------------------------------------------------


@runtime_checkable
class ApprovalPort(Protocol):
    """Two-eyes request/decide gateway (spec/15 ApprovalRequest FSM).

    The requester can NEVER approve their own request — enforced in the
    implementation AND by the DB CHECK in migration 0003. ``requires_two_eyes``
    fails closed: unknown action types refuse, missing amounts on
    threshold-gated actions require approval.
    """

    def requires_two_eyes(
        self, action_type: str, *, amount_minor: int | None = None
    ) -> bool: ...

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
    ) -> ApprovalRecord: ...

    def decide(
        self,
        approval_request_id: str,
        *,
        approver_id: str,
        approve: bool,
        decision_reason: str | None = None,
    ) -> ApprovalRecord: ...

    def get(self, approval_request_id: str) -> ApprovalRecord | None: ...


# ---------------------------------------------------------------------------
# Sanctions / AML (spec/02 pre-flight pipeline; specs 06/07 internals)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SanctionsScreenResult:
    """Outcome of a sanctions screen. ``hit=True`` means refuse and freeze."""

    hit: bool
    hit_id: str | None = None  # sanc_<...> when hit
    match_score: int | None = None  # 0..100 integer score (no floats)
    list_version_id: str | None = None
    detail: str | None = None


@runtime_checkable
class SanctionsPort(Protocol):
    """Sanctions screening — ALWAYS synchronous at payment initiation
    (ATA 2009 freeze obligation; arch cut-last #5; spec/02 pre-flight b).
    Cannot be disabled; an unavailable screener is a refusal, not a pass.
    """

    def check_sync(
        self, *, customer_id: str | None, merchant_id: str | None
    ) -> SanctionsScreenResult: ...

    def screen_entity(
        self,
        *,
        entity_name: str,
        entity_type: str,
        identifiers: Mapping[str, str],
        context: str = "ONBOARDING",
    ) -> SanctionsScreenResult: ...


@dataclass(frozen=True)
class SanctionsFreshnessVerdict:
    """Outcome of a sanctions-list freshness check (BDPAY-08).

    ``is_stale=True`` means the newest ACTIVE list version is older than the
    agreed staleness threshold (24h — the same line the spec/12 §G side-channel
    alarm draws, so the fail-closed gate and the alarm never disagree on what
    "stale" means). ``age_seconds`` is ``None`` when no active version exists
    at all (treated as stale: an unloaded book is the most obsolete book).
    """

    is_stale: bool
    threshold_seconds: int  # 24h == 86_400 in the reference deployment
    age_seconds: int | None = None
    detail: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.is_stale, bool):
            raise ValueError(f"is_stale must be a bool, got {type(self.is_stale).__name__}")
        if (
            isinstance(self.threshold_seconds, bool)
            or not isinstance(self.threshold_seconds, int)
            or self.threshold_seconds <= 0
        ):
            raise ValueError(
                f"threshold_seconds must be a positive int, got {self.threshold_seconds!r}"
            )
        if self.age_seconds is not None and (
            isinstance(self.age_seconds, bool)
            or not isinstance(self.age_seconds, int)
            or self.age_seconds < 0
        ):
            raise ValueError(
                f"age_seconds must be a non-negative int or None, got {self.age_seconds!r}"
            )


@runtime_checkable
class SanctionsFreshnessPort(Protocol):
    """Sanctions watchlist freshness — opt-in fail-closed surface (BDPAY-08).

    The default deployment posture is ALARM-ONLY: a stale list raises the
    spec/12 §G side-channel STALE alarm but screening continues against the
    previously ingested list. A caller that opts into fail-closed freshness
    (e.g. ``DisbursementService(stale_sanctions_fail_closed=True)``) consults
    this port and refuses to move money when ``is_stale`` — a stale watchlist
    cannot honestly clear anyone.

    ``now`` is the caller's injected-clock instant (convention: a port method
    that needs the current time takes it as a parameter, never a wall-clock
    read). The threshold lives on the verdict so callers and observers share
    one definition of "stale".
    """

    def is_stale(self, *, now: datetime) -> SanctionsFreshnessVerdict: ...


@dataclass(frozen=True)
class AmlPreflightResult:
    """AML pre-flight verdict (spec/02 pre-flight c).

    ``allow=False`` means hold: the intent transitions to FAILED/DECLINED and
    an ``aml_alerts`` row exists (``alert_id``). ``defer_async=True`` means the
    payment proceeds and post-event transaction monitoring covers it.
    """

    allow: bool
    defer_async: bool = False
    alert_id: str | None = None  # aml_<...> when held
    reason: str | None = None


@runtime_checkable
class AmlPort(Protocol):
    """AML/CFT surface the kernel and the outbox consumers depend on.

    ``preflight`` is the synchronous pre-dispatch check (high-risk subjects
    hold synchronously; low-risk defer to the stream monitor). ``intake_event``
    consumes spec/00 §5 envelopes from the bus and MUST be idempotent on
    ``event_id``.
    """

    def preflight(
        self,
        *,
        intent_id: str,
        customer_id: str | None,
        merchant_id: str,
        amount: Money,
        method: str,
        clock: Clock,
    ) -> AmlPreflightResult: ...

    def intake_event(self, envelope: Mapping[str, object]) -> None: ...


# ---------------------------------------------------------------------------
# Connector runner (spec/10 — the only dispatch path to any rail)
# ---------------------------------------------------------------------------


@runtime_checkable
class ConnectorRunnerPort(Protocol):
    """Submit / query / reverse through the connector registry by id.

    The runner resolves the connector from ``instruction.rail`` (a
    ``connector_id``), enforces mode + timeout + circuit breaker, persists
    every ConnectorResult, and NEVER touches business tables. The kernel
    receives the result and runs its own single business transaction.
    """

    async def submit(self, instruction: PaymentInstruction) -> ConnectorResult: ...

    async def query_status(
        self,
        connector_id: str,
        connector_ref: str,
        rail_transaction_id: str | None,
    ) -> ConnectorResult: ...

    async def reverse(
        self,
        connector_id: str,
        connector_ref: str,
        rail_transaction_id: str | None,
        reverse_amount: Money,
        reason: str,
    ) -> ConnectorResult: ...


# ---------------------------------------------------------------------------
# Notifications (spec/01 §J dispatch surface; delivery via spec/12 connectors)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NotificationDeliveryResult:
    """Outcome of one delivery attempt through a notification connector."""

    delivered: bool
    provider_message_id: str | None = None
    error_code: str | None = None


@runtime_checkable
class NotificationPort(Protocol):
    """Delivery transport abstraction (SMS/email/push connector, spec/12).

    Implementations MUST be idempotent on ``connector_ref`` per the SDK
    invariant; ``recipient_ref`` is an opaque entity id (cust_/mrch_/oper_),
    never a raw phone number or email at this boundary.
    """

    async def send(
        self,
        *,
        channel: str,
        recipient_ref: str,
        template_id: str,
        body: str,
        connector_ref: str,
    ) -> NotificationDeliveryResult: ...

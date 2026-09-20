"""PaymentOrchestrator — drives intent -> attempt -> connector dispatch (spec/02).

Every money-moving transition posts journal entries through
:class:`~bdpay.platform.interfaces.LedgerPort` and an audit row through
:class:`~bdpay.platform.interfaces.AuditPort`; every transition emits the
spec/00 §5 events via :class:`~bdpay.platform.interfaces.OutboxPort`. The
pre-flight pipeline refuses BEFORE any ledger write. Refunds are NEW
offsetting flows — the original ``payment_captured`` entry is never mutated.

Capture posting shape (errata K-6): the spec/02 pseudocode posts one
journal entry whose four legs do not balance (debits = amount + fee,
credits = amount); the same spec's "Ledger entry types" table and its
binding invariant ("every ``payment_captured`` entry is immediately followed,
same transaction, by a ``fee_collected`` entry") describe two balanced
entries. The balanced two-entry form is implemented and pinned by test.

The connector SDK exposes ``submit`` / ``query_status`` / ``reverse`` only —
no capture primitive (errata K-7): manual capture is the platform-side
completion of an authorized hold (attempt ``capture_submitted`` ->
``capture_confirmed``), and voids release without a connector call. The
zero-value void journal marker in the spec table is recorded as the audit
event ``VOID_MARKER`` because postings of zero are illegal (errata K-8).
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from bdpay.connectors.sdk import ConnectorStatus, PaymentInstruction
from bdpay.connectors.sdk import Money as SdkMoney
from bdpay.kernel.ids_ext import make_kernel_id
from bdpay.kernel.models import PaymentAttemptRecord, PaymentIntentRecord, RefundRecord
from bdpay.kernel.payment_states import ATTEMPT_TABLE, INTENT_TABLE, REFUND_TABLE
from bdpay.kernel.preflight import MerchantFeeProfilePort, PreflightPipeline
from bdpay.kernel.repository import PaymentStore, intent_ttl_default
from bdpay.kernel.routing import STORE_AND_FORWARD, RoutingEngine
from bdpay.ledger.account_ids import (
    MDR_INCOME_PURPOSE_TAG,
    merchant_hold_reserve_id,
    merchant_settlement_id,
    system_account_id,
)
from bdpay.ledger.settlement.fees import (
    INSTRUMENT_CLASSES,
    FeeRule,
    compute_fee,
    default_fee_rules,
    resolve_fee_rule,
)
from bdpay.platform.canonical import sha256_canonical
from bdpay.platform.clock import Clock
from bdpay.platform.errors import (
    AmlBlockError,
    ConflictError,
    IdempotencyConflictError,
    InvalidRequestError,
    LimitExceededError,
    NotFoundError,
    SanctionsBlockError,
)
from bdpay.platform.ids import make_id
from bdpay.platform.interfaces import (
    AuditEventSpec,
    AuditPort,
    ConnectorRunnerPort,
    JournalEntrySpec,
    LedgerPort,
    OutboxPort,
    PostingSpec,
)
from bdpay.platform.money import Money
from bdpay.platform.outbox import build_event
from bdpay.platform.pii import is_opaque_identifier_key, redact
from bdpay.platform.scheduler import BangladeshBankCalendar

__all__ = [
    "ConfirmOutcome",
    "MAX_ATTEMPTS_PER_INTENT",
    "MAX_STATUS_POLLS",
    "PaymentIntentRequest",
    "PaymentOrchestrator",
    "derive_instrument_class",
    "fee_method_for",
    "kernel_accounts",
    "rail_ref_proof",
    "transit_account_for",
]

PRODUCER = "payment-orchestrator@1"
MAX_ATTEMPTS_PER_INTENT = 3  # spec/02 retry policy
MAX_STATUS_POLLS = 3  # spec/02 TIMED_OUT poll sequence
MAX_REFUND_RETRIES = 3
REVERSAL_PENDING_RETRY_AFTER = timedelta(minutes=5)
AUTH_HOLD_DEFAULT = timedelta(days=7)  # spec/02 REQUIRES_CAPTURE default TTL


def _rfc3339(at: datetime) -> str:
    """RFC3339 UTC, millisecond precision, ``Z`` suffix (E12 wire form)."""
    return at.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


#: Prefix on the redaction-safe rail-reference proof carried by the MONEY-04
#: discrepancy event.
RAIL_REF_PROOF_PREFIX = "rrh_"


def rail_ref_proof(rail_transaction_id: str, payment_intent_id: str) -> str:
    """MONEY-04: a REDACTION-SAFE proof binding a rail reference to its intent.

    The raw ``rail_transaction_id`` cannot be carried in the discrepancy event:
    a real NPSB RRN is a 12-digit numeric run that ``redact()`` matches as an
    account number and replaces with ``[REDACTED-account]`` (PII redaction runs
    on every string payload value in ``_emit``). That would erase the very proof
    money moved on the rail — the thing the discrepancy exists to preserve.

    So we carry a content-addressed digest instead, in a form that survives
    ``redact()`` *by construction* (not by luck): the 64-char sha256 hex is
    chunked into 8-char groups joined by ``-`` so the longest unbroken digit run
    is 8 (< the 11-digit account-pattern threshold). A bare hex digest can itself
    contain an 11+ digit run and be partially redacted; chunking removes that
    failure mode for every possible RRN.

    The digest binds the rail ref to ``payment_intent_id`` so reconciliation can
    confirm a given RRN matches a given intent. NOTE (money semantics): this is
    correlation / tamper-evidence, NOT secrecy — the 10^12 RRN space is small
    enough to brute-force against this unkeyed digest, and ``payment_intent_id``
    travels in the same payload. A keyed HMAC is the follow-up that would make
    the proof confidential — but it needs a PLATFORM-level secret (the only
    in-reach key, the gateway ``hmac_master_key``, is constructed below/after
    the kernel and would invert the kernel->gateway dependency), so it is an
    operator-signed-off change, not part of this smallest fix. This form keeps
    the proof durable today; it restores redaction-safety, not RRN secrecy.
    """
    digest = sha256_canonical(
        {
            "rail_transaction_id": rail_transaction_id,
            "payment_intent_id": payment_intent_id,
        }
    )
    chunked = "-".join(digest[i : i + 8] for i in range(0, len(digest), 8))
    return f"{RAIL_REF_PROOF_PREFIX}{chunked}"


#: Kernel ``payment_method_type`` (spec/02) -> the (account_subtype, purpose_tag)
#: of the chart account the capture's *gross* leg debits — the rail-specific
#: transit/clearing account (MONEY rail-routing). The generic kernel ``in_transit``
#: leg previously resolved every rail to ``IN_TRANSIT/npsb_transit``, silently
#: misclassifying BEFTN/RTGS/MFS/card captures into the NPSB transit account.
#: Each entry is chart-faithful against spec/03 §"Chart of accounts" (the only
#: enumerated transit/clearing rows): the three ``IN_TRANSIT`` rails
#: (npsb/beftn/rtgs_transit) and the per-MFS / card ``CONNECTOR_CLEARING`` floats
#: (bkash/nagad/rocket/card_clearing — "Per-MFS connector interim float").
#: BANGLA_QR routes via NPSB (routing.py ``METHOD_CONNECTORS``; spec/13 §Issuer
#: flows), so it shares ``npsb_transit``. NAGAD/ROCKET clearing rows mirror the
#: spec's ``bkash_clearing`` pattern and are added to ``chart.SYSTEM_ACCOUNTS``.
_TRANSIT_ACCOUNT_BY_METHOD: dict[str, tuple[str, str]] = {
    "NPSB_IBFT": ("IN_TRANSIT", "npsb_transit"),
    "BEFTN_CREDIT": ("IN_TRANSIT", "beftn_transit"),
    "RTGS": ("IN_TRANSIT", "rtgs_transit"),  # not a v1 kernel method; completeness
    "BANGLA_QR": ("IN_TRANSIT", "npsb_transit"),  # QR settles over NPSB
    "BKASH": ("CONNECTOR_CLEARING", "bkash_clearing"),
    "NAGAD": ("CONNECTOR_CLEARING", "nagad_clearing"),
    "ROCKET": ("CONNECTOR_CLEARING", "rocket_clearing"),
    "CARD": ("CONNECTOR_CLEARING", "card_clearing"),
}

#: The transit account the ``in_transit`` leg falls back to when no rail is
#: supplied (``method=None``) — NPSB IBFT, the v1 domestic-acquiring primary
#: rail. Real money paths always pass ``intent.method``; this default only
#: serves rail-agnostic callers (e.g. ``offers.ledger_legs.offer_accounts``,
#: which never touches the transit leg).
_DEFAULT_TRANSIT: tuple[str, str] = _TRANSIT_ACCOUNT_BY_METHOD["NPSB_IBFT"]


def transit_account_for(method: str | None) -> str:
    """Resolve the chart transit/clearing account id for a payment ``method``.

    Fails closed (``InvalidRequestError``) on an unknown rail — the whole point
    of per-rail routing is that no capture silently misclassifies into the NPSB
    default. ``method=None`` keeps the NPSB fallback for rail-agnostic callers.
    Id resolution itself stays in ``account_ids.system_account_id`` (the single
    id resolver); this function owns only the kernel rail semantics.
    """
    if method is None:
        subtype, purpose = _DEFAULT_TRANSIT
    else:
        mapped = _TRANSIT_ACCOUNT_BY_METHOD.get(method)
        if mapped is None:
            raise InvalidRequestError(
                f"no transit-account mapping for payment method {method!r}",
                code="method_unsupported",
            )
        subtype, purpose = mapped
    return system_account_id(subtype, purpose)


def kernel_accounts(
    merchant_id: str | None = None, *, method: str | None = None
) -> dict[str, str]:
    """Deterministic account ids the kernel posts against (spec/02 entry types).

    The ledger package owns the ``accounts`` rows. These ids are resolved
    through ``bdpay.ledger.account_ids`` — the single source of truth shared
    with ``LedgerService.create_account`` and the chart bootstrap — so the
    three accounts every capture/refund touches are byte-identical to the rows
    ``bootstrap_chart_of_accounts`` / ``open_merchant_accounts`` create
    (MONEY-01). The ``in_transit`` leg resolves PER RAIL via ``method``
    (``_TRANSIT_ACCOUNT_BY_METHOD``): an NPSB capture lands in ``npsb_transit``,
    a BEFTN capture in ``beftn_transit``, an MFS/card capture in the matching
    ``CONNECTOR_CLEARING`` float — so no rail silently misclassifies. With
    ``method=None`` the leg keeps the NPSB default for rail-agnostic callers.
    """
    out = {
        "in_transit": transit_account_for(method),
        "mdr_income": system_account_id("MDR_INCOME", MDR_INCOME_PURPOSE_TAG),
    }
    if merchant_id is not None:
        out["merchant_settlement"] = merchant_settlement_id(merchant_id)
        out["merchant_hold_reserve"] = merchant_hold_reserve_id(merchant_id)
    return out


#: Kernel ``payment_method_type`` (spec/02) -> fee-rule ``method`` (spec/04).
#: ``CARD`` maps to the domestic-acquiring rule: domestic card acquiring is the
#: v1 scope; the international/Amex rows activate with the acquiring data that
#: distinguishes them on the attempt.
_FEE_METHOD_BY_KERNEL_METHOD: dict[str, str] = {
    "NPSB_IBFT": "NPSB_IBFT",
    "BEFTN_CREDIT": "BEFTN_CREDIT",
    "BKASH": "BKASH",
    "NAGAD": "NAGAD",
    "ROCKET": "ROCKET",
    "CARD": "CARD_DOMESTIC",
    "BANGLA_QR": "BANGLA_QR",
}

#: Wallet rails whose paying instrument is by definition an MFS/PSP wallet.
_WALLET_METHODS = frozenset({"BKASH", "NAGAD", "ROCKET"})

#: Card funding type -> spec/16 LR-3 instrument_class.
_CARD_FUNDING_CLASS: dict[str, str] = {
    "debit": "DEBIT_PREPAID",
    "prepaid": "DEBIT_PREPAID",
    "credit": "CREDIT",
}


def fee_method_for(method: str) -> str:
    """Map the kernel payment method to the spec/04 fee-rule method."""
    fee_method = _FEE_METHOD_BY_KERNEL_METHOD.get(method)
    if fee_method is None:
        raise InvalidRequestError(
            f"no fee-rule method mapping for payment method {method!r}",
            code="method_unsupported",
        )
    return fee_method


def derive_instrument_class(method: str, metadata: dict[str, str] | None) -> str | None:
    """Derive the spec/16 LR-3 instrument_class for a payment (or None = unknown).

    Derivation (spec/16 §LR-3): card funding type debit/prepaid ->
    ``DEBIT_PREPAID``, credit -> ``CREDIT``; bKash/Nagad/Rocket/Upay/PSP-wallet
    originated payments -> ``MFS_PSP_WALLET``.  The instrument is read from the
    persisted intent metadata (keys ``instrument_class``, ``wallet_provider``,
    ``card_funding``) so the derivation is replayable at any settle time (poll
    completion, manual capture).  An unknown or unrecognised instrument returns
    ``None``: only wildcard fee rules match, so the regulated ceiling applies —
    an unidentified instrument never receives a discounted tier.
    """
    details = dict(metadata or {})
    explicit = details.get("instrument_class")
    if explicit in INSTRUMENT_CLASSES:
        return explicit
    if method in _WALLET_METHODS:
        return "MFS_PSP_WALLET"
    if details.get("wallet_provider"):
        return "MFS_PSP_WALLET"
    funding = details.get("card_funding", "").lower()
    return _CARD_FUNDING_CLASS.get(funding)


@dataclass(frozen=True)
class PaymentIntentRequest:
    """POST /v1/payment-intents body, post-gateway (spec/02 §API surface)."""

    merchant_id: str
    amount_minor: int
    method: str
    idempotency_key: str
    customer_id: str | None = None
    currency: str = "BDT"
    capture_method: str = "automatic"
    description: str | None = None
    statement_descriptor: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)
    credits_customer_wallet: bool = False


@dataclass(frozen=True)
class ConfirmOutcome:
    """Result of confirm(): either dispatched/deferred or action-gated."""

    payment_intent_id: str
    status: str
    next_action_type: str | None = None
    next_action_token: str | None = None


class PaymentOrchestrator:
    """The spec/02 payment-orchestrator sub-service."""

    def __init__(
        self,
        *,
        store: PaymentStore,
        ledger: LedgerPort,
        audit: AuditPort,
        outbox: OutboxPort,
        runner: ConnectorRunnerPort,
        routing: RoutingEngine,
        preflight: PreflightPipeline,
        clock: Clock,
        fee_rules: Sequence[FeeRule] | None = None,
        merchant_profiles: MerchantFeeProfilePort | None = None,
        calendar: BangladeshBankCalendar | None = None,
        psp_license_active: bool = False,
        transaction_factory: Callable[[], Any] | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._store = store
        self._ledger = ledger
        self._audit = audit
        self._outbox = outbox
        self._runner = runner
        self._routing = routing
        self._preflight = preflight
        self._clock = clock
        self._transaction_factory = transaction_factory
        # COMP-08 (Foster Payments analog): the PSP-side merchant-of-record /
        # payment-aggregation gate.  When false, the platform refuses to take on
        # the THIRD-PARTY-fund obligation that a charge capture creates (the
        # ``MERCHANT_SETTLEMENT`` credit in ``_settle_charge``).  Mirrors the PSO
        # ``pso_license_active`` activation gate.  Fail-closed by default so the
        # unsafe posture requires an affirmative opt-in at every construction
        # site (never the implicit default).
        if not isinstance(psp_license_active, bool):
            raise InvalidRequestError(
                "psp_license_active must be a boolean, "
                f"got {psp_license_active!r}"
            )
        self._psp_license_active = psp_license_active
        # spec/16 §G (errata E-S16-08): store-and-forward deferral records the
        # actual scheduled redispatch instant, derived from the BB calendar.
        self._calendar = calendar or BangladeshBankCalendar()
        # spec/16 LR-3: the capture-time fee comes from the tiered rule table,
        # resolved per transaction — never from a flat per-orchestrator rate.
        self._fee_rules = list(fee_rules) if fee_rules is not None else default_fee_rules()
        self._merchant_profiles = merchant_profiles
        self._log = logger or logging.getLogger("bdpay.kernel.orchestrator")

    @contextlib.contextmanager
    def _operation_transaction(self, conn: Any | None) -> Iterator[Any | None]:
        if conn is not None or self._transaction_factory is None:
            yield conn
            return
        with self._transaction_factory() as owned:
            with owned.transaction():
                yield owned

    def _lock_customer_limit_reservation(
        self, customer_id: str | None, *, conn: Any | None
    ) -> None:
        if customer_id is None or conn is None:
            return
        conn.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))",
            (customer_id,),
        )

    # ------------------------------------------------------------------
    # intent creation
    # ------------------------------------------------------------------

    def create_intent(
        self, request: PaymentIntentRequest, *, conn: Any | None = None
    ) -> PaymentIntentRecord:
        """Create a PaymentIntent (idempotent on the client key)."""
        if conn is None and self._transaction_factory is not None:
            with self._operation_transaction(conn) as tx_conn:
                return self.create_intent(request, conn=tx_conn)
        if not request.idempotency_key:
            raise InvalidRequestError(
                "Idempotency-Key is required for intent creation",
                code="idempotency_key_required",
            )
        existing = self._store.find_intent_by_idempotency_key(
            request.merchant_id, request.idempotency_key, conn=conn
        )
        if existing is not None:
            if (
                existing.amount_minor != request.amount_minor
                or existing.method != request.method
                or existing.customer_id != request.customer_id
            ):
                raise IdempotencyConflictError(
                    "Idempotency-Key replayed with mismatched parameters"
                )
            return existing  # replay: stored intent returned verbatim
        now = self._clock.now()
        intent_id = make_id(
            "pi",
            {
                "merchant_id": request.merchant_id,
                "customer_id": request.customer_id,
                "amount_minor": request.amount_minor,
                "currency": request.currency,
                "method": request.method,
                "idempotency_key": request.idempotency_key,
                "created_at": now,
            },
        )
        client_secret_hash = sha256_canonical(
            {"intent_id": intent_id, "purpose": "client_secret", "created_at": now}
        )
        record = PaymentIntentRecord(
            payment_intent_id=intent_id,
            merchant_id=request.merchant_id,
            customer_id=request.customer_id,
            amount_minor=request.amount_minor,
            currency=request.currency,
            method=request.method,
            capture_method=request.capture_method,
            status="CREATED",
            description=request.description,
            statement_descriptor=request.statement_descriptor,
            metadata=request.metadata,
            idempotency_key=request.idempotency_key,
            client_secret_hash=client_secret_hash,
            expires_at=intent_ttl_default(now),
            created_at=now,
            updated_at=now,
        )
        self._store.insert_intent(record, conn=conn)
        self._audit_transition(record, from_state=None, to_state="CREATED", conn=conn)
        self._emit(
            "payment_intent.created",
            record,
            {
                "payment_intent_id": intent_id,
                "merchant_id": record.merchant_id,
                "amount_minor": record.amount_minor,
                "currency": record.currency,
                "method": record.method,
            },
            conn=conn,
        )
        return record

    # ------------------------------------------------------------------
    # confirm: pre-flight then dispatch
    # ------------------------------------------------------------------

    async def confirm(
        self,
        payment_intent_id: str,
        *,
        payment_method_details: dict[str, str] | None = None,
        credits_customer_wallet: bool = False,
        conn: Any | None = None,
    ) -> ConfirmOutcome:
        """spec/02 confirm: PRE_FLIGHT pipeline, action gate, dispatch."""
        snapshot = self._require_intent(payment_intent_id, conn=conn)
        refusal: AmlBlockError | LimitExceededError | SanctionsBlockError | None = None
        outcome: ConfirmOutcome | None = None
        intent_to_dispatch: PaymentIntentRecord | None = None

        with self._operation_transaction(conn) as tx_conn:
            self._lock_customer_limit_reservation(snapshot.customer_id, conn=tx_conn)
            intent = self._require_intent(payment_intent_id, conn=tx_conn)
            if intent.status == "REQUIRES_PAYMENT_METHOD":
                raise ConflictError(
                    "intent requires a payment method before confirm",
                    code="invalid_state_transition",
                )
            # COMP-08: PSP-licence / merchant-of-record gate fires HERE — before the
            # intent advances to PRE_FLIGHT and before ANY connector dispatch — so an
            # unlicensed platform moves no third-party funds at all (not move-then-error).
            # Refusal happens with no FSM mutation; the intent stays confirmable once
            # the licence activates.
            self._require_psp_license()
            pre_confirm_status = intent.status
            # Advance to PRE_FLIGHT WITHOUT the audit append. The audit chain
            # append acquires ``pg_advisory_xact_lock`` on the AUDIT domain, which
            # this transaction would then hold until commit. Preflight's sanctions
            # screen appends its SANCTIONS_SCREENING_COMPLETED audit on the ledger
            # store's OWN autocommit connection (durability-on-rollback design: the
            # screening record must survive a refused payment), which blocks on
            # this transaction's held lock — a self-deadlock that wedges ``confirm``
            # on Postgres (the in-memory path never acquires the lock, so it never
            # hung). Deferring the audit until preflight is done — so the screening
            # append has already committed — breaks the cycle. Atomicity is
            # preserved: the deferred audit still joins this transaction and rolls
            # back on refusal, matching the prior commit/rollback semantics.
            intent = self._advance_intent(
                intent, "confirm", conn=tx_conn, defer_audit=True
            )  # -> PRE_FLIGHT
            refusal_trigger: str | None = None
            try:
                # The PG limit data port reads on its own READ COMMITTED connection:
                # prior committed reservations are visible after the lock wait,
                # while this uncommitted PRE_FLIGHT row is not double-counted.
                result = self._preflight.run(
                    intent_id=intent.payment_intent_id,
                    merchant_id=intent.merchant_id,
                    customer_id=intent.customer_id,
                    amount_minor=intent.amount_minor,
                    method=intent.method,
                    payment_method_details=payment_method_details,
                    expires_at=intent.expires_at,
                    credits_customer_wallet=credits_customer_wallet,
                    clock=self._clock,
                )
            except LimitExceededError as exc:
                refusal = exc
                refusal_trigger = "limit_exceeded"
            except SanctionsBlockError as exc:
                refusal = exc
                refusal_trigger = "sanctions_hit"
            except AmlBlockError as exc:
                refusal = exc
                refusal_trigger = "aml_block"
            # Preflight is complete; the sanctions screen's separate-connection
            # audit append has committed, so this transaction can now acquire the
            # AUDIT chain lock for the deferred confirm-transition audit without
            # self-deadlocking.
            self._audit_transition(
                intent,
                from_state=pre_confirm_status,
                to_state=intent.status,
                conn=tx_conn,
            )
            if refusal_trigger is not None:
                self._fail_preflight(intent, refusal_trigger, conn=tx_conn)
            elif result.action is not None:
                intent = self._advance_intent(intent, "action_required", conn=tx_conn)
                outcome = ConfirmOutcome(
                    payment_intent_id=intent.payment_intent_id,
                    status=intent.status,
                    next_action_type=result.action.action_type,
                    next_action_token=result.action.action_token,
                )
            else:
                intent = self._advance_intent(intent, "preflight_passed", conn=tx_conn)
                self._emit(
                    "payment_intent.processing",
                    intent,
                    {"payment_intent_id": intent.payment_intent_id},
                    conn=tx_conn,
                )
                intent_to_dispatch = intent

        if refusal is not None:
            raise refusal
        if outcome is not None:
            return outcome
        if intent_to_dispatch is None:
            raise ConflictError("confirm produced no dispatchable intent")
        intent = await self._dispatch(intent_to_dispatch, attempt_number=1, conn=conn)
        return ConfirmOutcome(
            payment_intent_id=intent.payment_intent_id, status=intent.status
        )

    def _fail_preflight(
        self, intent: PaymentIntentRecord, trigger: str, *, conn: Any | None
    ) -> None:
        """Refusal-first: transition to FAILED with audit + event; the typed
        refusal is re-raised by the caller. No ledger write has happened."""
        failed = self._advance_intent(intent, trigger, conn=conn)
        self._emit(
            "payment_intent.failed",
            failed,
            {"payment_intent_id": failed.payment_intent_id, "failure_code": trigger},
            conn=conn,
        )

    async def complete_action(
        self,
        payment_intent_id: str,
        *,
        success: bool,
        conn: Any | None = None,
    ) -> ConfirmOutcome:
        """OTP verified / 3DS authenticated (or failed) — spec/02 FSM 1."""
        intent = self._require_intent(payment_intent_id, conn=conn)
        if not success:
            intent = self._advance_intent(intent, "action_failed", conn=conn)
            self._emit(
                "payment_intent.failed",
                intent,
                {
                    "payment_intent_id": intent.payment_intent_id,
                    "failure_code": "action_failed",
                },
                conn=conn,
            )
            return ConfirmOutcome(
                payment_intent_id=intent.payment_intent_id, status=intent.status
            )
        # COMP-08: gate the action-required continuation (OTP/3DS verified) BEFORE
        # the advance and dispatch too — e.g. a licence revoked between confirm and
        # action completion. No FSM mutation, no connector call on refusal.
        self._require_psp_license()
        intent = self._advance_intent(intent, "action_completed", conn=conn)
        self._emit(
            "payment_intent.processing",
            intent,
            {"payment_intent_id": intent.payment_intent_id},
            conn=conn,
        )
        intent = await self._dispatch(intent, attempt_number=1, conn=conn)
        return ConfirmOutcome(
            payment_intent_id=intent.payment_intent_id, status=intent.status
        )

    def supply_method(
        self, payment_intent_id: str, method: str, *, conn: Any | None = None
    ) -> PaymentIntentRecord:
        """``method_supplied`` trigger (portal/SDK method-selection step)."""
        if conn is None and self._transaction_factory is not None:
            with self._operation_transaction(conn) as tx_conn:
                return self.supply_method(payment_intent_id, method, conn=tx_conn)
        intent = self._require_intent(payment_intent_id, conn=conn)
        rule = INTENT_TABLE.resolve(intent.status, "method_supplied")
        updated = replace(
            intent, status=rule.to_state, method=method, updated_at=self._clock.now()
        )
        self._store.update_intent(updated, conn=conn, expected_status=intent.status)
        self._audit_transition(
            updated, from_state=intent.status, to_state=rule.to_state, conn=conn
        )
        return updated

    def cancel(self, payment_intent_id: str, *, conn: Any | None = None) -> PaymentIntentRecord:
        """Merchant cancel for pre-dispatch states (errata K-4 declared rows)."""
        if conn is None and self._transaction_factory is not None:
            with self._operation_transaction(conn) as tx_conn:
                return self.cancel(payment_intent_id, conn=tx_conn)
        intent = self._require_intent(payment_intent_id, conn=conn)
        if intent.status == "REQUIRES_CAPTURE":
            return self._void(intent, conn=conn)
        intent = self._advance_intent(
            intent, "cancel_requested", cancelled_at=self._clock.now(), conn=conn
        )
        self._emit(
            "payment_intent.cancelled",
            intent,
            {"payment_intent_id": intent.payment_intent_id, "reason": "merchant_cancel"},
            conn=conn,
        )
        return intent

    # ------------------------------------------------------------------
    # connector dispatch + result handling
    # ------------------------------------------------------------------

    def _next_rail_window(self, method: str, now: datetime) -> datetime:
        """Scheduled redispatch instant for a deferred intent (spec/16 §G).

        Errata E-S16-08: the redispatch instant is the next rail-window
        boundary from the BB calendar. The only deferrable method today is
        ``BEFTN_CREDIT`` (routing `_DEFERRABLE_METHODS`): deferred files
        queue for the next same-day cutoff — 12:30 Dhaka on a working day
        (spec/02's scheduler task fires at session open and flushes by the
        cutoff). Future deferrable methods take the same conservative
        working-day boundary unless their spec pins a different window.
        """
        del method  # one deferrable method in v1; signature is the contract
        return self._calendar.next_beftn_cutoff(now)

    async def _dispatch(
        self,
        intent: PaymentIntentRecord,
        *,
        attempt_number: int,
        conn: Any | None = None,
    ) -> PaymentIntentRecord:
        # COMP-08 chokepoint backstop: NO connector dispatch on an inactive PSP
        # licence from ANY path (confirm / complete_action / store-and-forward
        # redispatch). The clean pre-advance gates sit in confirm()/complete_action()/
        # capture(); this guarantees no rail movement can originate here.
        self._require_psp_license()
        connector_id = self._routing.select_connector(intent.method, clock=self._clock)
        if connector_id == STORE_AND_FORWARD:
            # spec/16 §G (errata E-S16-08): the payload carries the kernel's
            # actual scheduled redispatch instant, not a sentinel string.
            deferred_until = self._next_rail_window(intent.method, self._clock.now())
            intent = replace(
                intent, deferred_until=deferred_until, updated_at=self._clock.now()
            )
            self._store.update_intent(intent, conn=conn, expected_status=intent.status)
            self._emit(
                "payment_intent.deferred",
                intent,
                {
                    "payment_intent_id": intent.payment_intent_id,
                    "rail": intent.method,
                    "deferred_until": _rfc3339(deferred_until),
                },
                conn=conn,
            )
            return intent  # stays PROCESSING (spec/02 store-and-forward)
        if intent.deferred_until is not None:
            # A real connector is dispatchable again — the deferral is over.
            intent = replace(intent, deferred_until=None, updated_at=self._clock.now())
            self._store.update_intent(intent, conn=conn, expected_status=intent.status)
        now = self._clock.now()
        attempt_id = make_id(
            "pa",
            {
                "payment_intent_id": intent.payment_intent_id,
                "attempt_number": attempt_number,
                "connector_id": connector_id,
                "created_at": now,
            },
        )
        connector_ref = make_kernel_id(
            "cref", {"attempt_id": attempt_id, "connector_id": connector_id}
        )
        attempt = PaymentAttemptRecord(
            attempt_id=attempt_id,
            payment_intent_id=intent.payment_intent_id,
            attempt_number=attempt_number,
            connector_id=connector_id,
            instruction_id=make_id("ins", {"connector_ref": connector_ref}),
            connector_ref=connector_ref,
            amount_minor=intent.amount_minor,
            status="STARTED",
            created_at=now,
            updated_at=now,
        )
        self._store.insert_attempt(attempt, conn=conn)
        # T-27: refresh latest_attempt_id on the local intent object. insert_attempt
        # writes this pointer to the store (PG: UPDATE payment_intents SET
        # latest_attempt_id = %s; memory: replaces the dict entry) but does NOT
        # update the caller's local variable. Without this refresh, the subsequent
        # _advance_intent → update_intent call writes the pre-insert None value back,
        # clobbering the correct pointer and breaking every sweep that JOINs on it.
        intent = replace(intent, latest_attempt_id=attempt.attempt_id)
        attempt = self._advance_attempt(attempt, "submitted", conn=conn)
        instruction = PaymentInstruction(
            instruction_id=attempt.instruction_id,
            connector_ref=connector_ref,
            amount=SdkMoney(amount_minor=intent.amount_minor),
            method=intent.method,
            sender_ref=intent.customer_id or intent.merchant_id,
            beneficiary_ref=intent.merchant_id,
            rail=connector_id,
            instruction_at=now.isoformat(),
            metadata=dict(intent.metadata),
        )
        result = await self._runner.submit(instruction)
        return await self._apply_connector_result(intent, attempt, result, conn=conn)

    async def _apply_connector_result(
        self,
        intent: PaymentIntentRecord,
        attempt: PaymentAttemptRecord,
        result: Any,
        *,
        conn: Any | None,
    ) -> PaymentIntentRecord:
        if conn is None and self._transaction_factory is not None:
            with self._operation_transaction(conn) as tx_conn:
                return await self._apply_connector_result(
                    intent, attempt, result, conn=tx_conn
                )
        now = self._clock.now()
        attempt = replace(
            attempt,
            rail_transaction_id=result.rail_transaction_id,
            error_code=result.error_code,
            raw_response_hash=result.raw_response_hash,
            updated_at=now,
        )
        status = result.status
        if status == ConnectorStatus.SUCCESS:
            if intent.capture_method == "manual":
                attempt = self._advance_attempt(
                    attempt,
                    "connector_authorized",
                    authorized_at=now,
                    auth_expires_at=now + AUTH_HOLD_DEFAULT,
                    conn=conn,
                )
                try:
                    self._open_authorization_hold(intent, attempt, conn=conn)
                except Exception as exc:
                    self._record_ledger_post_failure(intent, attempt, exc)
                    raise
                self._emit_attempt(
                    "payment_attempt.authorized", intent, attempt, conn=conn
                )
                # Repoint the intent's expires_at to the auth-hold deadline so
                # the TTL sweep picks it up at the correct 7-day boundary
                # (spec/02 REQUIRES_CAPTURE ttl_expired).  The original 30-min
                # expires_at is no longer the relevant deadline once the
                # authorization is open.
                return self._advance_intent(
                    intent,
                    "attempt_authorized",
                    conn=conn,
                    expires_at=attempt.auth_expires_at,
                )
            attempt = self._advance_attempt(
                attempt, "connector_success_direct", charged_at=now, conn=conn
            )
            return self._settle_charge(intent, attempt, conn=conn)
        if status == ConnectorStatus.PENDING:
            self._store.update_attempt(attempt, conn=conn, expected_status=attempt.status)
            return intent  # stays PROCESSING; callback/poll completes it
        if status == ConnectorStatus.REJECTED:
            attempt = self._advance_attempt(
                attempt, "connector_rejected", failed_at=now, conn=conn
            )
            self._emit_attempt("payment_attempt.hard_declined", intent, attempt, conn=conn)
            intent = self._advance_intent(intent, "attempt_hard_declined", conn=conn)
            self._emit(
                "payment_intent.failed",
                intent,
                {
                    "payment_intent_id": intent.payment_intent_id,
                    "failure_code": attempt.error_code or "hard_declined",
                },
                conn=conn,
            )
            return intent
        if status == ConnectorStatus.FAILED:
            attempt = self._advance_attempt(
                attempt, "connector_failed", failed_at=now, conn=conn
            )
            if attempt.attempt_number < MAX_ATTEMPTS_PER_INTENT:
                intent = self._advance_intent(intent, "attempt_failed_retryable", conn=conn)
                return await self._dispatch(
                    intent, attempt_number=attempt.attempt_number + 1, conn=conn
                )
            intent = self._advance_intent(intent, "attempt_hard_declined", conn=conn)
            self._emit(
                "payment_intent.failed",
                intent,
                {
                    "payment_intent_id": intent.payment_intent_id,
                    "failure_code": "retries_exhausted",
                },
                conn=conn,
            )
            return intent
        if status == ConnectorStatus.TIMED_OUT:
            attempt = self._advance_attempt(attempt, "connector_timeout", conn=conn)
            return self._advance_intent(intent, "attempt_timed_out", conn=conn)
        raise ConflictError(
            f"unhandled connector status {status!r}", code="connector_status_unknown"
        )

    async def apply_inbound_connector_result(
        self, connector_id: str, result: Any, *, conn: Any | None = None
    ) -> str:
        """Apply a verified connector callback to the owning payment/refund FSM.

        Returns ``DISPATCHED`` only when the callback was consumed by a pending
        domain object. Unknown refs, instruction mismatches, and terminal
        duplicates are marked ``SKIPPED`` by the durable inbox drainer.
        """
        if conn is None and self._transaction_factory is not None:
            with self._operation_transaction(conn) as tx_conn:
                return await self.apply_inbound_connector_result(
                    connector_id, result, conn=tx_conn
                )
        attempt = self._store.find_attempt_by_connector_ref(
            connector_id, result.connector_ref, conn=conn
        )
        if attempt is not None:
            if attempt.instruction_id != result.instruction_id:
                return "SKIPPED"
            if attempt.status == "AUTHORIZATION_PENDING":
                intent = self._require_intent(attempt.payment_intent_id, conn=conn)
                await self._apply_connector_result(intent, attempt, result, conn=conn)
                return "DISPATCHED"
            if attempt.status == "TIMED_OUT":
                self._apply_status_poll_result(attempt.attempt_id, result, conn=conn)
                return "DISPATCHED"
            return "SKIPPED"

        refund = self._store.find_refund_by_connector_ref(
            connector_id, result.connector_ref, conn=conn
        )
        if refund is None:
            return "SKIPPED"
        if refund.instruction_id is not None and refund.instruction_id != result.instruction_id:
            return "SKIPPED"
        if refund.status != "REFUND_PENDING_CONNECTOR":
            return "SKIPPED"
        self._apply_refund_result(refund.refund_id, result, conn=conn)
        return "DISPATCHED"

    async def poll_timed_out_attempt(
        self, attempt_id: str, *, conn: Any | None = None
    ) -> PaymentIntentRecord:
        """spec/02 timeout status-poll semantics (up to 3 polls then reverse)."""
        attempt = self._store.get_attempt(attempt_id, conn=conn)
        if attempt is None:
            raise NotFoundError(f"unknown attempt {attempt_id}")
        self._require_intent(attempt.payment_intent_id, conn=conn)
        result = await self._runner.query_status(
            attempt.connector_id, attempt.connector_ref, attempt.rail_transaction_id
        )
        if conn is None and self._transaction_factory is not None:
            with self._operation_transaction(conn) as tx_conn:
                return self._apply_status_poll_result(
                    attempt_id, result, conn=tx_conn
                )
        return self._apply_status_poll_result(attempt_id, result, conn=conn)

    def _apply_status_poll_result(
        self, attempt_id: str, result: Any, *, conn: Any | None
    ) -> PaymentIntentRecord:
        attempt = self._store.get_attempt(attempt_id, conn=conn)
        if attempt is None:
            raise NotFoundError(f"unknown attempt {attempt_id}")
        intent = self._require_intent(attempt.payment_intent_id, conn=conn)
        now = self._clock.now()
        if result.status == ConnectorStatus.SUCCESS:
            attempt = replace(
                attempt,
                rail_transaction_id=result.rail_transaction_id or attempt.rail_transaction_id,
                raw_response_hash=result.raw_response_hash,
                updated_at=now,
            )
            attempt = self._advance_attempt(
                attempt, "poll_success", guard="charged", charged_at=now, conn=conn
            )
            return self._settle_charge(intent, attempt, conn=conn)
        if result.status == ConnectorStatus.REVERSED:
            # The rail has already reversed the pre-authorisation; do NOT book
            # a capture. Route the intent into the reversal lifecycle so the
            # merchant sees a clean REVERSED outcome.
            attempt = replace(
                attempt,
                rail_transaction_id=result.rail_transaction_id or attempt.rail_transaction_id,
                raw_response_hash=result.raw_response_hash,
                updated_at=now,
            )
            attempt = self._advance_attempt(attempt, "poll_reversed", conn=conn)
            intent = self._advance_intent(intent, "reversal_triggered", conn=conn)
            self._emit(
                "payment_intent.reversal_initiated",
                intent,
                {
                    "payment_intent_id": intent.payment_intent_id,
                    "attempt_id": attempt.attempt_id,
                },
                conn=conn,
            )
            return intent
        if result.status == ConnectorStatus.PENDING:
            attempt = replace(attempt, poll_count=attempt.poll_count + 1, updated_at=now)
            if attempt.poll_count >= MAX_STATUS_POLLS:
                attempt = self._advance_attempt(
                    attempt, "poll_exhausted", failed_at=now, conn=conn
                )
                intent = self._advance_intent(intent, "reversal_triggered", conn=conn)
                self._emit(
                    "payment_intent.reversal_initiated",
                    intent,
                    {
                        "payment_intent_id": intent.payment_intent_id,
                        "attempt_id": attempt.attempt_id,
                    },
                    conn=conn,
                )
                return intent
            attempt = self._advance_attempt(attempt, "poll_pending", conn=conn)
            return intent
        # FAILED / REJECTED from the poll
        attempt = self._advance_attempt(attempt, "poll_failed", failed_at=now, conn=conn)
        intent = self._advance_intent(intent, "attempt_hard_declined", conn=conn)
        self._emit(
            "payment_intent.failed",
            intent,
            {
                "payment_intent_id": intent.payment_intent_id,
                "failure_code": result.error_code or "poll_failed",
            },
            conn=conn,
        )
        return intent

    # ------------------------------------------------------------------
    # capture / void / reversal
    # ------------------------------------------------------------------

    def capture(
        self,
        payment_intent_id: str,
        *,
        amount_minor: int | None = None,
        conn: Any | None = None,
    ) -> PaymentIntentRecord:
        """Manual capture (REQUIRES_CAPTURE -> SUCCEEDED; partial allowed)."""
        if conn is None and self._transaction_factory is not None:
            with self._operation_transaction(conn) as tx_conn:
                return self.capture(
                    payment_intent_id, amount_minor=amount_minor, conn=tx_conn
                )
        intent = self._require_intent(payment_intent_id, conn=conn)
        if intent.status != "REQUIRES_CAPTURE":
            raise ConflictError(
                "intent is not awaiting capture", code="invalid_capture_state"
            )
        attempt = self._latest_attempt(intent, conn=conn)
        if attempt is None or attempt.status != "AUTHORIZED":
            raise ConflictError("no AUTHORIZED attempt to capture", code="invalid_capture_state")
        capture_minor = intent.amount_minor if amount_minor is None else amount_minor
        if not isinstance(capture_minor, int) or isinstance(capture_minor, bool):
            raise InvalidRequestError("amount_minor must be int paisa")
        if not 0 < capture_minor <= attempt.amount_minor:
            raise ConflictError(
                "capture amount must be > 0 and <= authorized amount",
                code="invalid_capture_state",
            )
        now = self._clock.now()
        # COMP-08: refuse before ANY FSM mutation or settlement on the manual
        # capture path (no move/mutate-then-error).
        self._require_psp_license()
        attempt = self._advance_attempt(attempt, "capture_submitted", conn=conn)
        attempt = self._advance_attempt(
            attempt, "capture_confirmed", charged_at=now, conn=conn
        )
        if capture_minor != intent.amount_minor:
            # partial capture burns the remaining authorization (spec/02)
            intent = replace(intent, amount_minor=capture_minor, updated_at=now)
            self._store.update_intent(intent, conn=conn, expected_status=intent.status)
            attempt = replace(attempt, amount_minor=capture_minor, updated_at=now)
            self._store.update_attempt(attempt, conn=conn, expected_status=attempt.status)
        return self._settle_authorized_charge(
            intent, attempt, trigger="capture_requested", conn=conn
        )

    def _void(
        self, intent: PaymentIntentRecord, *, conn: Any | None
    ) -> PaymentIntentRecord:
        attempt = self._latest_attempt(intent, conn=conn)
        now = self._clock.now()
        if attempt is not None and attempt.status == "AUTHORIZED":
            hold_id = self._ledger.get_open_hold_id(intent.payment_intent_id, conn=conn)
            if hold_id is None:
                raise ConflictError(
                    "authorized intent has no open ledger hold",
                    code="authorization_hold_missing",
                )
            self._ledger.release_hold(hold_id, "VOIDED", clock=self._clock, conn=conn)
            attempt = self._advance_attempt(attempt, "void_submitted", conn=conn)
            self._emit_attempt("payment_attempt.voided", intent, attempt, conn=conn)
        intent = self._advance_intent(
            intent, "void_requested", cancelled_at=now, conn=conn
        )
        self._emit(
            "payment_intent.cancelled",
            intent,
            {"payment_intent_id": intent.payment_intent_id, "reason": "void"},
            conn=conn,
        )
        return intent

    async def execute_reversal(
        self, payment_intent_id: str, *, reason: str, conn: Any | None = None
    ) -> PaymentIntentRecord:
        """REVERSAL_INITIATED -> REVERSAL_PENDING -> REVERSED | FAILED."""
        # ORCH-1: refusal must happen BEFORE any rail/ledger write.  A
        # suspended PSP licence means the platform may no longer act as
        # merchant-of-record on a rail reversal; like charging, this is
        # BFIU/BB-reportable if it proceeds while the licence is inactive.
        self._require_psp_license()
        if conn is None and self._transaction_factory is not None:
            with self._operation_transaction(conn) as tx_conn:
                _, attempt = self._claim_reversal_dispatch(
                    payment_intent_id, conn=tx_conn
                )
        else:
            _, attempt = self._claim_reversal_dispatch(payment_intent_id, conn=conn)
        # Rail-retry safety: attempt.connector_ref is the ORIGINAL charge
        # attempt's reference — reversal never creates a new attempt record
        # (_advance_attempt only updates status/timestamps in place via
        # dataclasses.replace), so this value is identical across every retry
        # of a reversal for the same intent (dispatch_pending_reversals may
        # call execute_reversal on the same payment_intent_id more than once
        # after a stranded REVERSAL_PENDING claim).  Per the connector SDK
        # contract (connectors/sdk.py: "a connector MUST be idempotent on
        # connector_ref"), a retried reverse() submission is therefore
        # already deduplicable by the rail on the existing connector_ref —
        # no additional reference/idempotency field is invented here.
        result = await self._runner.reverse(
            attempt.connector_id,
            attempt.connector_ref,
            attempt.rail_transaction_id,
            SdkMoney(amount_minor=attempt.amount_minor),
            reason,
        )
        if conn is None and self._transaction_factory is not None:
            with self._operation_transaction(conn) as tx_conn:
                return self._apply_reversal_result(
                    payment_intent_id, result, conn=tx_conn
            )
        return self._apply_reversal_result(payment_intent_id, result, conn=conn)

    def _claim_reversal_dispatch(
        self, payment_intent_id: str, *, conn: Any | None
    ) -> tuple[PaymentIntentRecord, PaymentAttemptRecord]:
        intent = self._store.lock_intent(payment_intent_id, conn=conn)
        if intent is None:
            raise NotFoundError(f"unknown payment intent {payment_intent_id}")
        if intent.status == "REVERSAL_INITIATED":
            intent = self._advance_intent(
                intent, "reversal_dispatch_claimed", conn=conn
            )
        elif intent.status == "REVERSAL_PENDING":
            retry_at = intent.updated_at + REVERSAL_PENDING_RETRY_AFTER
            if self._clock.now() < retry_at:
                raise ConflictError(
                    "reversal is already in progress",
                    code="reversal_already_in_progress",
                )
            intent = self._advance_intent(
                intent, "reversal_dispatch_claimed", conn=conn
            )
        else:
            raise ConflictError(
                "intent is not in REVERSAL_INITIATED", code="invalid_state_transition"
            )
        attempt = self._latest_attempt(intent, conn=conn)
        if attempt is None:
            raise ConflictError("no attempt to reverse", code="invalid_state_transition")
        return intent, attempt

    def _apply_reversal_result(
        self, payment_intent_id: str, result: Any, *, conn: Any | None
    ) -> PaymentIntentRecord:
        intent = self._require_intent(payment_intent_id, conn=conn)
        if intent.status not in ("REVERSAL_INITIATED", "REVERSAL_PENDING"):
            raise ConflictError(
                "intent is not in a reversible state", code="invalid_state_transition"
            )
        attempt = self._latest_attempt(intent, conn=conn)
        if attempt is None:
            raise ConflictError("no attempt to reverse", code="invalid_state_transition")
        now = self._clock.now()
        if result.status in (ConnectorStatus.REVERSED, ConnectorStatus.SUCCESS):
            was_charged = attempt.status == "CHARGED"
            if was_charged:
                attempt = self._advance_attempt(attempt, "reversal_submitted", conn=conn)
                self._post_reversal_entries(intent, attempt, conn=conn)
                self._emit_attempt("payment_attempt.reversed", intent, attempt, conn=conn)
            elif attempt.status == "AUTHORIZED":
                # Auth-only reversal (REQUIRES_CAPTURE ttl_expired): the
                # connector voided the authorization.  Release the
                # merchant_hold_reserve and advance the attempt to VOIDED.
                # No journal entry is needed — the capture never posted, so
                # there is no rail movement to offset (only the hold release).
                hold_id = self._ledger.get_open_hold_id(
                    intent.payment_intent_id, conn=conn
                )
                if hold_id is not None:
                    self._ledger.release_hold(
                        hold_id, "VOIDED", clock=self._clock, conn=conn
                    )
                attempt = self._advance_attempt(
                    attempt, "void_submitted", conn=conn
                )
                self._emit_attempt(
                    "payment_attempt.voided", intent, attempt, conn=conn
                )
            intent = self._advance_intent(
                intent, "reversal_confirmed", reversed_at=now, conn=conn
            )
            self._emit(
                "payment_intent.reversed",
                intent,
                {
                    "payment_intent_id": intent.payment_intent_id,
                    "attempt_id": attempt.attempt_id,
                    "amount_minor": attempt.amount_minor,
                },
                conn=conn,
            )
            return intent
        intent = self._advance_intent(intent, "reversal_failed", conn=conn)
        self._emit(
            "compensation_item.queued",
            intent,
            {
                "compensation_id": make_kernel_id(
                    "cref",
                    {
                        "payment_intent_id": intent.payment_intent_id,
                        "purpose": "compensation",
                    },
                ),
                "resolution_kind": "manual_reversal",
                "amount_minor": attempt.amount_minor,
            },
            topic="platform.events",
            conn=conn,
        )
        return intent

    # ------------------------------------------------------------------
    # TTL sweep (scheduler task 'ttl-sweep', spec/02 TTL policy)
    # ------------------------------------------------------------------

    def sweep_expired_intents(self, *, conn: Any | None = None) -> int:
        """Move every TTL-expired intent per the spec/02 table. Returns count."""
        if conn is None and self._transaction_factory is not None:
            with self._operation_transaction(conn) as tx_conn:
                return self.sweep_expired_intents(conn=tx_conn)
        now = self._clock.now()
        moved = 0
        for intent in self._store.list_expired_intents(now=now, conn=conn):
            if intent.status in ("CREATED", "REQUIRES_PAYMENT_METHOD", "REQUIRES_CONFIRMATION"):
                updated = self._advance_intent(
                    intent, "ttl_expired", cancelled_at=now, conn=conn
                )
                self._emit(
                    "payment_intent.expired",
                    updated,
                    {"payment_intent_id": updated.payment_intent_id},
                    conn=conn,
                )
            elif intent.status in ("PRE_FLIGHT", "REQUIRES_ACTION"):
                # PRE_FLIGHT shares the 30-min TTL window (spec/02 TTL policy);
                # REQUIRES_ACTION expiry is a FAILED terminal.
                trigger = "ttl_expired" if intent.status == "REQUIRES_ACTION" else None
                if trigger is None:
                    # PRE_FLIGHT has no declared ttl row; it is transient —
                    # an intent stuck here is cancelled via the CREATED-family
                    # path once confirm aborts. Refusal-first: skip.
                    continue
                updated = self._advance_intent(intent, "ttl_expired", conn=conn)
                self._emit(
                    "payment_intent.failed",
                    updated,
                    {
                        "payment_intent_id": updated.payment_intent_id,
                        "failure_code": "action_ttl_expired",
                    },
                    conn=conn,
                )
            elif intent.status == "PROCESSING":
                updated = self._advance_intent(intent, "ttl_expired", conn=conn)
                self._emit(
                    "payment_intent.reversal_initiated",
                    updated,
                    {
                        "payment_intent_id": updated.payment_intent_id,
                        "attempt_id": updated.latest_attempt_id,
                    },
                    conn=conn,
                )
            elif intent.status == "REQUIRES_CAPTURE":
                # spec/02: REQUIRES_CAPTURE + ttl_expired (now > auth_expires_at)
                # → REVERSAL_INITIATED.  expires_at was repointed to
                # auth_expires_at when the auth hold opened, so the sweep
                # query picks this up at the 7-day boundary.  The downstream
                # reversal-dispatch task calls execute_reversal, which
                # releases the merchant_hold_reserve.
                updated = self._advance_intent(intent, "ttl_expired", conn=conn)
                self._emit(
                    "payment_intent.reversal_initiated",
                    updated,
                    {
                        "payment_intent_id": updated.payment_intent_id,
                        "attempt_id": updated.latest_attempt_id,
                    },
                    conn=conn,
                )
            else:  # refusal-first: nothing else is swept
                continue
            moved += 1
        return moved

    async def dispatch_pending_reversals(self) -> list[str]:
        """Scheduler task 'reversal-dispatch': drive every REVERSAL_INITIATED
        intent, and every REVERSAL_PENDING intent whose retry window has
        elapsed, through ``execute_reversal`` so the connector reverses the
        rail instruction and the hold/charge is released.

        Without this task, the TTL sweep moves expired PROCESSING /
        REQUIRES_CAPTURE intents to REVERSAL_INITIATED but nothing actually
        executes the reversal — the hold stays locked forever.

        REVERSAL_PENDING must also be swept: ``_claim_reversal_dispatch``
        advances an intent to REVERSAL_PENDING as soon as it claims the
        dispatch, BEFORE the rail call happens.  If that rail call then
        raises (connector down, timeout, process crash), the intent is
        stranded in REVERSAL_PENDING — selecting only REVERSAL_INITIATED
        here would never look at it again and the hold would stay locked
        forever.  ``_claim_reversal_dispatch`` re-claims a REVERSAL_PENDING
        row once ``REVERSAL_PENDING_RETRY_AFTER`` has elapsed since it was
        last touched; a still-hot row (inside that window, e.g. another
        dispatch in flight) raises ``ConflictError(code=
        "reversal_already_in_progress")``, which is treated as a quiet,
        expected skip rather than a logged failure.

        Per-intent isolation: one failing reversal (connector unavailable,
        conflict) does not block the others; the next sweep retries.
        """
        intents = list(
            self._store.list_intents(status="REVERSAL_INITIATED", limit=100)
        ) + list(self._store.list_intents(status="REVERSAL_PENDING", limit=100))
        dispatched: list[str] = []
        for intent in intents:
            try:
                await self.execute_reversal(
                    intent.payment_intent_id, reason="scheduler_reversal"
                )
                dispatched.append(intent.payment_intent_id)
            except ConflictError as exc:
                if exc.code == "reversal_already_in_progress":
                    self._log.debug(
                        "dispatch_pending_reversals: %s still within the "
                        "retry window, skipping (hot REVERSAL_PENDING row)",
                        intent.payment_intent_id,
                    )
                # any other ConflictError (e.g. no attempt to reverse) falls
                # through with the same per-intent isolation as before.
            except Exception:
                pass
        return dispatched

    # ------------------------------------------------------------------
    # delivery confirmation (spec/02 §confirm-delivery)
    # ------------------------------------------------------------------

    def confirm_delivery(
        self,
        payment_intent_id: str,
        *,
        delivered_at: datetime,
        conn: Any | None = None,
    ) -> PaymentIntentRecord:
        if conn is None and self._transaction_factory is not None:
            with self._operation_transaction(conn) as tx_conn:
                return self.confirm_delivery(
                    payment_intent_id, delivered_at=delivered_at, conn=tx_conn
                )
        intent = self._require_intent(payment_intent_id, conn=conn)
        if intent.status not in ("SUCCEEDED", "PARTIALLY_REFUNDED"):
            raise ConflictError(
                "delivery can only be confirmed for a succeeded intent",
                code="intent_not_succeeded",
            )
        now = self._clock.now()
        if delivered_at > now:
            raise InvalidRequestError(
                "delivered_at cannot be in the future", code="delivered_at_in_future"
            )
        if intent.delivery_confirmed_at is not None:
            return intent  # idempotent second call: no event
        updated = replace(intent, delivery_confirmed_at=now, updated_at=now)
        self._store.update_intent(updated, conn=conn, expected_status=intent.status)
        self._audit_transition(
            updated,
            from_state=intent.status,
            to_state=intent.status,
            event_type="DELIVERY_CONFIRMED",
            conn=conn,
        )
        self._emit(
            "payment_intent.delivery_confirmed",
            updated,
            {
                "payment_intent_id": updated.payment_intent_id,
                "delivery_confirmed_at": now,
            },
            conn=conn,
        )
        return updated

    # ------------------------------------------------------------------
    # refunds (new offsetting flows, never mutations)
    # ------------------------------------------------------------------

    def create_refund(
        self,
        payment_intent_id: str,
        *,
        amount_minor: int,
        reason: str,
        idempotency_key: str | None = None,
        conn: Any | None = None,
    ) -> RefundRecord:
        # ORCH-1: refunds debit MERCHANT_SETTLEMENT and credit the customer
        # back via the rail, so a suspended PSP licence must block this path
        # before any transaction, lock, idempotency reservation, or FSM write.
        self._require_psp_license()
        if conn is None and self._transaction_factory is not None:
            with self._operation_transaction(conn) as tx_conn:
                return self.create_refund(
                    payment_intent_id,
                    amount_minor=amount_minor,
                    reason=reason,
                    idempotency_key=idempotency_key,
                    conn=tx_conn,
                )
        if idempotency_key is not None:
            if not idempotency_key:
                raise InvalidRequestError(
                    "idempotency_key must be non-empty",
                    code="idempotency_key_required",
                )
        intent = self._store.lock_intent(payment_intent_id, conn=conn)
        if intent is None:
            raise NotFoundError(f"unknown payment intent {payment_intent_id}")
        if idempotency_key is not None:
            existing = self._store.find_refund_by_idempotency_key(
                payment_intent_id, idempotency_key, conn=conn
            )
            if existing is not None:
                if existing.amount_minor != amount_minor or existing.reason != reason:
                    raise IdempotencyConflictError(
                        "Idempotency-Key replayed with mismatched refund parameters"
                    )
                return existing
        if intent.status not in ("SUCCEEDED", "PARTIALLY_REFUNDED"):
            raise ConflictError(
                "refunds require a SUCCEEDED or PARTIALLY_REFUNDED intent",
                code="invalid_state_transition",
            )
        charged = self._charged_attempt(intent, conn=conn)
        if charged is None:
            raise ConflictError("no charged attempt to refund", code="invalid_state_transition")
        remaining = intent.amount_minor - self._reserved_refund_total(intent, conn=conn)
        if amount_minor > remaining:
            raise LimitExceededError(
                "refund exceeds the remaining refundable amount",
                code="refund_exceeds_captured",
            )
        now = self._clock.now()
        refund_id = make_id(
            "rfnd",
            {
                "payment_intent_id": intent.payment_intent_id,
                "attempt_id": charged.attempt_id,
                "amount_minor": amount_minor,
                "created_at": now,
            },
        )
        connector_ref = make_kernel_id(
            "cref", {"refund_id": refund_id, "connector_id": charged.connector_id}
        )
        instruction_id = make_id(
            "ins", {"connector_ref": connector_ref, "operation": "refund"}
        )
        refund = RefundRecord(
            refund_id=refund_id,
            payment_intent_id=intent.payment_intent_id,
            attempt_id=charged.attempt_id,
            amount_minor=amount_minor,
            reason=reason,
            status="REFUND_INITIATED",
            connector_id=charged.connector_id,
            connector_ref=connector_ref,
            instruction_id=instruction_id,
            idempotency_key=idempotency_key,
            created_at=now,
            updated_at=now,
        )
        self._store.insert_refund(refund, conn=conn)
        intent = self._advance_intent(intent, "refund_initiated", conn=conn)
        self._emit(
            "refund.initiated",
            intent,
            {
                "refund_id": refund_id,
                "payment_intent_id": intent.payment_intent_id,
                "amount_minor": amount_minor,
                "reason": reason,
            },
            subject_type="Refund",
            subject_id=refund_id,
            conn=conn,
        )
        return refund

    async def process_refund(
        self, refund_id: str, *, conn: Any | None = None
    ) -> RefundRecord:
        """Dispatch the refund through the connector and settle the outcome."""
        # ORCH-1: process_refund performs the actual rail reversal (runner.reverse),
        # which is the same licence-gated act as create_refund.  A refund created
        # before licence suspension must not be dispatched on the rail after
        # suspension (no new rail money-movement under an inactive licence).
        self._require_psp_license()
        refund = self._store.get_refund(refund_id, conn=conn)
        if refund is None:
            raise NotFoundError(f"unknown refund {refund_id}")
        self._require_intent(refund.payment_intent_id, conn=conn)
        if refund.status == "REFUND_INITIATED":
            refund = self._advance_refund(refund, "connector_dispatched", conn=conn)
        result = await self._runner.reverse(
            refund.connector_id or "",
            refund.connector_ref or refund.refund_id,
            refund.rail_transaction_id,
            SdkMoney(amount_minor=refund.amount_minor),
            refund.reason,
        )
        if conn is None and self._transaction_factory is not None:
            with self._operation_transaction(conn) as tx_conn:
                return self._apply_refund_result(refund_id, result, conn=tx_conn)
        return self._apply_refund_result(refund_id, result, conn=conn)

    def _apply_refund_result(
        self, refund_id: str, result: Any, *, conn: Any | None
    ) -> RefundRecord:
        refund = self._store.get_refund(refund_id, conn=conn)
        if refund is None:
            raise NotFoundError(f"unknown refund {refund_id}")
        # ORCH-H3: acquire the intent row lock (SELECT ... FOR UPDATE) BEFORE the
        # over-refund guard reads _refunded_total below, so the read-check-advance-post
        # critical section is serialized across concurrent refund settlements on the
        # same intent.  A plain SELECT (read_committed) lets two distinct refunds
        # (different connector_ref / null idem_key — so neither UNIQUE constraint in
        # 0034_refunds.sql collides) both read the SAME refunded_total, both pass the
        # guard, and both post — driving total refunded past the captured amount and a
        # NEGATIVE MERCHANT_SETTLEMENT ledger.  This mirrors create_refund's lock at
        # the create-time guard; lock ordering is identical (intent row first, then
        # ledger/refund writes), so no new deadlock cycle is introduced.  In the
        # in-memory path (conn is None) lock_intent falls back to get_intent — a no-op
        # lock that is correct because that path has no concurrent transaction.
        intent = self._store.lock_intent(refund.payment_intent_id, conn=conn)
        if intent is None:
            raise NotFoundError(f"unknown payment intent {refund.payment_intent_id}")
        now = self._clock.now()
        if result.status in (ConnectorStatus.SUCCESS, ConnectorStatus.REVERSED):
            if self._refunded_total(intent, conn=conn) + refund.amount_minor > intent.amount_minor:
                raise LimitExceededError(
                    "refund exceeds the captured amount",
                    code="refund_exceeds_captured",
                )
            refund = replace(
                refund,
                rail_transaction_id=result.rail_transaction_id,
                raw_response_hash=result.raw_response_hash,
                succeeded_at=now,
                updated_at=now,
            )
            refund = self._advance_refund(refund, "connector_success", conn=conn)
            self._post_refund_entries(intent, refund, conn=conn)
            refunded_total = self._refunded_total(intent, conn=conn)
            if refunded_total >= intent.amount_minor:
                intent = self._advance_intent(intent, "refund_full_succeeded", conn=conn)
            else:
                intent = self._advance_intent(intent, "refund_partial_succeeded", conn=conn)
            self._emit(
                "refund.succeeded",
                intent,
                {
                    "refund_id": refund.refund_id,
                    "payment_intent_id": intent.payment_intent_id,
                    "amount_minor": refund.amount_minor,
                },
                subject_type="Refund",
                subject_id=refund.refund_id,
                conn=conn,
            )
            return refund
        if result.status == ConnectorStatus.REJECTED:
            refund = replace(refund, failed_at=now, updated_at=now)
            refund = self._advance_refund(refund, "connector_rejected", conn=conn)
            self._return_intent_after_refund_failure(intent, refund, conn=conn)
            return refund
        # FAILED / TIMED_OUT — retry budget
        refund = replace(refund, retry_count=refund.retry_count + 1, updated_at=now)
        if refund.retry_count >= MAX_REFUND_RETRIES:
            refund = replace(refund, failed_at=now)
            refund = self._advance_refund(refund, "retries_exhausted", conn=conn)
            self._return_intent_after_refund_failure(intent, refund, conn=conn)
            return refund
        trigger = (
            "connector_timeout"
            if result.status == ConnectorStatus.TIMED_OUT
            else "connector_failed"
        )
        refund = self._advance_refund(refund, trigger, conn=conn)
        return refund

    def _return_intent_after_refund_failure(
        self, intent: PaymentIntentRecord, refund: RefundRecord, *, conn: Any | None
    ) -> None:
        prior_partial = self._refunded_total(intent, conn=conn) > 0
        guard = "prior_partial" if prior_partial else "no_prior_partial"
        intent = self._advance_intent(intent, "refund_failed", guard=guard, conn=conn)
        self._emit(
            "refund.failed",
            intent,
            {"refund_id": refund.refund_id, "error_code": "refund_failed"},
            subject_type="Refund",
            subject_id=refund.refund_id,
            conn=conn,
        )

    def _refunded_total(
        self, intent: PaymentIntentRecord, *, conn: Any | None
    ) -> int:
        return sum(
            r.amount_minor
            for r in self._store.list_refunds(intent.payment_intent_id, conn=conn)
            if r.status == "REFUND_SUCCEEDED"
        )

    def _reserved_refund_total(
        self, intent: PaymentIntentRecord, *, conn: Any | None
    ) -> int:
        return sum(
            r.amount_minor
            for r in self._store.list_refunds(intent.payment_intent_id, conn=conn)
            if r.status != "REFUND_FAILED"
        )

    # ------------------------------------------------------------------
    # ledger posting helpers (spec/02 entry types; errata K-6 balanced form)
    # ------------------------------------------------------------------

    def _require_psp_license(self) -> None:
        """COMP-08 PSP-side merchant-of-record / aggregation gate (fail-closed).

        Capturing a charge credits ``MERCHANT_SETTLEMENT`` — the platform takes
        custody of third-party merchant funds it must later settle.  In
        Bangladesh that aggregation/settlement of *third-party* funds is
        PSO/PSP-licence-gated (Foster Payments freeze precedent); holding
        merchant-of-record on one's own receivables pre-licence is lawful, but
        crediting the settlement obligation is not.  This mirrors the PSO
        ``license_not_active`` activation gate at
        ``bdpay/kernel/participants/service.py``.  Refusal happens BEFORE any
        ledger write, so no settlement obligation is ever booked unlicensed.
        """
        if not self._psp_license_active:
            raise ConflictError(
                "holding or settling third-party merchant funds requires a "
                "live PSP licence / merchant-of-record authorization "
                "(PSP_LICENSE_ACTIVE)",
                code="psp_license_not_active",
            )

    def _settle_charge(
        self,
        intent: PaymentIntentRecord,
        attempt: PaymentAttemptRecord,
        *,
        trigger: str = "attempt_charged",
        conn: Any | None,
    ) -> PaymentIntentRecord:
        self._require_psp_license()
        now = self._clock.now()
        accounts = kernel_accounts(intent.merchant_id, method=intent.method)
        amount = Money(attempt.amount_minor)
        fee_minor, fee_rule_id = self._resolve_capture_fee(intent, attempt, at=now)
        # MONEY-04: the connector already returned SUCCESS, so money has moved on
        # the rail. If the capture posts raise, the bare exception used to unwind
        # with NO journal entry AND no discrepancy record — money on the rail with
        # no ledger trace and nothing to reconcile (silent loss). Wrap both posts:
        # on any post failure, durably record a discrepancy event (on an
        # INDEPENDENT path — conn=None, so it is not rolled back by the same
        # transaction the failed ledger post may have aborted) carrying a
        # redaction-safe proof money moved on the rail, then re-raise. The
        # intent-advance + payment_intent.succeeded emit below never run, so a
        # capture that did not post can never read as a completed payment
        # (fail-closed).
        try:
            self._ledger.post_journal_entry(
                JournalEntrySpec(
                    reference_id=intent.payment_intent_id,
                    reference_type="PAYMENT",
                    entry_type="payment_captured",
                    description="payment captured",
                    produced_by=PRODUCER,
                    idempotency_key=make_id(
                        "je", {"attempt_id": attempt.attempt_id, "entry_type": "payment_captured"}
                    ),
                    postings=(
                        PostingSpec(accounts["in_transit"], "DEBIT", amount.amount_minor),
                        PostingSpec(
                            accounts["merchant_settlement"], "CREDIT", amount.amount_minor
                        ),
                    ),
                ),
                clock=self._clock,
                conn=conn,
            )
            if fee_minor > 0:
                self._ledger.post_journal_entry(
                    JournalEntrySpec(
                        reference_id=intent.payment_intent_id,
                        reference_type="FEE",
                        entry_type="fee_collected",
                        description=f"MDR fee collected at capture ({fee_rule_id})",
                        produced_by=PRODUCER,
                        idempotency_key=make_id(
                            "je",
                            {"attempt_id": attempt.attempt_id, "entry_type": "fee_collected"},
                        ),
                        postings=(
                            PostingSpec(accounts["merchant_settlement"], "DEBIT", fee_minor),
                            PostingSpec(accounts["mdr_income"], "CREDIT", fee_minor),
                        ),
                    ),
                    clock=self._clock,
                    conn=conn,
                )
        except Exception as exc:
            self._record_ledger_post_failure(intent, attempt, exc)
            raise
        intent = self._advance_intent(intent, trigger, succeeded_at=now, conn=conn)
        self._emit_attempt("payment_attempt.charged", intent, attempt, conn=conn)
        self._emit(
            "payment_intent.succeeded",
            intent,
            {
                "payment_intent_id": intent.payment_intent_id,
                "merchant_id": intent.merchant_id,
                "amount_minor": attempt.amount_minor,
                "method": intent.method,
                "fee_minor": fee_minor,
                "fee_rule_id": fee_rule_id,
                "succeeded_at": now,
            },
            conn=conn,
        )
        return intent

    def _open_authorization_hold(
        self,
        intent: PaymentIntentRecord,
        attempt: PaymentAttemptRecord,
        *,
        conn: Any | None,
    ) -> str:
        accounts = kernel_accounts(intent.merchant_id, method=intent.method)
        return self._ledger.open_hold(
            accounts["merchant_settlement"],
            attempt.amount_minor,
            "PAYMENT_AUTHORIZATION",
            intent.payment_intent_id,
            source_account_id=accounts["in_transit"],
            hold_reserve_account_id=accounts["merchant_hold_reserve"],
            hold_expires_at=attempt.auth_expires_at,
            clock=self._clock,
            conn=conn,
        )

    def _settle_authorized_charge(
        self,
        intent: PaymentIntentRecord,
        attempt: PaymentAttemptRecord,
        *,
        trigger: str,
        conn: Any | None,
    ) -> PaymentIntentRecord:
        self._require_psp_license()
        now = self._clock.now()
        accounts = kernel_accounts(intent.merchant_id, method=intent.method)
        fee_minor, fee_rule_id = self._resolve_capture_fee(intent, attempt, at=now)
        hold_id = self._ledger.get_open_hold_id(intent.payment_intent_id, conn=conn)
        if hold_id is None:
            raise ConflictError(
                "authorized intent has no open ledger hold",
                code="authorization_hold_missing",
            )
        try:
            self._ledger.release_hold(
                hold_id,
                "CAPTURED",
                amount_minor=attempt.amount_minor,
                clock=self._clock,
                conn=conn,
            )
            if fee_minor > 0:
                self._ledger.post_journal_entry(
                    JournalEntrySpec(
                        reference_id=intent.payment_intent_id,
                        reference_type="FEE",
                        entry_type="fee_collected",
                        description=f"MDR fee collected at capture ({fee_rule_id})",
                        produced_by=PRODUCER,
                        idempotency_key=make_id(
                            "je",
                            {"attempt_id": attempt.attempt_id, "entry_type": "fee_collected"},
                        ),
                        postings=(
                            PostingSpec(accounts["merchant_settlement"], "DEBIT", fee_minor),
                            PostingSpec(accounts["mdr_income"], "CREDIT", fee_minor),
                        ),
                    ),
                    clock=self._clock,
                    conn=conn,
                )
        except Exception as exc:
            self._record_ledger_post_failure(intent, attempt, exc)
            raise
        intent = self._advance_intent(intent, trigger, succeeded_at=now, conn=conn)
        self._emit_attempt("payment_attempt.charged", intent, attempt, conn=conn)
        self._emit(
            "payment_intent.succeeded",
            intent,
            {
                "payment_intent_id": intent.payment_intent_id,
                "merchant_id": intent.merchant_id,
                "amount_minor": attempt.amount_minor,
                "method": intent.method,
                "fee_minor": fee_minor,
                "fee_rule_id": fee_rule_id,
                "succeeded_at": now,
            },
            conn=conn,
        )
        return intent

    def _record_ledger_post_failure(
        self,
        intent: PaymentIntentRecord,
        attempt: PaymentAttemptRecord,
        exc: BaseException,
    ) -> None:
        """MONEY-04: durably record a capture that succeeded on the rail but
        failed to post to the ledger, so the discrepancy is never silently lost.

        Emitted on an INDEPENDENT path (``conn=None``): the discrepancy must
        survive even if the ledger-post failure aborted the caller's
        transaction. The event must durably PROVE money moved on the rail so
        reconciliation can locate and repair the missing journal entry. The raw
        ``rail_transaction_id`` cannot do that: ``_emit`` redacts every string
        payload value, and a real NPSB RRN (12-digit numeric) matches the PII
        account pattern and is redacted away — the proof would be erased. So the
        proof is carried in two REDACTION-SAFE forms: an explicit
        ``money_moved=True`` boolean marker, and ``rail_ref_proof`` — a chunked
        content-addressed digest binding the rail ref to the intent that
        survives ``redact()`` by construction (see :func:`rail_ref_proof`).
        Recording is best-effort fail-closed: it must never mask the original
        ledger failure, which is always re-raised by the caller.
        """
        try:
            self._emit(
                "payment_intent.ledger_post_failed",
                intent,
                {
                    "payment_intent_id": intent.payment_intent_id,
                    "attempt_id": attempt.attempt_id,
                    "merchant_id": intent.merchant_id,
                    "amount_minor": attempt.amount_minor,
                    "method": intent.method,
                    # Proof money moved on the rail, in redaction-safe form: a
                    # raw RRN would be redacted to nothing by _emit (see above).
                    "money_moved": True,
                    "rail_ref_proof": rail_ref_proof(
                        attempt.rail_transaction_id or "",
                        intent.payment_intent_id,
                    ),
                    "ledger_error": f"{type(exc).__name__}: {exc}",
                },
                subject_type="PaymentAttempt",
                subject_id=attempt.attempt_id,
                conn=None,
            )
        except Exception:  # noqa: BLE001 - never let recording mask the real fault
            self._log.exception(
                "MONEY-04: failed to record ledger-post-failure discrepancy for intent %s",
                intent.payment_intent_id,
            )

    def _resolve_capture_fee(
        self,
        intent: PaymentIntentRecord,
        attempt: PaymentAttemptRecord,
        *,
        at: datetime,
    ) -> tuple[int, str]:
        """Resolve the tiered fee rule for this capture (spec/16 LR-3).

        Resolution happens exactly once, at the succeed transition; the
        resolved ``fee_rule_id`` travels on the ``payment_intent.succeeded``
        event so settlement records which rule priced the instruction.
        ``resolve_fee_rule`` fails closed (FeeRuleError) when no rule matches;
        the seeded wildcard rows guarantee coverage for every mapped method.
        """
        profile = (
            self._merchant_profiles.fee_profile(intent.merchant_id)
            if self._merchant_profiles is not None
            else None
        )
        rule = resolve_fee_rule(
            self._fee_rules,
            merchant_id=intent.merchant_id,
            method=fee_method_for(intent.method),
            mcc=profile.mcc if profile is not None else None,
            at=at,
            merchant_class=profile.merchant_class if profile is not None else None,
            instrument_class=derive_instrument_class(intent.method, dict(intent.metadata)),
        )
        # banker's rounding applied exactly once, inside Money.multiply (E17)
        return compute_fee(attempt.amount_minor, rule)

    def _post_reversal_entries(
        self,
        intent: PaymentIntentRecord,
        attempt: PaymentAttemptRecord,
        *,
        conn: Any | None,
    ) -> None:
        # The reversal offsets the SAME rail account the capture debited
        # (intent.method is the original rail), so the contra entry never lands
        # in a different transit/clearing bucket.
        accounts = kernel_accounts(intent.merchant_id, method=intent.method)
        self._ledger.post_journal_entry(
            JournalEntrySpec(
                reference_id=intent.payment_intent_id,
                reference_type="REVERSAL",
                entry_type="payment_reversed",
                description="post-charge reversal",
                produced_by=PRODUCER,
                idempotency_key=make_id(
                    "je", {"attempt_id": attempt.attempt_id, "entry_type": "payment_reversed"}
                ),
                postings=(
                    PostingSpec(
                        accounts["merchant_settlement"], "DEBIT", attempt.amount_minor
                    ),
                    PostingSpec(accounts["in_transit"], "CREDIT", attempt.amount_minor),
                ),
            ),
            clock=self._clock,
            conn=conn,
        )

    def _post_refund_entries(
        self, intent: PaymentIntentRecord, refund: RefundRecord, *, conn: Any | None
    ) -> None:
        # Refund is a new offsetting flow on the SAME rail as the original
        # capture (intent.method), crediting the rail transit/clearing account
        # the capture debited.
        accounts = kernel_accounts(intent.merchant_id, method=intent.method)
        self._ledger.post_journal_entry(
            JournalEntrySpec(
                reference_id=refund.refund_id,
                reference_type="REFUND",
                entry_type="refund_settled",
                description="refund settled (new offsetting flow)",
                produced_by=PRODUCER,
                idempotency_key=make_id(
                    "je", {"refund_id": refund.refund_id, "entry_type": "refund_settled"}
                ),
                postings=(
                    PostingSpec(
                        accounts["merchant_settlement"], "DEBIT", refund.amount_minor
                    ),
                    PostingSpec(accounts["in_transit"], "CREDIT", refund.amount_minor),
                ),
            ),
            clock=self._clock,
            conn=conn,
        )

    # ------------------------------------------------------------------
    # shared internals
    # ------------------------------------------------------------------

    def _require_intent(
        self, payment_intent_id: str, *, conn: Any | None
    ) -> PaymentIntentRecord:
        intent = self._store.get_intent(payment_intent_id, conn=conn)
        if intent is None:
            raise NotFoundError(f"unknown payment intent {payment_intent_id}")
        return intent

    def _latest_attempt(
        self, intent: PaymentIntentRecord, *, conn: Any | None
    ) -> PaymentAttemptRecord | None:
        attempts = self._store.list_attempts(intent.payment_intent_id, conn=conn)
        return attempts[-1] if attempts else None

    def _charged_attempt(
        self, intent: PaymentIntentRecord, *, conn: Any | None
    ) -> PaymentAttemptRecord | None:
        for attempt in reversed(self._store.list_attempts(intent.payment_intent_id, conn=conn)):
            if attempt.status == "CHARGED":
                return attempt
        return None

    def _advance_intent(
        self,
        intent: PaymentIntentRecord,
        trigger: str,
        *,
        guard: str | None = None,
        conn: Any | None = None,
        defer_audit: bool = False,
        **stamps: datetime | None,
    ) -> PaymentIntentRecord:
        rule = INTENT_TABLE.resolve(intent.status, trigger, guard=guard)
        updated = replace(
            intent, status=rule.to_state, updated_at=self._clock.now(), **stamps
        )
        self._store.update_intent(updated, conn=conn, expected_status=intent.status)
        if not defer_audit:
            self._audit_transition(
                updated, from_state=intent.status, to_state=rule.to_state, conn=conn
            )
        return updated

    def _advance_attempt(
        self,
        attempt: PaymentAttemptRecord,
        trigger: str,
        *,
        guard: str | None = None,
        conn: Any | None = None,
        **stamps: datetime | None,
    ) -> PaymentAttemptRecord:
        rule = ATTEMPT_TABLE.resolve(attempt.status, trigger, guard=guard)
        updated = replace(
            attempt, status=rule.to_state, updated_at=self._clock.now(), **stamps
        )
        self._store.update_attempt(updated, conn=conn, expected_status=attempt.status)
        self._audit.append(
            AuditEventSpec(
                event_type="PAYMENT_STATE_TRANSITION",
                actor_id=PRODUCER,
                subject_type="PaymentAttempt",
                subject_id=updated.attempt_id,
                from_state=attempt.status,
                to_state=rule.to_state,
                payload={"trigger": trigger},
            ),
            clock=self._clock,
            conn=conn,
        )
        return updated

    def _advance_refund(
        self, refund: RefundRecord, trigger: str, *, conn: Any | None = None
    ) -> RefundRecord:
        rule = REFUND_TABLE.resolve(refund.status, trigger)
        updated = replace(refund, status=rule.to_state, updated_at=self._clock.now())
        self._store.update_refund(updated, conn=conn, expected_status=refund.status)
        self._audit.append(
            AuditEventSpec(
                event_type="PAYMENT_STATE_TRANSITION",
                actor_id=PRODUCER,
                subject_type="Refund",
                subject_id=updated.refund_id,
                from_state=refund.status,
                to_state=rule.to_state,
                payload={"trigger": trigger},
            ),
            clock=self._clock,
            conn=conn,
        )
        return updated

    def _audit_transition(
        self,
        intent: PaymentIntentRecord,
        *,
        from_state: str | None,
        to_state: str,
        event_type: str = "PAYMENT_STATE_TRANSITION",
        conn: Any | None,
    ) -> None:
        self._audit.append(
            AuditEventSpec(
                event_type=event_type,
                actor_id=PRODUCER,
                subject_type="PaymentIntent",
                subject_id=intent.payment_intent_id,
                from_state=from_state,
                to_state=to_state,
                payload={"merchant_id": intent.merchant_id},
            ),
            clock=self._clock,
            conn=conn,
        )

    def _emit(
        self,
        event_type: str,
        intent: PaymentIntentRecord,
        payload: dict[str, object],
        *,
        subject_type: str = "PaymentIntent",
        subject_id: str | None = None,
        topic: str = "payment.events",
        conn: Any | None,
    ) -> None:
        # Redact PII at write, but EXEMPT structural identifier/reference keys:
        # their opaque content-addressed values (pi_/mrch_/RRNs) are not PII and
        # redact() would mangle any with a 10+ digit run as an NID/PAN, corrupting
        # downstream linkage (e.g. the AML monitor's payment_intent_id / subject).
        clean = {
            k: (
                redact(v)
                if isinstance(v, str) and not is_opaque_identifier_key(k)
                else v
            )
            for k, v in payload.items()
        }
        event = build_event(
            event_type=event_type,
            subject_type=subject_type,
            subject_id=subject_id or intent.payment_intent_id,
            producer=PRODUCER,
            topic=topic,
            payload=clean,
            occurred_at=self._clock.now(),
        )
        self._outbox.enqueue(event, conn=conn)

    # ------------------------------------------------------------------

    def _emit_attempt(
        self,
        event_type: str,
        intent: PaymentIntentRecord,
        attempt: PaymentAttemptRecord,
        *,
        conn: Any | None,
    ) -> None:
        # The AML transaction monitor (spec/06) consumes ``payment_attempt.charged``
        # and re-derives its subject from the PAYLOAD: a present ``customer_id``
        # selects the Customer subject (else it falls back to Merchant), and the
        # mule fan-in/fan-out rule partitions by ``direction`` over
        # ``sender_ref``/``beneficiary_ref``. A charge is OUTBOUND from the customer
        # (payer) to the merchant (payee). Omitting these mis-subjects every charge
        # to the Merchant and starves the per-customer rules, so they are carried
        # here — mirroring the PaymentInstruction sender/beneficiary convention in
        # ``_dispatch``.
        self._emit(
            event_type,
            intent,
            {
                "payment_intent_id": intent.payment_intent_id,
                "attempt_id": attempt.attempt_id,
                "merchant_id": intent.merchant_id,
                "customer_id": intent.customer_id,
                "amount_minor": attempt.amount_minor,
                "currency": attempt.currency,
                "connector_id": attempt.connector_id,
                "rail_transaction_id": attempt.rail_transaction_id,
                "method": intent.method,
                "direction": "OUTBOUND",
                "sender_ref": intent.customer_id,
                "beneficiary_ref": intent.merchant_id,
            },
            subject_type="PaymentAttempt",
            subject_id=attempt.attempt_id,
            conn=conn,
        )

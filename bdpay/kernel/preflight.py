"""Pre-flight pipeline — refusal-first checks BEFORE any ledger write (spec/02).

Binding order (spec/02 §Pre-flight pipeline; arch cut-last #3/#5):

1. :class:`LimitEnforcer` — merchant activation gates, eKYC-tier caps from
   :mod:`bdpay.platform.config` (simplified per-txn BDT 2.5 lakh / monthly
   BDT 5 lakh / wallet cap BDT 4 lakh), per-method caps and velocity caps
   from spec/02, plus active compliance subject freezes.
2. ``SanctionsPort.check_sync`` — ALWAYS synchronous (ATA 2009 freeze
   obligation); an unavailable screener is a refusal, not a pass.
3. ``AmlPort.preflight`` — synchronous hold for high-risk subjects; async
   tag otherwise.
4. Authentication requirement resolution — 3DS / OTP / redirect flows
   produce ``REQUIRES_ACTION`` with an action token.

Any failure denies BEFORE any ledger write with the spec/00 §4 error
envelope code. The pipeline itself writes nothing; the orchestrator owns the
state transition + audit + event for each refusal.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from bdpay.platform.canonical import sha256_canonical
from bdpay.platform.clock import Clock
from bdpay.platform.config import Settings, get_settings
from bdpay.platform.errors import (
    AmlBlockError,
    LimitExceededError,
    SanctionsBlockError,
)
from bdpay.platform.interfaces import AmlPort, SanctionsPort
from bdpay.platform.money import Money

__all__ = [
    "ActionRequirement",
    "BANGLA_QR_STATIC_PER_TXN_MINOR",
    "LimitContext",
    "LimitDataPort",
    "LimitEnforcer",
    "MerchantFeeProfile",
    "MerchantFeeProfilePort",
    "MerchantGatePort",
    "MerchantGates",
    "NPSB_LIMITS",
    "PreflightPipeline",
    "PreflightResult",
    "SubjectControlReadPort",
    "resolve_authentication",
]

# ---------------------------------------------------------------------------
# Regulatory constants (integer paisa; spec/02 regulatory mapping)
# ---------------------------------------------------------------------------

#: NPSB caps per PSD Circular No. 12 (research 02 §1): per-txn / daily amount
#: / daily count, keyed by customer class.
NPSB_LIMITS: dict[str, dict[str, int]] = {
    "INDIVIDUAL": {
        "per_txn_minor": 30_000_000,  # BDT 3 lakh
        "daily_minor": 100_000_000,  # BDT 10 lakh
        "daily_count": 10,
    },
    "CORPORATE": {
        "per_txn_minor": 50_000_000,  # BDT 5 lakh
        "daily_minor": 250_000_000,  # BDT 25 lakh
        "daily_count": 20,
    },
}

#: Static Bangla QR per-txn limit BDT 20,200 (research 02 §5).
BANGLA_QR_STATIC_PER_TXN_MINOR = 2_020_000

#: Methods that resolve to a customer-redirect authentication step (spec/02
#: §3DS / OTP CHECK: bKash/Nagad/Rocket are redirect-based).
_REDIRECT_METHODS = frozenset({"BKASH", "NAGAD", "ROCKET"})


# ---------------------------------------------------------------------------
# Read-side ports the enforcer depends on
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MerchantGates:
    """The spec/08 activation gates re-checked on EVERY payment initiation."""

    merchant_id: str
    kyb_active: bool
    payout_blocked: bool
    sanctions_cleared: bool
    agreement_signed: bool
    mdr_basis_points: int = 0


@runtime_checkable
class MerchantGatePort(Protocol):
    """Reads the merchant activation gates (spec/08 §Activation gates)."""

    def gates(self, merchant_id: str) -> MerchantGates | None: ...


@dataclass(frozen=True)
class MerchantFeeProfile:
    """The Merchant-profile inputs to capture-time fee resolution (spec/16 LR-3).

    ``merchant_class`` is the regulated rate tier (``merchants.merchant_class``,
    migration 0091: MICRO | PERSONAL_RETAIL | STANDARD); ``None`` means
    unclassified, which resolves only wildcard fee rules (the 115 bps Bangla QR
    ceiling — the conservative fallback, never a discounted tier).
    ``mcc`` is the merchant's primary business MCC for the mcc-specific
    fee-rule levels; ``None`` matches mcc-wildcard rules only.
    """

    merchant_class: str | None = None
    mcc: str | None = None


@runtime_checkable
class MerchantFeeProfilePort(Protocol):
    """Reads the fee-resolution profile for a merchant (spec/16 LR-3)."""

    def fee_profile(self, merchant_id: str) -> MerchantFeeProfile | None: ...


@dataclass(frozen=True)
class KycTierInfo:
    """Active eKYC standing for a customer (spec/09 ``kyc_records``)."""

    tier: str  # SIMPLIFIED | REGULAR
    is_corporate: bool = False


@runtime_checkable
class LimitDataPort(Protocol):
    """Read-side data the LimitEnforcer needs; implementations read
    ``kyc_records`` / ``accounts`` / ``kyc_monthly_totals``."""

    def kyc_tier(self, customer_id: str) -> KycTierInfo | None: ...

    def wallet_balance_minor(self, customer_id: str) -> int: ...

    def month_total_minor(self, customer_id: str, year_month: str) -> int: ...

    def succeeded_count_24h(self, customer_id: str, method: str, now: datetime) -> int: ...

    def succeeded_amount_24h_minor(
        self, customer_id: str, method: str, now: datetime
    ) -> int: ...


@runtime_checkable
class SubjectControlReadPort(Protocol):
    """Read active compliance controls before permitting money movement."""

    def is_frozen(self, subject_type: str, subject_id: str) -> bool: ...


@dataclass(frozen=True)
class LimitContext:
    """Everything the LimitEnforcer evaluates for one intent."""

    merchant_id: str
    customer_id: str | None
    amount_minor: int
    method: str
    #: Whether this payment credits the customer wallet (cash-in style flows);
    #: the BDT 4 lakh balance cap applies only then.
    credits_customer_wallet: bool = False


# ---------------------------------------------------------------------------
# LimitEnforcer
# ---------------------------------------------------------------------------


class LimitEnforcer:
    """Refusal-first limit checks (spec/02 pre-flight a; spec/09 tier caps).

    Check order is deterministic and pinned by tests:
    merchant gates -> active compliance subject freezes -> tier per-txn ->
    tier monthly -> wallet balance cap -> method per-txn cap -> daily amount
    velocity -> daily count velocity.
    Boundary semantics: AT the cap passes, OVER the cap refuses
    (``amount > cap`` refuses).
    """

    def __init__(
        self,
        *,
        merchants: MerchantGatePort,
        data: LimitDataPort,
        settings: Settings | None = None,
        subject_controls: SubjectControlReadPort | None = None,
    ) -> None:
        self._merchants = merchants
        self._data = data
        self._settings = settings if settings is not None else get_settings()
        self._subject_controls = subject_controls

    def check(self, ctx: LimitContext, *, clock: Clock) -> None:
        """Raise :class:`LimitExceededError` (422) on the first failed check."""
        self._check_merchant_gates(ctx.merchant_id)
        self._check_subject_controls(ctx)
        if ctx.customer_id is not None:
            self._check_tier_limits(ctx, clock)
        self._check_method_caps(ctx, clock)

    def _check_subject_controls(self, ctx: LimitContext) -> None:
        if self._subject_controls is None:
            return
        if self._subject_controls.is_frozen("Merchant", ctx.merchant_id):
            raise SanctionsBlockError(
                "merchant is frozen by a compliance control; payment refused",
                code="subject_frozen",
            )
        if ctx.customer_id is not None and self._subject_controls.is_frozen(
            "Customer", ctx.customer_id
        ):
            raise SanctionsBlockError(
                "customer is frozen by a compliance control; payment refused",
                code="subject_frozen",
            )

    # -- merchant gates (spec/08, re-checked every initiation) ----------------

    def _check_merchant_gates(self, merchant_id: str) -> None:
        gates = self._merchants.gates(merchant_id)
        if gates is None or not gates.kyb_active:
            raise LimitExceededError(
                "merchant is not ACTIVE; payment initiation refused",
                code="merchant_not_active",
            )
        if gates.payout_blocked:
            raise LimitExceededError(
                "merchant is blocked; payment initiation refused",
                code="merchant_blocked",
            )
        if not gates.sanctions_cleared:
            raise LimitExceededError(
                "merchant sanctions clearance missing; payment initiation refused",
                code="merchant_not_active",
            )
        if not gates.agreement_signed:
            raise LimitExceededError(
                "merchant agreement not signed; payment initiation refused",
                code="merchant_not_active",
            )

    # -- eKYC tier limits (spec/09 via platform config) ------------------------

    def _check_tier_limits(self, ctx: LimitContext, clock: Clock) -> None:
        assert ctx.customer_id is not None
        tier = self._data.kyc_tier(ctx.customer_id)
        if tier is None:
            raise LimitExceededError(
                "customer has no ACTIVE eKYC record; payment refused",
                code="kyc_record_missing",
            )
        limits = self._settings.ekyc_simplified
        if tier.tier == "SIMPLIFIED":
            if ctx.amount_minor > limits.per_txn_minor:
                raise LimitExceededError(
                    "amount exceeds the simplified eKYC per-transaction cap",
                    code="kyc_tier_limit_exceeded",
                )
            year_month = clock.now().strftime("%Y-%m")
            month_total = self._data.month_total_minor(ctx.customer_id, year_month)
            if month_total + ctx.amount_minor > limits.monthly_minor:
                raise LimitExceededError(
                    "amount exceeds the simplified eKYC monthly cap",
                    code="kyc_tier_monthly_limit_exceeded",
                )
        if ctx.credits_customer_wallet:
            balance = self._data.wallet_balance_minor(ctx.customer_id)
            if balance + ctx.amount_minor > limits.wallet_balance_cap_minor:
                raise LimitExceededError(
                    "credit would exceed the e-wallet balance cap",
                    code="wallet_balance_cap_exceeded",
                )

    # -- per-method + velocity caps (spec/02) -----------------------------------

    def _check_method_caps(self, ctx: LimitContext, clock: Clock) -> None:
        if ctx.method == "BANGLA_QR" and ctx.amount_minor > BANGLA_QR_STATIC_PER_TXN_MINOR:
            raise LimitExceededError(
                "amount exceeds the static Bangla QR per-transaction limit",
                code="method_txn_limit_exceeded",
            )
        if ctx.method != "NPSB_IBFT" or ctx.customer_id is None:
            return
        tier = self._data.kyc_tier(ctx.customer_id)
        klass = "CORPORATE" if tier is not None and tier.is_corporate else "INDIVIDUAL"
        caps = NPSB_LIMITS[klass]
        if ctx.amount_minor > caps["per_txn_minor"]:
            raise LimitExceededError(
                "amount exceeds the NPSB per-transaction limit",
                code="method_txn_limit_exceeded",
            )
        now = clock.now()
        amount_24h = self._data.succeeded_amount_24h_minor(ctx.customer_id, ctx.method, now)
        if amount_24h + ctx.amount_minor > caps["daily_minor"]:
            raise LimitExceededError(
                "amount exceeds the NPSB daily amount limit",
                code="velocity_limit_exceeded",
            )
        count_24h = self._data.succeeded_count_24h(ctx.customer_id, ctx.method, now)
        if count_24h + 1 > caps["daily_count"]:
            raise LimitExceededError(
                "NPSB daily transaction count limit reached",
                code="velocity_limit_exceeded",
            )


# ---------------------------------------------------------------------------
# Authentication requirement resolution (pipeline step 4)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ActionRequirement:
    """A customer action gate: 3DS / OTP / wallet redirect (REQUIRES_ACTION)."""

    action_type: str  # "redirect_to_url" | "otp"
    action_token: str


def resolve_authentication(
    *,
    intent_id: str,
    method: str,
    payment_method_details: dict[str, str] | None,
    expires_at: datetime,
) -> ActionRequirement | None:
    """spec/02 §3DS / OTP CHECK — returns the action gate or None.

    The action token is content-addressed (deterministic for replay): it
    binds the intent, its expiry, and the purpose; the gateway layer wraps it
    in its short-lived session transport.
    """
    details = payment_method_details or {}
    needs_redirect = method in _REDIRECT_METHODS
    needs_3ds = method == "CARD" and details.get("requires_3ds", "true") != "false"
    needs_otp = details.get("otp_required") == "true"
    if not (needs_redirect or needs_3ds or needs_otp):
        return None
    token = "act_" + sha256_canonical(
        {"intent_id": intent_id, "expires_at": expires_at, "purpose": "customer_action"}
    )[:24]
    action_type = "otp" if (needs_otp and not needs_redirect and not needs_3ds) else (
        "redirect_to_url"
    )
    return ActionRequirement(action_type=action_type, action_token=token)


# ---------------------------------------------------------------------------
# The pipeline
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PreflightResult:
    """Pipeline verdict when nothing refused."""

    action: ActionRequirement | None
    aml_deferred_async: bool


class PreflightPipeline:
    """Limits -> sanctions -> AML -> authentication, in that binding order."""

    def __init__(
        self,
        *,
        limits: LimitEnforcer,
        sanctions: SanctionsPort,
        aml: AmlPort,
    ) -> None:
        self._limits = limits
        self._sanctions = sanctions
        self._aml = aml

    def run(
        self,
        *,
        intent_id: str,
        merchant_id: str,
        customer_id: str | None,
        amount_minor: int,
        method: str,
        payment_method_details: dict[str, str] | None,
        expires_at: datetime,
        credits_customer_wallet: bool = False,
        clock: Clock,
    ) -> PreflightResult:
        """Run all four steps; raises the typed refusal of the FIRST failure.

        Nothing has been written when a refusal is raised — the caller owns
        the FAILED transition, audit row, and event.
        """
        # 1. limits (refusal-first; fails closed)
        self._limits.check(
            LimitContext(
                merchant_id=merchant_id,
                customer_id=customer_id,
                amount_minor=amount_minor,
                method=method,
                credits_customer_wallet=credits_customer_wallet,
            ),
            clock=clock,
        )
        # 2. sanctions — ALWAYS synchronous; unavailability is a refusal
        try:
            screen = self._sanctions.check_sync(
                customer_id=customer_id, merchant_id=merchant_id
            )
        except Exception as exc:
            raise SanctionsBlockError(
                "sanctions screening unavailable; payment refused (fails closed)",
                code="sanctions_screen_unavailable",
            ) from exc
        if screen.hit:
            raise SanctionsBlockError(
                "sanctions screening returned a hit; payment refused",
                code="sanctions_screen_hit",
            )
        # 3. AML pre-flight — sync hold for high-risk, async tag otherwise
        verdict = self._aml.preflight(
            intent_id=intent_id,
            customer_id=customer_id,
            merchant_id=merchant_id,
            amount=Money(amount_minor),
            method=method,
            clock=clock,
        )
        if not verdict.allow:
            raise AmlBlockError(
                "AML pre-flight hold; payment refused",
                code=verdict.reason or "aml_preflight_hold",
            )
        # 4. authentication requirement resolution
        action = resolve_authentication(
            intent_id=intent_id,
            method=method,
            payment_method_details=payment_method_details,
            expires_at=expires_at,
        )
        return PreflightResult(action=action, aml_deferred_async=verdict.defer_async)

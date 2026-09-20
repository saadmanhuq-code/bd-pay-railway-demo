"""Fee computation (spec/04 §Fee Computation) — integer paisa, banker's rounding once.

All percentage math goes through ``bdpay.platform.money.Money.multiply``
(Decimal arithmetic, ROUND_HALF_EVEN applied exactly once, E17). Fees are
computed at capture time by the kernel; this module is the binding rule
resolution + arithmetic both the kernel and the settlement engine share.

Binding vector (spec/04): Bangla QR static-limit amount BDT 20,200
(2,020,000 paisa) at the regulated 1.15% = 23,230 paisa exactly.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from bdpay.ledger.errors import FeeRuleError
from bdpay.ledger.ids_ext import make_ledger_id
from bdpay.platform.money import Money

__all__ = [
    "FEE_METHODS",
    "FEE_TYPES",
    "MERCHANT_CLASSES",
    "INSTRUMENT_CLASSES",
    "FeeRule",
    "compute_fee",
    "default_fee_rules",
    "resolve_fee_rule",
    "tiered_fee_rules",
]

FEE_TYPES: frozenset[str] = frozenset({"PERCENTAGE", "FLAT_PAISA", "FLAT_PLUS_PERCENTAGE"})

#: Valid merchant_class values (spec/16 LR-3; NULL = wildcard in the DB, omitted here).
MERCHANT_CLASSES: frozenset[str] = frozenset({"MICRO", "PERSONAL_RETAIL", "STANDARD"})

#: Valid instrument_class values (spec/16 LR-3; NULL = wildcard).
INSTRUMENT_CLASSES: frozenset[str] = frozenset({"DEBIT_PREPAID", "CREDIT", "MFS_PSP_WALLET"})

FEE_METHODS: frozenset[str] = frozenset(
    {
        "BANGLA_QR",
        "NPSB_IBFT",
        "BEFTN_CREDIT",
        "RTGS_GROSS",
        "BKASH",
        "NAGAD",
        "ROCKET",
        "CARD_DOMESTIC",
        "CARD_INTERNATIONAL",
        "CARD_AMEX",
    }
)

_BPS_DENOMINATOR = Decimal(10_000)


@dataclass(frozen=True, slots=True)
class FeeRule:
    """fee_rules row (spec/04 DDL + spec/16 LR-3 tiering columns).

    ``rate_bps`` is basis points (115 = 1.15%).

    ``merchant_class`` and ``instrument_class`` are the spec/16 tiering dimensions
    added by migration 0091.  ``None`` means wildcard — the row matches any value
    for that dimension (preserving all pre-LR-3 rules unchanged).
    """

    rule_id: str
    rule_name: str
    merchant_id: str | None
    method: str
    mcc: str | None
    fee_type: str
    rate_bps: int | None
    flat_amount_minor: int | None
    regulated: bool
    valid_from: datetime
    valid_to: datetime | None
    created_by: str
    approved_by: str | None
    created_at: datetime
    schema_version: int = 1
    # spec/16 LR-3 additive tiering columns (None = wildcard)
    merchant_class: str | None = None
    instrument_class: str | None = None

    def __post_init__(self) -> None:
        if self.method not in FEE_METHODS:
            raise FeeRuleError(f"unknown fee method {self.method!r}")
        if self.fee_type not in FEE_TYPES:
            raise FeeRuleError(f"unknown fee_type {self.fee_type!r}")
        if self.fee_type in ("PERCENTAGE", "FLAT_PLUS_PERCENTAGE"):
            if self.rate_bps is None or not (0 <= self.rate_bps <= 10_000):
                raise FeeRuleError(f"rate_bps must be in [0, 10000], got {self.rate_bps!r}")
        if self.fee_type in ("FLAT_PAISA", "FLAT_PLUS_PERCENTAGE"):
            if self.flat_amount_minor is None or self.flat_amount_minor < 0:
                raise FeeRuleError(
                    f"flat_amount_minor must be >= 0, got {self.flat_amount_minor!r}"
                )
        if self.merchant_class is not None and self.merchant_class not in MERCHANT_CLASSES:
            raise FeeRuleError(
                f"merchant_class must be one of {sorted(MERCHANT_CLASSES)} or None, "
                f"got {self.merchant_class!r}"
            )
        if self.instrument_class is not None and self.instrument_class not in INSTRUMENT_CLASSES:
            raise FeeRuleError(
                f"instrument_class must be one of {sorted(INSTRUMENT_CLASSES)} or None, "
                f"got {self.instrument_class!r}"
            )

    def is_active(self, at: datetime) -> bool:
        if at < self.valid_from:
            return False
        return self.valid_to is None or at < self.valid_to


def resolve_fee_rule(
    rules: list[FeeRule],
    *,
    merchant_id: str,
    method: str,
    mcc: str | None,
    at: datetime,
    merchant_class: str | None = None,
    instrument_class: str | None = None,
) -> FeeRule:
    """Resolve the most-specific applicable FeeRule.

    Precedence order (spec/04 existing levels + spec/16 LR-3 tiering extension).
    Most-specific wins; ``compute_fee`` is NOT modified.

    Levels (highest to lowest priority):
      1. (merchant_id, method, mcc)
      2. (method, mcc, merchant_class, instrument_class)   — spec/16 new
      3. (method, merchant_class, instrument_class)        — spec/16 new
      4. (method, merchant_class)                          — spec/16 new
      5. (method, mcc)
      6. (method)

    NULL on ``merchant_class`` or ``instrument_class`` in a :class:`FeeRule` is a
    wildcard (matches any value), preserving every pre-LR-3 row's behaviour.
    The caller's ``merchant_class`` / ``instrument_class`` keyword args are the
    runtime values from the Merchant profile and payment attempt; passing ``None``
    means "not known" and only wildcard rules will match at the tiering levels.

    No applicable rule fails closed (raises :class:`~bdpay.ledger.errors.FeeRuleError`).
    """

    def _mc_match(rule: FeeRule) -> bool:
        """True if this rule's merchant_class matches the runtime value (None = wildcard)."""
        return rule.merchant_class is None or rule.merchant_class == merchant_class

    def _ic_match(rule: FeeRule) -> bool:
        """True if this rule's instrument_class matches the runtime value (None = wildcard)."""
        return rule.instrument_class is None or rule.instrument_class == instrument_class

    active = [r for r in rules if r.method == method and r.is_active(at)]

    # Level 1: merchant-specific + MCC (existing spec/04, unchanged)
    # Level 1a: (merchant_id, method, mcc)
    # Level 1b: (merchant_id, method) — mcc wildcard
    # Combined into the original two predicates:
    levels = [
        lambda r: (r.merchant_id == merchant_id  # noqa: E731
                   and r.mcc is not None and r.mcc == mcc),
        lambda r: (r.merchant_id == merchant_id and r.mcc is None),  # noqa: E731
        # Level 2: (method, mcc, merchant_class, instrument_class)
        lambda r: (r.merchant_id is None and r.mcc is not None and r.mcc == mcc  # noqa: E731
                   and r.merchant_class is not None and r.instrument_class is not None
                   and _mc_match(r) and _ic_match(r)),
        # Level 3: (method, merchant_class, instrument_class)
        lambda r: (r.merchant_id is None and r.mcc is None  # noqa: E731
                   and r.merchant_class is not None and r.instrument_class is not None
                   and _mc_match(r) and _ic_match(r)),
        # Level 4: (method, merchant_class) — instrument wildcard
        lambda r: (r.merchant_id is None and r.mcc is None  # noqa: E731
                   and r.merchant_class is not None and r.instrument_class is None
                   and _mc_match(r)),
        # Level 5: (method, mcc) — existing spec/04
        lambda r: (r.merchant_id is None and r.mcc is not None and r.mcc == mcc  # noqa: E731
                   and r.merchant_class is None and r.instrument_class is None),
        # Level 6: (method) — global wildcard, existing spec/04
        lambda r: (r.merchant_id is None and r.mcc is None  # noqa: E731
                   and r.merchant_class is None and r.instrument_class is None),
    ]

    for predicate in levels:
        matched = [r for r in active if predicate(r)]
        if matched:
            # Deterministic pick: most recent valid_from wins within a level.
            return max(matched, key=lambda r: r.valid_from)
    raise FeeRuleError(
        f"no applicable fee rule for method={method!r} mcc={mcc!r} "
        f"merchant_class={merchant_class!r} instrument_class={instrument_class!r}"
    )


def compute_fee(amount_minor: int, rule: FeeRule) -> tuple[int, str]:
    """Return (fee_minor, rule_id). Banker's rounding applied exactly once."""
    if isinstance(amount_minor, bool) or not isinstance(amount_minor, int) or amount_minor <= 0:
        raise FeeRuleError(f"amount_minor must be a positive int, got {amount_minor!r}")
    amount = Money(amount_minor)
    if rule.fee_type == "PERCENTAGE":
        assert rule.rate_bps is not None
        fee_minor = amount.multiply(Decimal(rule.rate_bps) / _BPS_DENOMINATOR).amount_minor
    elif rule.fee_type == "FLAT_PAISA":
        assert rule.flat_amount_minor is not None
        fee_minor = rule.flat_amount_minor
    else:  # FLAT_PLUS_PERCENTAGE
        assert rule.rate_bps is not None and rule.flat_amount_minor is not None
        pct = amount.multiply(Decimal(rule.rate_bps) / _BPS_DENOMINATOR).amount_minor
        fee_minor = rule.flat_amount_minor + pct
    # Integrity guard (spec/04): the fee may never exceed the amount.
    if not 0 <= fee_minor <= amount_minor:
        raise FeeRuleError(
            f"computed fee {fee_minor} outside [0, {amount_minor}] for rule {rule.rule_id}"
        )
    return fee_minor, rule.rule_id


def _rule(
    name: str,
    method: str,
    fee_type: str,
    rate_bps: int | None,
    flat_amount_minor: int | None,
    *,
    regulated: bool = False,
) -> FeeRule:
    valid_from = datetime(2025, 2, 1, tzinfo=UTC)
    return FeeRule(
        rule_id=make_ledger_id(
            "frule",
            {"method": method, "rate_bps": rate_bps, "flat": flat_amount_minor, "name": name},
        ),
        rule_name=name,
        merchant_id=None,
        method=method,
        mcc=None,
        fee_type=fee_type,
        rate_bps=rate_bps,
        flat_amount_minor=flat_amount_minor,
        regulated=regulated,
        valid_from=valid_from,
        valid_to=None,
        created_by="system",
        approved_by="system",
        created_at=valid_from,
    )


# PROPOSED RC-1 correction (FACT-VERIFICATION-PACK 2026-06-13 §FC-02), operator-UNVERIFIED:
# the claim is that PSD Circular No. 02/2025 (06 Feb 2025) replaced the Jan-2024 tiered
# regime with a FLAT 1.15% MDR for ALL merchants and withdrew the 0.15% NPSB platform fee.
# This is NOT yet verified (FC_02_SUPERSEDING_ID, status UNVERIFIED); these rows are a
# test/preview fixture only and are NOT seeded live (migration 0091 seeds nothing).
# valid_from below is the Circular 02/2025 *claimed* effective date.
_TIERED_VALID_FROM = datetime(2025, 2, 6, tzinfo=UTC)


def _tiered_rule(
    rule_id: str,
    name: str,
    merchant_class: str,
    instrument_class: str | None,
    rate_bps: int,
) -> FeeRule:
    return FeeRule(
        rule_id=rule_id,
        rule_name=name,
        merchant_id=None,
        method="BANGLA_QR",
        mcc=None,
        fee_type="PERCENTAGE",
        rate_bps=rate_bps,
        flat_amount_minor=None,
        regulated=True,
        valid_from=_TIERED_VALID_FROM,
        valid_to=None,
        created_by="system",
        approved_by="system",
        created_at=_TIERED_VALID_FROM,
        merchant_class=merchant_class,
        instrument_class=instrument_class,
    )


def tiered_fee_rules() -> list[FeeRule]:
    """PROPOSED Bangla QR class-tagged MDR rows — NOT a verified fact, NOT seeded live.

    These are the class-tagged rate rows the platform WOULD seed IF the
    superseding FC-02 fact is verified: the proposed correction (FACT-VERIFICATION
    -PACK 2026-06-13 §FC-02) is that BB PSD Circular No. 02/2025 (06 Feb 2025)
    superseded the Jan-2024 tiered regime (0.50% micro/debit-prepaid, 0.80%
    micro/credit+MFS-PSP, 0.70% personal-retail) with a FLAT 1.15% MDR for ALL
    merchants and withdrew the NPSB 0.15% platform fee. That claim is UNVERIFIED
    (FC_02_SUPERSEDING_ID, status UNVERIFIED) and is the operator's two-eyes call.

    This helper is a TEST/PREVIEW fixture only. Migration 0091 is schema-only and
    seeds NONE of these rows; they would land only via a future migration bound to
    a VERIFIED FC_02_SUPERSEDING_ID. Live Bangla QR fees resolve at the spec/04
    ceiling row ('frule_seed_bangla_qr_115bps_00') until then. All rows here resolve
    to 115 bps (= 1.15%) with merchant_class populated so FeeEngine resolution and
    the class dimension can be exercised in tests.
    """
    return [
        _tiered_rule(
            "frule_seed_bqr_micro_debit115",
            "Bangla QR micro — debit/prepaid (proposed flat 1.15%, unverified)",
            "MICRO",
            "DEBIT_PREPAID",
            115,
        ),
        _tiered_rule(
            "frule_seed_bqr_micro_mfspsp115",
            "Bangla QR micro — MFS/PSP wallet (proposed flat 1.15%, unverified)",
            "MICRO",
            "MFS_PSP_WALLET",
            115,
        ),
        _tiered_rule(
            "frule_seed_bqr_micro_credit115",
            "Bangla QR micro — credit (proposed flat 1.15%, unverified)",
            "MICRO",
            "CREDIT",
            115,
        ),
        _tiered_rule(
            "frule_seed_bqr_personal_retail115",
            "Bangla QR personal-retail (proposed flat 1.15%, unverified)",
            "PERSONAL_RETAIL",
            None,
            115,
        ),
    ]


def default_fee_rules() -> list[FeeRule]:
    """The binding spec/04 MDR table as platform-default fee rules."""
    return [
        _rule("Bangla QR Regulated MDR", "BANGLA_QR", "PERCENTAGE", 115, None, regulated=True),
        _rule("NPSB IBFT capped MDR", "NPSB_IBFT", "PERCENTAGE", 20, None, regulated=True),
        _rule("BEFTN credit MDR", "BEFTN_CREDIT", "PERCENTAGE", 50, None),
        _rule("RTGS gross MDR", "RTGS_GROSS", "PERCENTAGE", 25, None),
        _rule("bKash MDR", "BKASH", "PERCENTAGE", 175, None),
        _rule("Nagad MDR", "NAGAD", "PERCENTAGE", 175, None),
        _rule("Rocket MDR", "ROCKET", "PERCENTAGE", 175, None),
        _rule("Card domestic MDR", "CARD_DOMESTIC", "PERCENTAGE", 250, None),
        _rule("Card international MDR", "CARD_INTERNATIONAL", "PERCENTAGE", 350, None),
        _rule("Card Amex MDR", "CARD_AMEX", "PERCENTAGE", 350, None),
    ]

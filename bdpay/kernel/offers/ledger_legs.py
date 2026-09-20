"""Offer-engine ledger legs (spec/18 §Ledger entry types introduced).

Binding accounting rule (spec/18 §Data model, ledger section): the rail
moves NET money only. The discount itself is NEVER a ledger movement — it is
a price adjustment fixed before ``amount_minor`` exists. The only money legs
this module builds are the four registry additions:

| entry_type                  | Debits                          | Credits                         |
|-----------------------------|---------------------------------|---------------------------------|
| offer_commission_collected  | merchant_settlement:{merchant}  | offer_commission_income         |
| offer_commission_reversed   | offer_commission_income         | merchant_settlement:{merchant}  |
| offer_subsidy_granted       | offer_marketing_expense         | merchant_settlement:{merchant}  |
| offer_subsidy_reversed      | merchant_settlement:{merchant}  | offer_marketing_expense         |

``commission_minor`` / ``subsidy_minor`` are computed ONCE at reservation
(floor division), stored on the redemption row, and posted from the stored
value — no recomputation drift (spec/18 binding).
"""

from __future__ import annotations

from bdpay.kernel.offers.models import OfferRedemptionRecord
from bdpay.kernel.orchestrator import kernel_accounts
from bdpay.ledger.account_ids import (
    OFFER_COMMISSION_PURPOSE_TAG,
    OFFER_SUBSIDY_PURPOSE_TAG,
    system_account_id,
)
from bdpay.platform.ids import make_id
from bdpay.platform.interfaces import JournalEntrySpec, PostingSpec

__all__ = [
    "PRODUCER",
    "commission_collected_spec",
    "commission_minor_for",
    "commission_reversed_spec",
    "offer_accounts",
    "subsidy_granted_spec",
    "subsidy_minor_for",
    "subsidy_reversed_spec",
]

PRODUCER = "offer-engine@1"


def commission_minor_for(net_amount_minor: int, commission_bps: int | None) -> int:
    """``net * commission_bps // 10_000`` (floor), 0 when unconfigured."""
    if commission_bps is None or commission_bps == 0:
        return 0
    return net_amount_minor * commission_bps // 10_000


def subsidy_minor_for(discount_minor: int, subsidy_bps: int | None) -> int:
    """``discount * subsidy_bps // 10_000`` (floor), 0 when unconfigured."""
    if subsidy_bps is None or subsidy_bps == 0:
        return 0
    return discount_minor * subsidy_bps // 10_000


def offer_accounts(merchant_id: str) -> dict[str, str]:
    """Deterministic account ids the offer engine posts against.

    ``merchant_settlement`` reuses the kernel's content-addressed id (same
    payload via :func:`bdpay.kernel.orchestrator.kernel_accounts`) so the
    commission leg debits the SAME settlement account the capture credited.
    """
    accounts = kernel_accounts(merchant_id)
    return {
        "merchant_settlement": accounts["merchant_settlement"],
        # MONEY-02: resolve through the shared resolver so these match the
        # OFFER_* system accounts bootstrap_chart_of_accounts creates.
        "offer_commission_income": system_account_id(
            "OFFER_COMMISSION_INCOME", OFFER_COMMISSION_PURPOSE_TAG
        ),
        "offer_marketing_expense": system_account_id(
            "OFFER_MARKETING_EXPENSE", OFFER_SUBSIDY_PURPOSE_TAG
        ),
    }


def _entry(
    redemption: OfferRedemptionRecord,
    *,
    entry_type: str,
    description: str,
    debit_account: str,
    credit_account: str,
    amount_minor: int,
) -> JournalEntrySpec:
    return JournalEntrySpec(
        reference_id=redemption.redemption_id,
        reference_type="FEE",
        entry_type=entry_type,
        description=description,
        produced_by=PRODUCER,
        idempotency_key=make_id(
            "je", {"redemption_id": redemption.redemption_id, "entry_type": entry_type}
        ),
        postings=(
            PostingSpec(debit_account, "DEBIT", amount_minor),
            PostingSpec(credit_account, "CREDIT", amount_minor),
        ),
    )


def commission_collected_spec(redemption: OfferRedemptionRecord) -> JournalEntrySpec:
    """Deal-commission leg, identical txn pattern to ``fee_collected``."""
    accounts = offer_accounts(redemption.merchant_id)
    return _entry(
        redemption,
        entry_type="offer_commission_collected",
        description="offer deal commission collected",
        debit_account=accounts["merchant_settlement"],
        credit_account=accounts["offer_commission_income"],
        amount_minor=redemption.commission_minor,
    )


def commission_reversed_spec(redemption: OfferRedemptionRecord) -> JournalEntrySpec:
    """Offsetting commission leg on ``APPLIED -> REVERSED``."""
    accounts = offer_accounts(redemption.merchant_id)
    return _entry(
        redemption,
        entry_type="offer_commission_reversed",
        description="offer deal commission reversed",
        debit_account=accounts["offer_commission_income"],
        credit_account=accounts["merchant_settlement"],
        amount_minor=redemption.commission_minor,
    )


def subsidy_granted_spec(redemption: OfferRedemptionRecord) -> JournalEntrySpec:
    """Platform co-funded subsidy leg (operator-only config, ApprovalRequest-gated)."""
    accounts = offer_accounts(redemption.merchant_id)
    return _entry(
        redemption,
        entry_type="offer_subsidy_granted",
        description="offer launch subsidy granted",
        debit_account=accounts["offer_marketing_expense"],
        credit_account=accounts["merchant_settlement"],
        amount_minor=redemption.subsidy_minor,
    )


def subsidy_reversed_spec(redemption: OfferRedemptionRecord) -> JournalEntrySpec:
    """Offsetting subsidy leg on redemption reversal where subsidy was granted."""
    accounts = offer_accounts(redemption.merchant_id)
    return _entry(
        redemption,
        entry_type="offer_subsidy_reversed",
        description="offer launch subsidy reversed",
        debit_account=accounts["merchant_settlement"],
        credit_account=accounts["offer_marketing_expense"],
        amount_minor=redemption.subsidy_minor,
    )

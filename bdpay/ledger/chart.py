"""Chart-of-accounts bootstrap (spec/03 canonical instantiation guide).

``bootstrap_chart_of_accounts`` creates the system accounts exactly as the
spec/03 table lists them (E11: system-account uniqueness is on
(subtype, purpose_tag) — the spec's own bootstrap table has three IN_TRANSIT
system accounts). Per-merchant and per-customer account helpers follow the
same spec section. Idempotent: an account that already exists (same
content-addressed id) is returned, not re-created.
"""

from __future__ import annotations

from typing import Any

from bdpay.ledger.errors import LedgerValidationError
from bdpay.ledger.service import LedgerService
from bdpay.ledger.types import AccountSpec

__all__ = [
    "SYSTEM_ACCOUNTS",
    "bootstrap_chart_of_accounts",
    "open_customer_accounts",
    "open_merchant_accounts",
]

#: (account_subtype, account_type, purpose_tag) — spec/03 bootstrap table.
SYSTEM_ACCOUNTS: tuple[tuple[str, str, str], ...] = (
    ("SPONSOR_BANK_TCSA", "ASSET", "tcsa_main"),
    ("IN_TRANSIT", "ASSET", "npsb_transit"),
    ("IN_TRANSIT", "ASSET", "beftn_transit"),
    ("IN_TRANSIT", "ASSET", "rtgs_transit"),
    ("FEE_RECEIVABLE", "ASSET", "mdr_receivable"),
    ("SUSPENSE", "ASSET", "recon_suspense"),
    ("CONNECTOR_CLEARING", "ASSET", "bkash_clearing"),
    ("CONNECTOR_CLEARING", "ASSET", "card_clearing"),
    # MONEY rail-routing: per-MFS connector interim float for the remaining
    # wallet rails (NAGAD/ROCKET), mirroring spec/03's bkash_clearing row so
    # the kernel's rail-aware transit leg (orchestrator _TRANSIT_ACCOUNT_BY_METHOD)
    # resolves to a real chart account instead of misclassifying into npsb_transit.
    ("CONNECTOR_CLEARING", "ASSET", "nagad_clearing"),
    ("CONNECTOR_CLEARING", "ASSET", "rocket_clearing"),
    ("REFUND_RESERVE", "LIABILITY", "refund_reserve_main"),
    ("MDR_INCOME", "INCOME", "mdr_earned"),
    ("SWITCHING_FEE_INCOME", "INCOME", "switching_fee_earned"),
    ("INTERCHANGE_INCOME", "INCOME", "interchange_earned"),
    ("FLOAT_INTEREST_INCOME", "INCOME", "tcsa_interest"),
    ("SPONSOR_BANK_FEE", "EXPENSE", "sponsor_bank_fee"),
    ("NETWORK_FEE", "EXPENSE", "scheme_fee"),
    ("REFUND_EXPENSE", "EXPENSE", "refund_cost"),
    ("RETAINED_EARNINGS", "EQUITY", "retained"),
    ("PAID_UP_CAPITAL", "EQUITY", "paid_up"),
    # spec/18 offer-engine system accounts (MONEY-02): the four offer ledger
    # legs (offers/ledger_legs.py) post the platform side of deal commission
    # and launch subsidy to these. purpose_tags match
    # account_ids.OFFER_*_PURPOSE_TAG so offer_accounts() resolves to these ids.
    ("OFFER_COMMISSION_INCOME", "INCOME", "offer_commission"),
    ("OFFER_MARKETING_EXPENSE", "EXPENSE", "offer_subsidy"),
)


def _create_or_get(
    service: LedgerService, spec: AccountSpec, *, conn: Any | None = None
) -> str:
    try:
        return service.create_account(spec, conn=conn)
    except LedgerValidationError as exc:
        if "already exists" not in str(exc):
            raise
        # Content-addressed id: recompute via the shared resolver (MONEY-01) and
        # return the existing account.
        from bdpay.ledger.account_ids import account_id

        return account_id(
            account_subtype=spec.account_subtype,
            purpose_tag=spec.purpose_tag,
            owner_id=spec.owner_id,
            currency=spec.currency,
        )


def bootstrap_chart_of_accounts(service: LedgerService) -> dict[tuple[str, str], str]:
    """Create all spec/03 system accounts. Returns {(subtype, purpose_tag): id}."""
    out: dict[tuple[str, str], str] = {}
    for subtype, account_type, purpose_tag in SYSTEM_ACCOUNTS:
        out[(subtype, purpose_tag)] = _create_or_get(
            service,
            AccountSpec(
                account_type=account_type,
                account_subtype=subtype,
                purpose_tag=purpose_tag,
                is_system=True,
                owner_type="SYSTEM",
            ),
        )
    return out


def open_merchant_accounts(
    service: LedgerService, merchant_id: str, *, conn: Any | None = None
) -> dict[str, str]:
    """MERCHANT_SETTLEMENT / MERCHANT_HOLD_RESERVE / MERCHANT_ROLLING_RESERVE."""
    settlement = _create_or_get(
        service,
        AccountSpec(
            account_type="LIABILITY",
            account_subtype="MERCHANT_SETTLEMENT",
            owner_id=merchant_id,
            owner_type="MERCHANT",
            purpose_tag="settlement_main",
        ),
        conn=conn,
    )
    hold = _create_or_get(
        service,
        AccountSpec(
            account_type="LIABILITY",
            account_subtype="MERCHANT_HOLD_RESERVE",
            owner_id=merchant_id,
            owner_type="MERCHANT",
            parent_id=settlement,
            purpose_tag="hold_reserve",
        ),
        conn=conn,
    )
    rolling = _create_or_get(
        service,
        AccountSpec(
            account_type="LIABILITY",
            account_subtype="MERCHANT_ROLLING_RESERVE",
            owner_id=merchant_id,
            owner_type="MERCHANT",
            parent_id=settlement,
            purpose_tag="rolling_reserve",
        ),
        conn=conn,
    )
    return {
        "MERCHANT_SETTLEMENT": settlement,
        "MERCHANT_HOLD_RESERVE": hold,
        "MERCHANT_ROLLING_RESERVE": rolling,
    }


def open_customer_accounts(service: LedgerService, customer_id: str) -> dict[str, str]:
    """CUSTOMER_FLOAT + CUSTOMER_HOLD_RESERVE (PSP mode)."""
    float_acct = _create_or_get(
        service,
        AccountSpec(
            account_type="LIABILITY",
            account_subtype="CUSTOMER_FLOAT",
            owner_id=customer_id,
            owner_type="CUSTOMER",
            purpose_tag="float_main",
        ),
    )
    hold = _create_or_get(
        service,
        AccountSpec(
            account_type="LIABILITY",
            account_subtype="CUSTOMER_HOLD_RESERVE",
            owner_id=customer_id,
            owner_type="CUSTOMER",
            parent_id=float_acct,
            purpose_tag="hold_reserve",
        ),
    )
    return {"CUSTOMER_FLOAT": float_acct, "CUSTOMER_HOLD_RESERVE": hold}

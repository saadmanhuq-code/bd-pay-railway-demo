"""Canonical account-id resolver — the single source of truth shared by the
ledger bootstrap (``chart.py`` / ``LedgerService.create_account``), the payment
kernel (``orchestrator.kernel_accounts``) and the offer engine
(``offers.ledger_legs.offer_accounts``).

MONEY-01 root cause (audit 2026-06-13): the kernel minted account ids from a
key shape ``{"subtype", "purpose"}`` while the ledger created accounts under the
DISJOINT DDL key shape ``{"owner_id", "account_subtype", "currency",
"purpose_tag"}`` (spec/03 DDL; ``service.create_account``). ``make_id`` is
content-addressed, so the two never matched and every capture raised
``LedgerAccountNotFoundError``.

The fix: ``account_id(...)`` builds the EXACT payload ``create_account`` hashes,
so every caller — bootstrap, kernel, offers — derives byte-identical ids by
construction. ``create_account`` and the chart recompute-fallback call this same
function, so routing through it is id-preserving (zero behaviour change) and the
agreement is guaranteed, not coincidental.

The semantic mapping for the fixed (rail-independent) legs lives here too, so
there is one place an operator can audit:

  mdr_income          -> MDR_INCOME / mdr_earned      (system; owner_id=None)
  merchant_settlement -> MERCHANT_SETTLEMENT / settlement_main (owner = merchant)
  offer_commission_income -> OFFER_COMMISSION_INCOME / offer_commission (system)
  offer_marketing_expense -> OFFER_MARKETING_EXPENSE / offer_subsidy (system)

RAIL-AWARE TRANSIT (MONEY rail-routing, operator-ratified): the capture's gross
leg ("in_transit") is rail-specific, not a single generic account. The chart
(spec/03 §"Chart of accounts") enumerates three IN_TRANSIT rails
(npsb/beftn/rtgs_transit) and per-MFS / card CONNECTOR_CLEARING floats
(bkash/nagad/rocket/card_clearing). The kernel owns the
``payment_method`` -> (subtype, purpose_tag) map (``orchestrator``
``_TRANSIT_ACCOUNT_BY_METHOD`` / ``transit_account_for``) because rail
semantics are a kernel concern; it resolves the concrete id through
``system_account_id`` below, so this module stays the single *id* resolver.
"""

from __future__ import annotations

from bdpay.platform.ids import make_id

__all__ = [
    "MDR_INCOME_PURPOSE_TAG",
    "MERCHANT_HOLD_RESERVE_PURPOSE_TAG",
    "MERCHANT_SETTLEMENT_PURPOSE_TAG",
    "OFFER_COMMISSION_PURPOSE_TAG",
    "OFFER_SUBSIDY_PURPOSE_TAG",
    "account_id",
    "merchant_hold_reserve_id",
    "merchant_settlement_id",
    "system_account_id",
]

MDR_INCOME_PURPOSE_TAG = "mdr_earned"
MERCHANT_SETTLEMENT_PURPOSE_TAG = "settlement_main"
MERCHANT_HOLD_RESERVE_PURPOSE_TAG = "hold_reserve"
OFFER_COMMISSION_PURPOSE_TAG = "offer_commission"
OFFER_SUBSIDY_PURPOSE_TAG = "offer_subsidy"


def account_id(
    *,
    account_subtype: str,
    purpose_tag: str | None,
    owner_id: str | None = None,
    currency: str = "BDT",
) -> str:
    """Content-addressed account id — IDENTICAL payload to ``create_account``.

    ``create_account`` (service.py) hashes exactly
    ``{"owner_id", "account_subtype", "currency", "purpose_tag"}`` (the spec/03
    DDL natural key); ``canonical_json`` sorts keys, so order is irrelevant but
    the key NAMES and the inclusion of ``owner_id``/``currency`` are load-bearing.
    """
    return make_id(
        "acct",
        {
            "owner_id": owner_id,
            "account_subtype": account_subtype,
            "currency": currency,
            "purpose_tag": purpose_tag,
        },
    )


def system_account_id(account_subtype: str, purpose_tag: str) -> str:
    """Id of a system (owner_id=None) chart account, e.g. IN_TRANSIT/npsb_transit."""
    return account_id(account_subtype=account_subtype, purpose_tag=purpose_tag)


def merchant_settlement_id(merchant_id: str) -> str:
    """Id of a merchant's MERCHANT_SETTLEMENT/settlement_main liability account."""
    return account_id(
        account_subtype="MERCHANT_SETTLEMENT",
        purpose_tag=MERCHANT_SETTLEMENT_PURPOSE_TAG,
        owner_id=merchant_id,
    )


def merchant_hold_reserve_id(merchant_id: str) -> str:
    """Id of a merchant's MERCHANT_HOLD_RESERVE/hold_reserve liability account."""
    return account_id(
        account_subtype="MERCHANT_HOLD_RESERVE",
        purpose_tag=MERCHANT_HOLD_RESERVE_PURPOSE_TAG,
        owner_id=merchant_id,
    )

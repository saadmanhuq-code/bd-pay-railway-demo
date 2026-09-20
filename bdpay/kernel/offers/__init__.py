"""offer-engine — spec/18 dining-wedge offers (kernel logical sub-service).

BUILD AUTHORIZED (principal directive 2026-06-12): simulator-first build;
G2/G3 are LAUNCH gates. G4 naming grants honored: sub-service name
``offer-engine``, ID prefixes ``off``/``ored``/``octr``, topic
``offer.events``. Decisions D1 (authenticated diner login required to
redeem) and D2 (PERCENT_OFF-only pilot; BOGO schema-reserved,
build-refused) are binding throughout this package.

Package layout:

- :mod:`ids_ext` — format-parity shim for the ``off``/``ored``/``octr`` prefixes.
- :mod:`copy_bn` — bilingual ReasonCode copy table (mojibake-pinned).
- :mod:`eligibility` — pure, deterministic, reason-coded evaluator.
- :mod:`states` — Offer + OfferRedemption refusal-first transition tables.
- :mod:`models` — frozen record dataclasses mirroring migration 0130.
- :mod:`repository` — OfferStore protocol; in-memory + Postgres stores.
- :mod:`ledger_legs` — commission/subsidy journal-entry builders.
- :mod:`service` — OfferService (merchant lifecycle, discovery, sweeps).
- :mod:`seam` — reservation inside the PaymentIntent flow (zero-offer seam).
- :mod:`consumer` — OfferRedemption FSM event consumer + reservation sweep.
"""

from __future__ import annotations

from bdpay.kernel.offers.consumer import OfferEventConsumer
from bdpay.kernel.offers.eligibility import (
    EligibilityResult,
    ReasonCode,
    evaluate_eligibility,
)
from bdpay.kernel.offers.ids_ext import OFFER_PREFIXES, make_offer_id
from bdpay.kernel.offers.models import (
    OfferCounterRecord,
    OfferRecord,
    OfferRedemptionRecord,
    OfferVersionRecord,
    OfferWindow,
)
from bdpay.kernel.offers.repository import InMemoryOfferStore, OfferStore, PostgresOfferStore
from bdpay.kernel.offers.seam import SPEC02_FSM1_AMENDMENT, OfferPaymentSeam
from bdpay.kernel.offers.service import OfferService
from bdpay.kernel.offers.states import OFFER_TABLE, REDEMPTION_TABLE

__all__ = [
    "OFFER_PREFIXES",
    "OFFER_TABLE",
    "REDEMPTION_TABLE",
    "SPEC02_FSM1_AMENDMENT",
    "EligibilityResult",
    "InMemoryOfferStore",
    "OfferCounterRecord",
    "OfferEventConsumer",
    "OfferPaymentSeam",
    "OfferRecord",
    "OfferRedemptionRecord",
    "OfferService",
    "OfferStore",
    "OfferVersionRecord",
    "OfferWindow",
    "PostgresOfferStore",
    "ReasonCode",
    "evaluate_eligibility",
    "make_offer_id",
]

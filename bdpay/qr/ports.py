"""Local ports for the qr package (constructor injection).

``bdpay.platform.interfaces`` does not exist yet on this lane; per the build
rules the qr package defines its own Protocols here and takes implementations
via the service constructor. The qr package never moves money: it creates
PaymentIntents through :class:`PaymentIntentPort` and reads merchants through
:class:`MerchantDirectory` (spec/13 NOT-owned list).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

__all__ = [
    "AuditPort",
    "EventPublisher",
    "FeeQuote",
    "FeeQuoter",
    "IntentRecord",
    "MerchantDirectory",
    "MerchantRecord",
    "PaymentIntentPort",
    "ZeroFeeQuoter",
]


@dataclass(frozen=True)
class MerchantRecord:
    """The merchant facts qr needs (owned by spec/08; read-only here)."""

    merchant_id: str  # mrch_<...>
    state: str  # ACTIVE / SUSPENDED / TERMINATED / ...
    mcc: str | None
    name: str  # ASCII/Latin (ID 59 source)
    name_bn: str | None  # Bengali (ID 64 source)
    city: str
    merchant_pan: str  # BB-format merchant identifier (template sub-ID 02)
    bangla_qr_allowed: bool = True


@dataclass(frozen=True)
class IntentRecord:
    """The PaymentIntent facts qr needs (owned by spec/02; via kernel port)."""

    payment_intent_id: str  # pi_<...>
    merchant_id: str
    state: str
    amount_minor: int
    currency: str
    method: str


@dataclass(frozen=True)
class FeeQuote:
    """Sender fee disclosure shown before execution (PSD Circular 12)."""

    fee_minor: int
    payer_pays: bool
    disclosure: str


@runtime_checkable
class MerchantDirectory(Protocol):
    def get(self, merchant_id: str) -> MerchantRecord | None: ...

    def find_by_pan(self, merchant_pan: str) -> MerchantRecord | None: ...


@runtime_checkable
class PaymentIntentPort(Protocol):
    def get(self, payment_intent_id: str) -> IntentRecord | None: ...

    def create(
        self,
        *,
        merchant_id: str,
        amount_minor: int,
        method: str,
        metadata: dict[str, str],
    ) -> IntentRecord: ...


@runtime_checkable
class AuditPort(Protocol):
    def record(
        self,
        *,
        action: str,
        subject_type: str,
        subject_id: str,
        occurred_at: datetime,
        details: dict,
    ) -> None: ...


@runtime_checkable
class EventPublisher(Protocol):
    """Outbox-backed publisher for the ``qr.events`` topic (conventions §5)."""

    def publish(
        self,
        *,
        event_type: str,
        subject_type: str,
        subject_id: str,
        occurred_at: datetime,
        payload: dict,
    ) -> None: ...


@runtime_checkable
class FeeQuoter(Protocol):
    """Display-only quote source (fee computation is spec/04 FeeEngine)."""

    def quote(self, *, qr_type: str, on_us: bool, amount_minor: int | None) -> FeeQuote: ...


class ZeroFeeQuoter:
    """Launch posture: QR merchant payments carry no sender charge."""

    def quote(self, *, qr_type: str, on_us: bool, amount_minor: int | None) -> FeeQuote:
        return FeeQuote(
            fee_minor=0,
            payer_pays=False,
            disclosure="No charge to sender for QR merchant payment",
        )

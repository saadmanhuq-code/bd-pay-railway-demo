"""offer-engine entity records (spec/18 §Data model; migration 0130).

Frozen dataclasses mirroring the spec/18 DDL columns. Money is integer paisa
(``*_minor``); timestamps are aware UTC datetimes from the injected clock;
IDs are content-addressed. Validation fails closed at construction — an
invalid record can never exist in memory. Every constraint here mirrors a
DDL CHECK in ``db/migrations/0130_offer_engine.sql``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from bdpay.kernel.offers.states import OFFER_TABLE, REDEMPTION_TABLE
from bdpay.kernel.payment_states import PAYMENT_METHODS
from bdpay.platform.errors import InvalidRequestError

__all__ = [
    "COUNTER_SCOPES",
    "MAX_WINDOW_ROWS",
    "OFFER_KINDS",
    "PERCENT_BPS_CEILING",
    "PERCENT_BPS_FLOOR",
    "VALIDITY_MAX_DAYS",
    "OfferCounterRecord",
    "OfferRecord",
    "OfferRedemptionRecord",
    "OfferVersionRecord",
    "OfferWindow",
    "validate_windows",
]

OFFER_KINDS: tuple[str, ...] = ("PERCENT_OFF", "BOGO")
COUNTER_SCOPES: tuple[str, ...] = ("day", "total", "customer")

#: spec/18: percent_bps in [500, 5000] (5%-50%); ceiling is the config
#: ``offer.max_percent_bps`` (principal-changeable, never above the DDL CHECK).
PERCENT_BPS_FLOOR = 500
PERCENT_BPS_CEILING = 5000

#: spec/18: max 7 window rows; validity range <= 366 days.
MAX_WINDOW_ROWS = 7
VALIDITY_MAX_DAYS = 366

_HHMM_RE = re.compile(r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")
_DAY_CODES = frozenset({"MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"})


def _require_aware(value: datetime, what: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise InvalidRequestError(f"{what} must be a timezone-aware datetime")
    return value


def _require_minor(value: object, what: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidRequestError(
            f"{what} must be int paisa, got {type(value).__name__}",
            code="amount_must_be_integer",
        )
    if value < minimum:
        raise InvalidRequestError(f"{what} must be >= {minimum} paisa")
    return value


def _require_cap(value: object, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidRequestError(f"{what} must be an integer", code="invalid_request")
    if value < 1:
        raise InvalidRequestError(f"{what} must be >= 1 when present", code="invalid_request")
    return value


@dataclass(frozen=True)
class OfferWindow:
    """One time-window row: Asia/Dhaka local ``HH:MM``, start incl / end excl."""

    days: tuple[str, ...]
    start_local: str
    end_local: str

    def __post_init__(self) -> None:
        if not self.days:
            raise InvalidRequestError("window days must be non-empty", code="invalid_window")
        if len(set(self.days)) != len(self.days):
            raise InvalidRequestError("window days must not repeat", code="invalid_window")
        for day in self.days:
            if day not in _DAY_CODES:
                raise InvalidRequestError(
                    f"unknown window day {day!r}; use MON..SUN", code="invalid_window"
                )
        for name in ("start_local", "end_local"):
            value = getattr(self, name)
            if not isinstance(value, str) or not _HHMM_RE.fullmatch(value):
                raise InvalidRequestError(
                    f"{name} must be HH:MM (Asia/Dhaka local)", code="invalid_window"
                )
        if not self.start_local < self.end_local:
            raise InvalidRequestError(
                "window start_local must be before end_local", code="invalid_window"
            )

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> OfferWindow:
        if not isinstance(payload, Mapping):
            raise InvalidRequestError("window rows must be objects", code="invalid_window")
        days = payload.get("days")
        if not isinstance(days, list | tuple):
            raise InvalidRequestError("window days must be a list", code="invalid_window")
        return cls(
            days=tuple(str(d) for d in days),
            start_local=str(payload.get("start_local", "")),
            end_local=str(payload.get("end_local", "")),
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "days": list(self.days),
            "start_local": self.start_local,
            "end_local": self.end_local,
        }


def validate_windows(windows: tuple[OfferWindow, ...]) -> tuple[OfferWindow, ...]:
    """spec/18 create-time window rules: 1..7 rows, no same-day overlap.

    Errata S18-E8: at least one window row is required (an empty window set
    would make the offer silently never-eligible — refused at create instead).
    Overlap rule: two windows sharing a day whose half-open ranges
    ``[start, end)`` intersect are rejected (``overlapping_windows``).
    """
    if not windows:
        raise InvalidRequestError(
            "at least one time window is required", code="windows_required"
        )
    if len(windows) > MAX_WINDOW_ROWS:
        raise InvalidRequestError(
            f"at most {MAX_WINDOW_ROWS} window rows are allowed", code="invalid_window"
        )
    for i, first in enumerate(windows):
        for second in windows[i + 1 :]:
            shared = set(first.days) & set(second.days)
            if not shared:
                continue
            if first.start_local < second.end_local and second.start_local < first.end_local:
                raise InvalidRequestError(
                    "windows overlap on the same day", code="overlapping_windows"
                )
    return windows


@dataclass(frozen=True)
class OfferRecord:
    """One ``offers`` row (current version; spec/18 DDL + migration 0130)."""

    offer_id: str
    merchant_id: str
    kind: str
    title: str
    title_bn: str
    windows: tuple[OfferWindow, ...]
    valid_from: datetime
    valid_until: datetime
    allowed_methods: tuple[str, ...]
    created_idempotency_key: str
    created_at: datetime
    updated_at: datetime
    version: int = 1
    percent_bps: int | None = None
    bogo_config: Mapping[str, object] | None = None
    min_spend_minor: int = 0
    max_discount_minor: int | None = None
    cap_per_day: int | None = None
    cap_total: int | None = None
    cap_per_customer: int | None = None
    cap_recredit_on_refund: bool = True
    commission_bps: int | None = None
    subsidy_bps: int | None = None
    state: str = "DRAFT"
    currency: str = "BDT"

    def __post_init__(self) -> None:
        if not self.offer_id or not self.merchant_id:
            raise InvalidRequestError("offer_id and merchant_id are required")
        if self.kind not in OFFER_KINDS:
            raise InvalidRequestError(
                f"kind must be one of {OFFER_KINDS}", code="invalid_request"
            )
        # DDL: exactly one of percent_bps / bogo_config, matched to kind.
        if (self.kind == "PERCENT_OFF") != (self.percent_bps is not None):
            raise InvalidRequestError(
                "PERCENT_OFF requires percent_bps (and only PERCENT_OFF may carry it)",
                code="invalid_request",
            )
        if (self.kind == "BOGO") != (self.bogo_config is not None):
            raise InvalidRequestError(
                "BOGO requires bogo_config (and only BOGO may carry it)",
                code="invalid_request",
            )
        if self.percent_bps is not None and not (
            PERCENT_BPS_FLOOR <= self.percent_bps <= PERCENT_BPS_CEILING
        ):
            raise InvalidRequestError(
                f"percent_bps must be within [{PERCENT_BPS_FLOOR}, {PERCENT_BPS_CEILING}]",
                code="percent_bps_out_of_range",
            )
        if not self.title or not self.title_bn:
            raise InvalidRequestError(
                "title and title_bn are both required", code="invalid_request"
            )
        object.__setattr__(self, "windows", validate_windows(tuple(self.windows)))
        _require_aware(self.valid_from, "valid_from")
        _require_aware(self.valid_until, "valid_until")
        if not self.valid_from < self.valid_until:
            raise InvalidRequestError(
                "valid_from must be before valid_until", code="invalid_validity_range"
            )
        if self.valid_until - self.valid_from > timedelta(days=VALIDITY_MAX_DAYS):
            raise InvalidRequestError(
                f"validity range is capped at {VALIDITY_MAX_DAYS} days",
                code="invalid_validity_range",
            )
        _require_minor(self.min_spend_minor, "min_spend_minor")
        if self.max_discount_minor is not None:
            _require_minor(self.max_discount_minor, "max_discount_minor", minimum=1)
        for name in ("cap_per_day", "cap_total", "cap_per_customer"):
            value = getattr(self, name)
            if value is not None:
                _require_cap(value, name)
        if self.cap_per_day is None and self.cap_total is None:
            # The circuit breaker is not optional: uncapped offers do not exist.
            raise InvalidRequestError(
                "at least one of cap_per_day / cap_total is required",
                code="cap_required",
            )
        if (
            self.cap_per_day is not None
            and self.cap_total is not None
            and self.cap_per_day > self.cap_total
        ):
            raise InvalidRequestError(
                "cap_per_day must be <= cap_total", code="invalid_request"
            )
        if not self.allowed_methods:
            raise InvalidRequestError(
                "allowed_methods must be non-empty", code="invalid_request"
            )
        for method in self.allowed_methods:
            if method not in PAYMENT_METHODS:
                raise InvalidRequestError(
                    f"unknown payment method {method!r}", code="method_unsupported"
                )
        if self.commission_bps is not None and not 0 <= self.commission_bps <= 1000:
            raise InvalidRequestError(
                "commission_bps must be within [0, 1000]", code="invalid_request"
            )
        if self.subsidy_bps is not None and not 0 <= self.subsidy_bps <= 10000:
            raise InvalidRequestError(
                "subsidy_bps must be within [0, 10000]", code="invalid_request"
            )
        if self.state not in OFFER_TABLE.states:
            raise InvalidRequestError(f"unknown offer state {self.state!r}")
        if self.currency != "BDT":
            raise InvalidRequestError("currency must be BDT in v1", code="currency_unsupported")
        if isinstance(self.version, bool) or not isinstance(self.version, int) or self.version < 1:
            raise InvalidRequestError("version must be an integer >= 1")
        if not self.created_idempotency_key:
            raise InvalidRequestError(
                "created_idempotency_key is required", code="idempotency_key_required"
            )
        for name in ("created_at", "updated_at"):
            _require_aware(getattr(self, name), name)


@dataclass(frozen=True)
class OfferVersionRecord:
    """One immutable economics snapshot (errata S18-E5: edit-by-versioning).

    Economic fields are edit-by-versioning only; in-flight reservations pin
    the version they reserved against and read economics from this row.
    """

    offer_id: str
    version: int
    windows: tuple[OfferWindow, ...]
    valid_from: datetime
    valid_until: datetime
    allowed_methods: tuple[str, ...]
    created_at: datetime
    percent_bps: int | None = None
    bogo_config: Mapping[str, object] | None = None
    min_spend_minor: int = 0
    max_discount_minor: int | None = None
    cap_per_day: int | None = None
    cap_total: int | None = None
    cap_per_customer: int | None = None
    commission_bps: int | None = None
    subsidy_bps: int | None = None

    def __post_init__(self) -> None:
        if not self.offer_id:
            raise InvalidRequestError("offer_id is required")
        if isinstance(self.version, bool) or not isinstance(self.version, int) or self.version < 1:
            raise InvalidRequestError("version must be an integer >= 1")
        object.__setattr__(self, "windows", tuple(self.windows))
        _require_aware(self.valid_from, "valid_from")
        _require_aware(self.valid_until, "valid_until")
        _require_aware(self.created_at, "created_at")

    @classmethod
    def from_offer(cls, offer: OfferRecord, *, created_at: datetime) -> OfferVersionRecord:
        return cls(
            offer_id=offer.offer_id,
            version=offer.version,
            windows=offer.windows,
            valid_from=offer.valid_from,
            valid_until=offer.valid_until,
            allowed_methods=offer.allowed_methods,
            created_at=created_at,
            percent_bps=offer.percent_bps,
            bogo_config=offer.bogo_config,
            min_spend_minor=offer.min_spend_minor,
            max_discount_minor=offer.max_discount_minor,
            cap_per_day=offer.cap_per_day,
            cap_total=offer.cap_total,
            cap_per_customer=offer.cap_per_customer,
            commission_bps=offer.commission_bps,
            subsidy_bps=offer.subsidy_bps,
        )


@dataclass(frozen=True)
class OfferRedemptionRecord:
    """One ``offer_redemptions`` row (spec/18 DDL + errata S18-E10).

    ``refunded_minor`` is the additive accumulation column behind the
    ``intent_reversed_or_refunded_full`` trigger: partial refunds accumulate
    here without transitioning the redemption; reaching ``net_amount_minor``
    is a full refund.
    """

    redemption_id: str
    offer_id: str
    offer_version: int
    payment_intent_id: str
    merchant_id: str
    customer_id: str  # D1: login required; never nullable
    gross_amount_minor: int
    discount_minor: int
    net_amount_minor: int
    reserved_at: datetime
    reserved_expires_at: datetime
    commission_minor: int = 0
    subsidy_minor: int = 0
    refunded_minor: int = 0
    currency: str = "BDT"
    state: str = "RESERVED"
    resolved_at: datetime | None = None
    metadata: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("redemption_id", "offer_id", "payment_intent_id", "merchant_id"):
            if not getattr(self, name):
                raise InvalidRequestError(f"{name} is required")
        if not self.customer_id:
            # Decision D1: guests cannot redeem; the row is structurally
            # impossible without an authenticated customer.
            raise InvalidRequestError(
                "offer redemption requires an authenticated customer (D1)",
                code="offer_requires_login",
            )
        _require_minor(self.gross_amount_minor, "gross_amount_minor", minimum=1)
        _require_minor(self.discount_minor, "discount_minor")
        if self.discount_minor >= self.gross_amount_minor:
            raise InvalidRequestError(
                "discount must be strictly less than gross", code="discount_equals_gross"
            )
        if self.net_amount_minor != self.gross_amount_minor - self.discount_minor:
            raise InvalidRequestError(
                "net_amount_minor must equal gross - discount", code="invalid_request"
            )
        _require_minor(self.commission_minor, "commission_minor")
        _require_minor(self.subsidy_minor, "subsidy_minor")
        _require_minor(self.refunded_minor, "refunded_minor")
        if self.currency != "BDT":
            raise InvalidRequestError("currency must be BDT in v1", code="currency_unsupported")
        if self.state not in REDEMPTION_TABLE.states:
            raise InvalidRequestError(f"unknown redemption state {self.state!r}")
        if (
            isinstance(self.offer_version, bool)
            or not isinstance(self.offer_version, int)
            or self.offer_version < 1
        ):
            raise InvalidRequestError("offer_version must be an integer >= 1")
        _require_aware(self.reserved_at, "reserved_at")
        _require_aware(self.reserved_expires_at, "reserved_expires_at")
        if self.resolved_at is not None:
            _require_aware(self.resolved_at, "resolved_at")


@dataclass(frozen=True)
class OfferCounterRecord:
    """One ``offer_counters`` row: atomic fail-closed cap slot accounting."""

    counter_id: str
    offer_id: str
    scope: str
    scope_key: str
    cap_limit: int
    used: int
    updated_at: datetime

    def __post_init__(self) -> None:
        if not self.counter_id or not self.offer_id:
            raise InvalidRequestError("counter_id and offer_id are required")
        if self.scope not in COUNTER_SCOPES:
            raise InvalidRequestError(
                f"scope must be one of {COUNTER_SCOPES}", code="invalid_request"
            )
        if not self.scope_key:
            raise InvalidRequestError("scope_key is required")
        _require_cap(self.cap_limit, "cap_limit")
        if isinstance(self.used, bool) or not isinstance(self.used, int):
            raise InvalidRequestError("used must be an integer")
        if not 0 <= self.used <= self.cap_limit:
            raise InvalidRequestError(
                "counter invariant violated: 0 <= used <= cap_limit",
                code="invalid_request",
            )
        _require_aware(self.updated_at, "updated_at")

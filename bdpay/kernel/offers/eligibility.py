"""Pure eligibility evaluation (spec/18 §Eligibility evaluation algorithm).

No I/O, no clock reads — the caller injects ``now_utc`` (spec/00 §7
injectable clock) and pre-loaded rows. Deterministic and property-testable.

Binding rules implemented here:

- Evaluation order 1-6 with ALL failing reasons returned (not just the first).
- Asia/Dhaka window match at fixed UTC+06:00 (no DST table); boundary rule
  ``start_local`` inclusive, ``end_local`` exclusive.
- Discount computation integer-only: ``gross * percent_bps // 10_000``
  (floor — rounds DOWN in the customer's disfavor by at most 1 paisa),
  then capped by ``max_discount_minor`` when set.
- Invariant ``0 <= discount_minor < gross_amount_minor``; a 100% outcome is
  rejected (``discount_equals_gross``) — the rail must always move >= 1 paisa.
- Counter reads here are ADVISORY fast-fail only; the authoritative check is
  the atomic reservation (spec/18 §Cap enforcement) — NEVER trusted for
  enforcement.
- D2 (principal, 2026-06-12): PERCENT_OFF-only pilot. ``BOGO`` is
  schema-reserved but build-refused: evaluating a BOGO offer raises
  ``invalid_request/bogo_not_yet_enabled`` (errata S18-E7) — deterministic,
  fail-closed, and unreachable through the create path (which already
  rejects BOGO).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum

from bdpay.kernel.offers.copy_bn import REASON_COPY
from bdpay.platform.errors import InvalidRequestError

__all__ = [
    "DHAKA_FIXED_TZ",
    "WEEKDAY_CODES",
    "CounterReading",
    "EligibilityResult",
    "ReasonCode",
    "compute_percent_discount",
    "dhaka_date_str",
    "evaluate_eligibility",
    "window_matches",
]

#: Fixed UTC+06:00 (spec/18: Asia/Dhaka has no DST; no tz database needed).
DHAKA_FIXED_TZ = timezone(timedelta(hours=6), name="Asia/Dhaka")

#: ``datetime.weekday()`` index -> spec/18 window day code.
WEEKDAY_CODES: tuple[str, ...] = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")


class ReasonCode(StrEnum):
    """spec/18 §Eligibility ReasonCode enum — closed set, bilingual copy."""

    OFFER_NOT_ACTIVE = "OFFER_NOT_ACTIVE"
    OUTSIDE_WINDOW = "OUTSIDE_WINDOW"
    BEFORE_VALIDITY = "BEFORE_VALIDITY"
    AFTER_VALIDITY = "AFTER_VALIDITY"
    MIN_SPEND_NOT_MET = "MIN_SPEND_NOT_MET"
    METHOD_NOT_ALLOWED = "METHOD_NOT_ALLOWED"
    CAP_DAY_EXHAUSTED = "CAP_DAY_EXHAUSTED"
    CAP_TOTAL_EXHAUSTED = "CAP_TOTAL_EXHAUSTED"
    CAP_CUSTOMER_EXHAUSTED = "CAP_CUSTOMER_EXHAUSTED"
    MERCHANT_NOT_ACTIVE = "MERCHANT_NOT_ACTIVE"
    BOGO_LINE_ITEMS_MISSING = "BOGO_LINE_ITEMS_MISSING"

    @property
    def message(self) -> str:
        return REASON_COPY[self.value][0]

    @property
    def message_bn(self) -> str:
        return REASON_COPY[self.value][1]


@dataclass(frozen=True)
class EligibilityResult:
    """spec/18 binding output shape."""

    eligible: bool
    discount_minor: int  # 0 when ineligible
    reasons: tuple[ReasonCode, ...]  # empty when eligible


@dataclass(frozen=True)
class CounterReading:
    """Advisory counter snapshot pre-loaded by the caller (never authoritative)."""

    used: int
    cap_limit: int

    @property
    def exhausted(self) -> bool:
        return self.used >= self.cap_limit


def _require_aware(value: datetime, what: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise InvalidRequestError(f"{what} must be a timezone-aware datetime")
    return value


def dhaka_date_str(now_utc: datetime) -> str:
    """The Asia/Dhaka calendar date (``YYYY-MM-DD``) for a UTC instant."""
    return _require_aware(now_utc, "now_utc").astimezone(DHAKA_FIXED_TZ).date().isoformat()


def window_matches(windows, now_utc: datetime) -> bool:
    """True when ``now_utc`` falls inside ANY window row (Dhaka local time).

    Boundary rule (binding): ``start_local`` inclusive, ``end_local`` exclusive.
    """
    local = _require_aware(now_utc, "now_utc").astimezone(DHAKA_FIXED_TZ)
    day_code = WEEKDAY_CODES[local.weekday()]
    hhmm = f"{local.hour:02d}:{local.minute:02d}"
    for window in windows:
        if day_code in window.days and window.start_local <= hhmm < window.end_local:
            return True
    return False


def compute_percent_discount(
    gross_amount_minor: int,
    percent_bps: int,
    max_discount_minor: int | None,
) -> int:
    """Integer-only PERCENT_OFF discount: floor division, then the cap.

    ``discount = gross * percent_bps // 10_000`` (floor — at most 1 paisa in
    the customer's disfavor, never overdraws the cap), then
    ``min(discount, max_discount_minor)`` when the cap is set.
    """
    if isinstance(gross_amount_minor, bool) or not isinstance(gross_amount_minor, int):
        raise InvalidRequestError(
            "gross_amount_minor must be int paisa", code="amount_must_be_integer"
        )
    if gross_amount_minor <= 0:
        raise InvalidRequestError(
            "gross_amount_minor must be > 0 paisa", code="amount_not_positive"
        )
    if isinstance(percent_bps, bool) or not isinstance(percent_bps, int):
        raise InvalidRequestError("percent_bps must be int basis points")
    discount = gross_amount_minor * percent_bps // 10_000
    if max_discount_minor is not None:
        discount = min(discount, max_discount_minor)
    if discount >= gross_amount_minor:
        # The rail must always move >= 1 paisa; full-comp flows are not payments.
        raise InvalidRequestError(
            "discount equals or exceeds the gross amount; refused",
            code="discount_equals_gross",
        )
    return discount


def evaluate_eligibility(
    offer,
    *,
    merchant_active: bool,
    now_utc: datetime,
    gross_amount_minor: int | None = None,
    method: str | None = None,
    day_counter: CounterReading | None = None,
    total_counter: CounterReading | None = None,
    customer_counter: CounterReading | None = None,
) -> EligibilityResult:
    """Run ALL spec/18 eligibility checks; return every failing reason.

    ``offer`` is any object exposing the spec/18 offer economics fields
    (``state``, ``kind``, ``valid_from``, ``valid_until``, ``windows``,
    ``min_spend_minor``, ``max_discount_minor``, ``allowed_methods``,
    ``percent_bps``) — in practice an
    :class:`~bdpay.kernel.offers.models.OfferRecord` or a pinned version view.

    ``gross_amount_minor`` / ``method`` / counters are optional so the
    PII-free discovery endpoint can evaluate without customer context
    (spec/18 §GET /v1/offers/eligible): omitted inputs skip their checks.
    """
    now = _require_aware(now_utc, "now_utc")
    reasons: list[ReasonCode] = []

    # 1. Offer state ACTIVE; merchant ACTIVE.
    if offer.state != "ACTIVE":
        reasons.append(ReasonCode.OFFER_NOT_ACTIVE)
    if not merchant_active:
        reasons.append(ReasonCode.MERCHANT_NOT_ACTIVE)

    # 2. valid_from <= now <= valid_until.
    if now < offer.valid_from:
        reasons.append(ReasonCode.BEFORE_VALIDITY)
    elif now > offer.valid_until:
        reasons.append(ReasonCode.AFTER_VALIDITY)

    # 3. Window match in Asia/Dhaka (start inclusive, end exclusive).
    if not window_matches(offer.windows, now):
        reasons.append(ReasonCode.OUTSIDE_WINDOW)

    # 4. Minimum spend (only when the caller supplied an amount).
    if gross_amount_minor is not None and gross_amount_minor < offer.min_spend_minor:
        reasons.append(ReasonCode.MIN_SPEND_NOT_MET)

    # 5. Method allowed (only when the caller supplied a method).
    if method is not None and method not in offer.allowed_methods:
        reasons.append(ReasonCode.METHOD_NOT_ALLOWED)

    # 6. Caps — advisory fast-fail only; enforcement is the atomic reservation.
    if day_counter is not None and day_counter.exhausted:
        reasons.append(ReasonCode.CAP_DAY_EXHAUSTED)
    if total_counter is not None and total_counter.exhausted:
        reasons.append(ReasonCode.CAP_TOTAL_EXHAUSTED)
    if customer_counter is not None and customer_counter.exhausted:
        reasons.append(ReasonCode.CAP_CUSTOMER_EXHAUSTED)

    if reasons:
        return EligibilityResult(eligible=False, discount_minor=0, reasons=tuple(reasons))

    # 7. Discount computation (integer-only). D2: BOGO is build-refused.
    if offer.kind == "BOGO":
        raise InvalidRequestError(
            "BOGO offers are schema-reserved and build-deferred (decision D2)",
            code="bogo_not_yet_enabled",
        )
    if gross_amount_minor is None:
        # Discovery mode: eligible, but no per-transaction discount to compute.
        return EligibilityResult(eligible=True, discount_minor=0, reasons=())
    discount = compute_percent_discount(
        gross_amount_minor, offer.percent_bps, offer.max_discount_minor
    )
    return EligibilityResult(eligible=True, discount_minor=discount, reasons=())

"""Money value object — integer paisa, Decimal arithmetic, no floats. Ever.

Spec 00 §6 (binding): storage and wire are integer paisa (1 BDT = 100 paisa,
field ``amount_minor``); currency is a sibling ``"BDT"`` field. Rounding, when
a percentage ratio (e.g. MDR) applies, is banker's rounding (ROUND_HALF_EVEN)
applied exactly once at this layer, re-expressed as integer paisa.

Amounts are non-negative by default. Signed contexts (e.g. reversal postings)
must opt in explicitly with ``allow_negative=True``; any operation that would
otherwise produce a negative amount raises :class:`MoneyError`.
"""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal, localcontext

__all__ = ["BDT", "PAISA_PER_BDT", "Money", "MoneyError"]

BDT = "BDT"
PAISA_PER_BDT = Decimal(100)

_ONE_PAISA = Decimal("1")
_TWO_DP = Decimal("0.01")
_ARITHMETIC_PRECISION = 50


class MoneyError(ValueError):
    """Raised on any invalid Money construction or arithmetic."""


def _require_int_minor(amount_minor: object) -> int:
    if isinstance(amount_minor, bool) or not isinstance(amount_minor, int):
        raise MoneyError(
            f"amount_minor must be int paisa, got {type(amount_minor).__name__} "
            "(floats are rejected everywhere)"
        )
    return amount_minor


def _require_decimal(value: object, what: str) -> Decimal:
    if isinstance(value, float):
        raise MoneyError(f"{what} must be decimal.Decimal, got float (floats are rejected)")
    if not isinstance(value, Decimal):
        raise MoneyError(f"{what} must be decimal.Decimal, got {type(value).__name__}")
    if not value.is_finite():
        raise MoneyError(f"{what} must be a finite Decimal")
    return value


class Money:
    """Immutable amount in integer paisa with a BDT currency tag."""

    __slots__ = ("_amount_minor", "_currency")

    def __init__(
        self, amount_minor: int, currency: str = BDT, *, allow_negative: bool = False
    ) -> None:
        amount = _require_int_minor(amount_minor)
        if currency != BDT:
            raise MoneyError(f"unsupported currency {currency!r}; v1 is BDT only")
        if amount < 0 and not allow_negative:
            raise MoneyError(
                "negative amount requires explicit allow_negative=True (signed contexts only)"
            )
        object.__setattr__(self, "_amount_minor", amount)
        object.__setattr__(self, "_currency", currency)

    # -- immutability -------------------------------------------------------

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("Money is immutable")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("Money is immutable")

    # -- accessors ----------------------------------------------------------

    @property
    def amount_minor(self) -> int:
        return self._amount_minor

    @property
    def currency(self) -> str:
        return self._currency

    def as_decimal_bdt(self) -> Decimal:
        """The amount as a two-decimal-place BDT Decimal (display/parsing aid)."""
        with localcontext() as ctx:
            ctx.prec = _ARITHMETIC_PRECISION
            return (Decimal(self._amount_minor) / PAISA_PER_BDT).quantize(_TWO_DP)

    # -- constructors -------------------------------------------------------

    @classmethod
    def from_minor(cls, amount_minor: int, *, allow_negative: bool = False) -> Money:
        """Construct from integer paisa."""
        return cls(amount_minor, BDT, allow_negative=allow_negative)

    @classmethod
    def from_decimal_bdt(cls, amount_bdt: Decimal, *, allow_negative: bool = False) -> Money:
        """Construct from a Decimal BDT amount. Sub-paisa precision is rejected
        (construction is exact; rounding happens only in :meth:`multiply`)."""
        amount = _require_decimal(amount_bdt, "amount_bdt")
        with localcontext() as ctx:
            ctx.prec = _ARITHMETIC_PRECISION
            minor = amount * PAISA_PER_BDT
            if minor != minor.to_integral_value():
                raise MoneyError(
                    f"amount {amount} has sub-paisa precision; amounts are integer paisa"
                )
            return cls(int(minor), BDT, allow_negative=allow_negative)

    # -- arithmetic ---------------------------------------------------------

    def _require_same_currency(self, other: Money) -> None:
        if self._currency != other._currency:
            raise MoneyError(
                f"currency mismatch: {self._currency!r} vs {other._currency!r}"
            )

    def add(self, other: Money, *, allow_negative: bool = False) -> Money:
        if not isinstance(other, Money):
            raise MoneyError(f"can only add Money to Money, got {type(other).__name__}")
        self._require_same_currency(other)
        return Money(
            self._amount_minor + other._amount_minor,
            self._currency,
            allow_negative=allow_negative,
        )

    def subtract(self, other: Money, *, allow_negative: bool = False) -> Money:
        """Subtract; raises :class:`MoneyError` on a negative result unless the
        caller explicitly passes ``allow_negative=True`` (signed contexts)."""
        if not isinstance(other, Money):
            raise MoneyError(f"can only subtract Money from Money, got {type(other).__name__}")
        self._require_same_currency(other)
        return Money(
            self._amount_minor - other._amount_minor,
            self._currency,
            allow_negative=allow_negative,
        )

    def multiply(self, ratio: Decimal, *, allow_negative: bool = False) -> Money:
        """Multiply by a Decimal ratio (e.g. MDR percentage) with banker's
        rounding (ROUND_HALF_EVEN) applied exactly once, back to integer paisa."""
        factor = _require_decimal(ratio, "ratio")
        with localcontext() as ctx:
            ctx.prec = _ARITHMETIC_PRECISION
            product = Decimal(self._amount_minor) * factor
            rounded = product.quantize(_ONE_PAISA, rounding=ROUND_HALF_EVEN)
        return Money(int(rounded), self._currency, allow_negative=allow_negative)

    # -- operators (strict non-negative defaults) ----------------------------

    def __add__(self, other: object) -> Money:
        if not isinstance(other, Money):
            return NotImplemented
        return self.add(other)

    def __sub__(self, other: object) -> Money:
        if not isinstance(other, Money):
            return NotImplemented
        return self.subtract(other)

    def __mul__(self, ratio: object) -> Money:
        if not isinstance(ratio, Decimal):
            return NotImplemented
        return self.multiply(ratio)

    def __rmul__(self, ratio: object) -> Money:
        return self.__mul__(ratio)

    # -- comparisons ---------------------------------------------------------

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Money):
            return NotImplemented
        return self._amount_minor == other._amount_minor and self._currency == other._currency

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, Money):
            return NotImplemented
        self._require_same_currency(other)
        return self._amount_minor < other._amount_minor

    def __le__(self, other: object) -> bool:
        if not isinstance(other, Money):
            return NotImplemented
        self._require_same_currency(other)
        return self._amount_minor <= other._amount_minor

    def __gt__(self, other: object) -> bool:
        if not isinstance(other, Money):
            return NotImplemented
        self._require_same_currency(other)
        return self._amount_minor > other._amount_minor

    def __ge__(self, other: object) -> bool:
        if not isinstance(other, Money):
            return NotImplemented
        self._require_same_currency(other)
        return self._amount_minor >= other._amount_minor

    def __hash__(self) -> int:
        return hash((self._amount_minor, self._currency))

    def __bool__(self) -> bool:
        return self._amount_minor != 0

    def __repr__(self) -> str:
        return f"Money(amount_minor={self._amount_minor}, currency={self._currency!r})"

    def __str__(self) -> str:
        return f"{self._currency} {self.as_decimal_bdt()}"

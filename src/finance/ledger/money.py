"""Integer minor units <-> the ledger's decimal major units.

The only place outside the calculation modules where Decimal is used. The ledger parses
JSON numbers through a float (docs/ledger-contract.md, section 4), so amounts are always
sent as JSON *strings*, with exactly the currency's number of decimal places.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

# ISO 4217 minor-unit exponents for the currencies a school may be configured with. v1
# accepts ZAR only (a school setting, not a constant in business logic); adding a
# currency means adding its exponent here.
CURRENCY_EXPONENTS: dict[str, int] = {"ZAR": 2}


class MoneyError(ValueError):
    pass


def exponent(currency: str) -> int:
    try:
        return CURRENCY_EXPONENTS[currency]
    except KeyError:
        raise MoneyError(f"unsupported currency {currency!r}") from None


def to_major(amount_minor: int, currency: str) -> str:
    """250000 ZAR cents -> "2500.00". Positive amounts only (the ledger refuses others)."""
    if isinstance(amount_minor, bool) or not isinstance(amount_minor, int):
        raise MoneyError("amounts are integer minor units")
    if amount_minor <= 0:
        raise MoneyError("ledger transfers must be positive")
    places = exponent(currency)
    quantum = Decimal(1).scaleb(-places)  # Decimal("0.01") for 2 places
    return str((Decimal(amount_minor).scaleb(-places)).quantize(quantum))


def from_major(amount: str | int | Decimal, currency: str) -> int:
    """The ledger's decimal amount -> integer minor units, exactly or not at all."""
    places = exponent(currency)
    try:
        value = Decimal(str(amount))
    except InvalidOperation:
        raise MoneyError("not a decimal amount") from None
    if not value.is_finite():
        raise MoneyError("not a finite amount")
    minor = value.scaleb(places)
    if minor != minor.to_integral_value():
        raise MoneyError(f"more than {places} decimal places")
    return int(minor)

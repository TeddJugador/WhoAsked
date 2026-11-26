"""The one place fee calculations use Decimal: half-up rounding to an increment.

Rates are integer basis points (10% = 1000), so every input is an integer and the only
rounding is here, explicit and half-up (spec 3: roundingIncrementMinor).
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

BASIS_POINTS = 10_000  # 100%


def round_half_up(numerator: int, denominator: int, increment: int = 1) -> int:
    """numerator / denominator, rounded half-up to a multiple of ``increment``."""
    if denominator <= 0 or increment <= 0:
        raise ValueError("denominator and increment must be positive")
    units = (Decimal(numerator) / (Decimal(denominator) * increment)).quantize(
        Decimal(1), rounding=ROUND_HALF_UP
    )
    return int(units) * increment


def percent_of(amount_minor: int, basis_points: int, increment: int = 1) -> int:
    """``basis_points`` of ``amount_minor``, half-up to the increment."""
    return round_half_up(amount_minor * basis_points, BASIS_POINTS, increment)

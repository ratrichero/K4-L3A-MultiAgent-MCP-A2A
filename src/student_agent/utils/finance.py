from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Any


def to_decimal(value: Any) -> Decimal:
    """Convert int, float, str to Decimal safely."""
    if isinstance(value, Decimal):
        return value
    if value is None:
        return Decimal("0.00")
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal("0.00")


def round_brl(amount: Decimal) -> Decimal:
    """Round to 2 decimal places using standard financial ROUND_HALF_UP."""
    return amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def to_float_brl(amount: Decimal) -> float:
    """Convert rounded Decimal to standard float representation for JSON."""
    return float(round_brl(amount))

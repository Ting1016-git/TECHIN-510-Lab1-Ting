from __future__ import annotations

from decimal import Decimal, InvalidOperation


def parse_decimal(value: str | float | int | None) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, (float, int)):
        return Decimal(str(value))
    s = str(value).strip()
    if not s:
        return None
    try:
        return Decimal(s)
    except (InvalidOperation, ValueError):
        return None


def compute_amount(quantity: Decimal | None, unit_price: Decimal | None) -> Decimal | None:
    if quantity is None or unit_price is None:
        return None
    return quantity * unit_price


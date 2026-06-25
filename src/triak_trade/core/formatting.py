"""Human-facing number formatting helpers."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

_DISPLAY_QUANTUM = Decimal("0.001")


def format_decimal(value: Decimal | int | float | str | None) -> str | None:
    if value is None:
        return None
    decimal_value = value if isinstance(value, Decimal) else Decimal(str(value))
    quantized = decimal_value.quantize(_DISPLAY_QUANTUM, rounding=ROUND_HALF_UP)
    normalized = format(quantized, "f")
    if "." not in normalized:
        return normalized
    return normalized.rstrip("0").rstrip(".")

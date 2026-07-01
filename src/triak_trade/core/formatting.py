"""Human-facing number formatting helpers."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

_STANDARD_DISPLAY_QUANTUM = Decimal("0.001")
_SMALL_VALUE_EXTRA_DIGITS = 3


def _coerce_decimal(value: Decimal | int | float | str) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _strip_trailing_zeros(value: str) -> str:
    if "." not in value:
        return "0" if value == "-0" else value
    normalized = value.rstrip("0").rstrip(".")
    return "0" if normalized in {"", "-0"} else normalized


def decimal_to_plain_string(value: Decimal | int | float | str | None) -> str | None:
    if value is None:
        return None
    return _strip_trailing_zeros(format(_coerce_decimal(value), "f"))


def format_decimal(value: Decimal | int | float | str | None) -> str | None:
    if value is None:
        return None
    decimal_value = _coerce_decimal(value)
    if decimal_value.is_zero():
        return "0"
    if abs(decimal_value) >= Decimal("0.1"):
        quantized = decimal_value.quantize(_STANDARD_DISPLAY_QUANTUM, rounding=ROUND_HALF_UP)
        return _strip_trailing_zeros(format(quantized, "f"))

    adjusted = abs(decimal_value).normalize().adjusted()
    decimal_places = max(0, -adjusted + _SMALL_VALUE_EXTRA_DIGITS)
    quantum = Decimal("1").scaleb(-decimal_places)
    quantized = decimal_value.quantize(quantum, rounding=ROUND_HALF_UP)
    return format(quantized, f".{decimal_places}f")

from __future__ import annotations

from decimal import Decimal

from triak_trade.core.formatting import decimal_to_plain_string, format_decimal


def test_format_decimal_keeps_three_digits_after_first_non_zero_fractional_digit() -> None:
    assert format_decimal(Decimal("0.0061234")) == "0.006123"
    assert format_decimal(Decimal("0.0061784")) == "0.006178"
    assert format_decimal(Decimal("0.5")) == "0.5"


def test_format_decimal_keeps_standard_precision_for_values_above_one() -> None:
    assert format_decimal(Decimal("12.34567")) == "12.346"
    assert format_decimal(Decimal("12.30000")) == "12.3"


def test_decimal_to_plain_string_preserves_small_value_precision_without_scientific_notation(
) -> None:
    assert decimal_to_plain_string(Decimal("0.006123400")) == "0.0061234"
    assert decimal_to_plain_string(Decimal("1200.000")) == "1200"

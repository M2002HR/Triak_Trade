from decimal import Decimal

from triak_trade.domain.enums import TradeSide
from triak_trade.risk.stop_loss import (
    clamp_stop_loss_to_risk_budget,
    max_quantity_for_stop_risk_budget,
)


def test_long_stop_is_clamped_to_five_percent_balance_loss() -> None:
    result = clamp_stop_loss_to_risk_budget(
        side=TradeSide.LONG,
        entry_price=Decimal("100"),
        stop_loss=Decimal("90"),
        quantity=Decimal("12"),
        balance_at_entry=Decimal("1000"),
        fee_rate_pct=Decimal("0"),
        max_loss_pct_of_balance=Decimal("5"),
    )

    assert result.was_capped is True
    assert result.stop_loss == Decimal("95.83333333333333333333333334")
    assert result.risk_amount <= Decimal("50")
    assert result.risk_budget == Decimal("50")


def test_short_stop_is_clamped_to_five_percent_balance_loss() -> None:
    result = clamp_stop_loss_to_risk_budget(
        side=TradeSide.SHORT,
        entry_price=Decimal("100"),
        stop_loss=Decimal("110"),
        quantity=Decimal("12"),
        balance_at_entry=Decimal("1000"),
        fee_rate_pct=Decimal("0"),
        max_loss_pct_of_balance=Decimal("5"),
    )

    assert result.was_capped is True
    assert result.stop_loss == Decimal("104.1666666666666666666666666")
    assert result.risk_amount <= Decimal("50")


def test_existing_safe_stop_is_preserved_and_fees_are_counted() -> None:
    result = clamp_stop_loss_to_risk_budget(
        side=TradeSide.LONG,
        entry_price=Decimal("100"),
        stop_loss=Decimal("99"),
        quantity=Decimal("10"),
        balance_at_entry=Decimal("1000"),
        fee_rate_pct=Decimal("0.04"),
        max_loss_pct_of_balance=Decimal("5"),
    )

    assert result.was_capped is False
    assert result.stop_loss == Decimal("99")
    assert result.risk_amount == Decimal("10.79600")


def test_max_quantity_preserves_bank_signal_stop_instead_of_moving_it() -> None:
    quantity = max_quantity_for_stop_risk_budget(
        side=TradeSide.SHORT,
        entry_price=Decimal("0.19953"),
        stop_loss=Decimal("0.209736"),
        balance_at_entry=Decimal("54.5267433"),
        fee_rate_pct=Decimal("0.04"),
        max_loss_pct_of_balance=Decimal("5"),
    )

    assert quantity < Decimal("263")
    assert quantity > Decimal("262")
    result = clamp_stop_loss_to_risk_budget(
        side=TradeSide.SHORT,
        entry_price=Decimal("0.19953"),
        stop_loss=Decimal("0.209736"),
        quantity=quantity,
        balance_at_entry=Decimal("54.5267433"),
        fee_rate_pct=Decimal("0.04"),
        max_loss_pct_of_balance=Decimal("5"),
    )
    assert result.stop_loss == Decimal("0.209736")
    assert result.was_capped is False

from __future__ import annotations

from decimal import Decimal

from triak_trade.domain.enums import TradeSide
from triak_trade.parsing.side_inference import infer_trade_side_from_price_geometry


def test_infer_long_from_targets_above_entry_range() -> None:
    result = infer_trade_side_from_price_geometry(
        entry_low=Decimal("0.5895"),
        entry_high=Decimal("0.6282"),
        stop_loss=None,
        take_profits=[
            Decimal("0.6450"),
            Decimal("0.6679"),
            Decimal("0.6859"),
            Decimal("0.7412"),
        ],
    )

    assert result.side is TradeSide.LONG
    assert result.reason == "targets_above_entry"


def test_infer_short_from_targets_below_entry_range() -> None:
    result = infer_trade_side_from_price_geometry(
        entry_low=Decimal("4183"),
        entry_high=Decimal("4187"),
        stop_loss=Decimal("4196"),
        take_profits=[
            Decimal("4180"),
            Decimal("4177"),
            Decimal("4174"),
            Decimal("4171"),
        ],
    )

    assert result.side is TradeSide.SHORT
    assert result.reason == "targets_below_entry+stop_above_entry"


def test_infer_long_from_stop_when_targets_are_missing() -> None:
    result = infer_trade_side_from_price_geometry(
        entry_low=Decimal("100"),
        entry_high=Decimal("101"),
        stop_loss=Decimal("95"),
        take_profits=[],
    )

    assert result.side is TradeSide.LONG
    assert result.reason == "stop_below_entry"


def test_returns_unknown_when_targets_conflict_with_stop() -> None:
    result = infer_trade_side_from_price_geometry(
        entry_low=Decimal("100"),
        entry_high=Decimal("101"),
        stop_loss=Decimal("95"),
        take_profits=[Decimal("98")],
    )

    assert result.side is TradeSide.UNKNOWN
    assert result.reason == "conflicting_stop_and_targets"


def test_returns_unknown_without_entry_reference() -> None:
    result = infer_trade_side_from_price_geometry(
        entry_low=None,
        entry_high=None,
        stop_loss=Decimal("95"),
        take_profits=[Decimal("110")],
    )

    assert result.side is TradeSide.UNKNOWN
    assert result.reason == "missing_entry_reference"

from __future__ import annotations

from decimal import Decimal

from triak_trade.backtesting.directives import detect_tp_list_update


def test_detect_tp_list_update_extracts_ladder() -> None:
    # msg /6285 shape: a bare row of prices tagged "Tp List".
    values = detect_tp_list_update("0.39 0.375 0.36 0.35 Tp List🎁")
    assert values == [Decimal("0.39"), Decimal("0.375"), Decimal("0.36"), Decimal("0.35")]


def test_detect_tp_list_update_handles_thousands_separators() -> None:
    values = detect_tp_list_update("62,000 61,500 60,000 Tp list 🎁")
    assert values == [Decimal("62000"), Decimal("61500"), Decimal("60000")]


def test_detect_tp_list_update_requires_marker() -> None:
    assert detect_tp_list_update("0.39 0.375 0.36") == []


def test_detect_tp_list_update_requires_two_numbers() -> None:
    assert detect_tp_list_update("Tp list 0.39") == []


def test_detect_tp_list_update_empty() -> None:
    assert detect_tp_list_update(None) == []
    assert detect_tp_list_update("") == []

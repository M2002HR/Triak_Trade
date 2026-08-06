from __future__ import annotations

from decimal import Decimal

from triak_trade.backtesting.directives import (
    detect_close_instruction,
    detect_percentage_tp_update,
    detect_stop_loss_value,
    detect_tp_list_update,
)


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


def test_detect_tp_list_update_ignores_target_hit_report_numbers() -> None:
    values = detect_tp_list_update("50 درصد سود با لوریج 20 تارگت 1 ازش کشیدیم بیرون")
    assert values == []


def test_detect_percentage_tp_update_extracts_persian_ladder() -> None:
    values = detect_percentage_tp_update("تیپی 40% 80% 120% 160% 240% استاپ 0.1605")
    assert values == [
        Decimal("40"),
        Decimal("80"),
        Decimal("120"),
        Decimal("160"),
        Decimal("240"),
    ]
    assert detect_stop_loss_value("تیپی 40% 80% استاپ 0.1605") == Decimal("0.1605")


def test_detect_percentage_tp_update_accepts_structured_profit_ladder_with_stop() -> None:
    values = detect_percentage_tp_update(
        "سیو سود\n30%\n80%\n120%\n160%\n240%\nاستاپ 0.1138"  # noqa: RUF001
    )
    assert values == [
        Decimal("30"),
        Decimal("80"),
        Decimal("120"),
        Decimal("160"),
        Decimal("240"),
    ]
    assert detect_percentage_tp_update("سیو سود 30%") == []


def test_conversational_persian_close_word_is_not_an_instruction() -> None:
    assert detect_close_instruction("یه جوری مارکت کریپتو رو ببندم نسخه پیچش کنم") is False
    assert detect_close_instruction("این پوزیشن رو ببند") is True
    assert detect_close_instruction("پوزیشن را ببندید") is True

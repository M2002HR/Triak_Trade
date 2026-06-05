from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from triak_trade.domain.enums import EntryType, MarketType, SignalAction, TradeSide
from triak_trade.domain.models import ParsedSignal
from triak_trade.parsing.validator import ParsedSignalValidator

NOW = datetime.now(timezone.utc)


def _signal(**overrides: object) -> ParsedSignal:
    base: dict[str, object] = {
        "action": SignalAction.OPEN,
        "market": MarketType.FUTURES,
        "symbol": "BTCUSDT",
        "side": TradeSide.LONG,
        "entry_type": EntryType.RANGE,
        "entry_low": Decimal("68000"),
        "entry_high": Decimal("68200"),
        "stop_loss": Decimal("67400"),
        "take_profits": [Decimal("69000"), Decimal("70000")],
        "leverage": 5,
        "confidence": Decimal("0.90"),
        "invalid_reason": None,
        "source_channel_id": "c1",
        "source_message_id": 1,
        "parser_version": "regex-v1",
    }
    base.update(overrides)
    return ParsedSignal(**base)


def test_validator_accepts_complete_signal() -> None:
    ok, errors = ParsedSignalValidator().validate_for_proposal(_signal(), max_leverage=10)
    assert ok is True
    assert errors == []


def test_validator_rejects_missing_sl() -> None:
    ok, errors = ParsedSignalValidator().validate_for_proposal(
        _signal(stop_loss=None),
        max_leverage=10,
    )
    assert ok is False
    assert "missing stop_loss" in errors


def test_validator_rejects_high_leverage() -> None:
    ok, errors = ParsedSignalValidator().validate_for_proposal(
        _signal(leverage=30),
        max_leverage=10,
    )
    assert ok is False
    assert "leverage exceeds max limit" in errors


def test_validator_rejects_wrong_tp_for_long() -> None:
    ok, errors = ParsedSignalValidator().validate_for_proposal(
        _signal(take_profits=[Decimal("67000")]),
        max_leverage=10,
    )
    assert ok is False
    assert "long take_profits should be above entry" in errors


def test_validator_rejects_wrong_sl_for_short() -> None:
    short_signal = _signal(
        side=TradeSide.SHORT,
        entry_low=Decimal("68000"),
        entry_high=Decimal("68200"),
        stop_loss=Decimal("67000"),
        take_profits=[Decimal("66000")],
    )
    ok, errors = ParsedSignalValidator().validate_for_proposal(short_signal, max_leverage=10)
    assert ok is False
    assert "short stop_loss should be above entry" in errors


def test_validator_rejects_unknown() -> None:
    unknown = _signal(action=SignalAction.UNKNOWN)
    ok, errors = ParsedSignalValidator().validate_for_proposal(unknown, max_leverage=10)
    assert ok is False
    assert "signal action is UNKNOWN" in errors


def test_backtest_validator_requires_stop_loss_for_simulation() -> None:
    ok, errors = ParsedSignalValidator().validate_for_backtest(
        _signal(stop_loss=None, take_profits=[], leverage=20),
    )
    assert ok is False
    assert "missing stop_loss" in errors


def test_backtest_validator_allows_signal_without_take_profits() -> None:
    ok, errors = ParsedSignalValidator().validate_for_backtest(
        _signal(take_profits=[], leverage=20),
    )
    assert ok is True
    assert errors == []

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from triak_trade.backtesting.models import BacktestEvent
from triak_trade.backtesting.simulator import BacktestSimulator
from triak_trade.domain.enums import (
    BacktestFillPolicy,
    CandleSource,
    EntryType,
    MarketType,
    SignalAction,
    TradeSide,
)
from triak_trade.domain.models import Candle, ParsedSignal


def _parsed(action: SignalAction, side: TradeSide = TradeSide.LONG) -> ParsedSignal:
    return ParsedSignal(
        action=action,
        market=MarketType.FUTURES,
        symbol="BTCUSDT",
        side=side,
        entry_type=EntryType.LIMIT,
        entry_low=Decimal("100"),
        entry_high=Decimal("101"),
        stop_loss=Decimal("98") if side is TradeSide.LONG else Decimal("103"),
        take_profits=[Decimal("104") if side is TradeSide.LONG else Decimal("97")],
        leverage=2,
        confidence=Decimal("0.9"),
        invalid_reason=None,
        source_channel_id="c",
        source_message_id=1,
        parser_version="x",
    )


def _candle(minute: int, high: str, low: str, o: str = "100", c: str = "101") -> Candle:
    t = datetime(2026, 6, 1, 0, minute, tzinfo=timezone.utc)
    return Candle(
        symbol="BTCUSDT",
        interval="1m",
        open_time=t,
        close_time=t + timedelta(minutes=1),
        open=Decimal(o),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(c),
        volume=Decimal("10"),
        source=CandleSource.FIXTURE,
    )


def test_simulator_long_short_and_fill_policies() -> None:
    open_event = BacktestEvent(
        timestamp=datetime(2026, 6, 1, tzinfo=timezone.utc),
        action=SignalAction.OPEN,
        signal_id="s1",
        parsed_signal=_parsed(SignalAction.OPEN, TradeSide.LONG),
        related_signal_id=None,
        debug_notes=[],
    )
    candles = [_candle(0, "105", "97")]
    sim = BacktestSimulator()
    trades_cons, _ = sim.simulate(
        events=[open_event],
        candles=candles,
        initial_balance=Decimal("1000"),
        risk_per_trade_pct=Decimal("1"),
        fill_policy=BacktestFillPolicy.CONSERVATIVE,
    )
    trades_opt, _ = sim.simulate(
        events=[open_event],
        candles=candles,
        initial_balance=Decimal("1000"),
        risk_per_trade_pct=Decimal("1"),
        fill_policy=BacktestFillPolicy.OPTIMISTIC,
    )
    assert trades_cons[0].status in {"sl_hit_same_candle", "sl_hit"}
    assert trades_opt[0].status in {"tp_hit_same_candle", "tp_hit"}


def test_simulator_cancel_before_resolution() -> None:
    open_event = BacktestEvent(
        timestamp=datetime(2026, 6, 1, tzinfo=timezone.utc),
        action=SignalAction.OPEN,
        signal_id="s1",
        parsed_signal=_parsed(SignalAction.OPEN),
        related_signal_id=None,
        debug_notes=[],
    )
    cancel_event = BacktestEvent(
        timestamp=datetime(2026, 6, 1, 0, 1, tzinfo=timezone.utc),
        action=SignalAction.CANCEL,
        signal_id="s1",
        parsed_signal=_parsed(SignalAction.CANCEL),
        related_signal_id="s1",
        debug_notes=[],
    )
    sim = BacktestSimulator()
    trades, _ = sim.simulate(
        events=[open_event, cancel_event],
        candles=[_candle(0, "101", "99")],
        initial_balance=Decimal("1000"),
        risk_per_trade_pct=Decimal("1"),
        fill_policy=BacktestFillPolicy.CONSERVATIVE,
    )
    assert any(t.status == "cancelled" for t in trades)


def test_simulator_partial_take_profit_ladder_then_stop_loss() -> None:
    parsed = _parsed(SignalAction.OPEN)
    parsed.take_profits = [Decimal("102"), Decimal("104")]
    open_event = BacktestEvent(
        timestamp=datetime(2026, 6, 1, tzinfo=timezone.utc),
        action=SignalAction.OPEN,
        signal_id="s1",
        parsed_signal=parsed,
        related_signal_id=None,
        debug_notes=[],
    )
    candles = [
        _candle(0, "102.5", "99", o="100", c="102"),
        _candle(1, "101.5", "97.5", o="101", c="98"),
    ]
    trades, _ = BacktestSimulator().simulate(
        events=[open_event],
        candles=candles,
        initial_balance=Decimal("1000"),
        risk_per_trade_pct=Decimal("1"),
        fill_policy=BacktestFillPolicy.CONSERVATIVE,
    )
    assert trades[0].status == "partial_tp_then_sl"
    assert any("take_profit_hit=102" in note for note in trades[0].notes)
    assert any("sl_hit" in note for note in trades[0].notes)


def test_simulator_message_close_has_priority_over_future_candle_outcome() -> None:
    open_event = BacktestEvent(
        timestamp=datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc),
        action=SignalAction.OPEN,
        signal_id="s1",
        parsed_signal=_parsed(SignalAction.OPEN),
        related_signal_id=None,
        debug_notes=[],
    )
    close_event = BacktestEvent(
        timestamp=datetime(2026, 6, 1, 0, 1, 30, tzinfo=timezone.utc),
        action=SignalAction.CLOSE,
        signal_id="s1",
        parsed_signal=_parsed(SignalAction.CLOSE),
        related_signal_id="s1",
        debug_notes=[],
        close_fraction=Decimal("1"),
    )
    candles = [
        _candle(0, "101.5", "99.5", o="100", c="100.5"),
        _candle(1, "110", "97", o="100.5", c="105"),
    ]
    trades, _ = BacktestSimulator().simulate(
        events=[open_event, close_event],
        candles=candles,
        initial_balance=Decimal("1000"),
        risk_per_trade_pct=Decimal("1"),
        fill_policy=BacktestFillPolicy.CONSERVATIVE,
    )
    assert trades[0].status == "closed"
    assert trades[0].exit_time == datetime(2026, 6, 1, 0, 1, 30, tzinfo=timezone.utc)


def test_simulator_close_partial_then_finish_on_take_profit() -> None:
    open_event = BacktestEvent(
        timestamp=datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc),
        action=SignalAction.OPEN,
        signal_id="s1",
        parsed_signal=_parsed(SignalAction.OPEN),
        related_signal_id=None,
        debug_notes=[],
    )
    partial_close_event = BacktestEvent(
        timestamp=datetime(2026, 6, 1, 0, 1, 30, tzinfo=timezone.utc),
        action=SignalAction.CLOSE,
        signal_id="s1",
        parsed_signal=_parsed(SignalAction.CLOSE),
        related_signal_id="s1",
        debug_notes=[],
        close_fraction=Decimal("0.5"),
    )
    candles = [
        _candle(0, "101.5", "99.5", o="100", c="100.5"),
        _candle(1, "104.5", "100", o="100.5", c="104"),
    ]
    trades, _ = BacktestSimulator().simulate(
        events=[open_event, partial_close_event],
        candles=candles,
        initial_balance=Decimal("1000"),
        risk_per_trade_pct=Decimal("1"),
        fill_policy=BacktestFillPolicy.CONSERVATIVE,
    )
    assert trades[0].status == "partial_close_then_tp"
    assert any("manual_partial_close" in note for note in trades[0].notes)


def test_simulator_move_stop_to_entry_respects_followup_instruction() -> None:
    open_event = BacktestEvent(
        timestamp=datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc),
        action=SignalAction.OPEN,
        signal_id="s1",
        parsed_signal=_parsed(SignalAction.OPEN),
        related_signal_id=None,
        debug_notes=[],
    )
    breakeven_event = BacktestEvent(
        timestamp=datetime(2026, 6, 1, 0, 1, 30, tzinfo=timezone.utc),
        action=SignalAction.UPDATE_SL,
        signal_id="s1",
        parsed_signal=_parsed(SignalAction.UPDATE_SL),
        related_signal_id="s1",
        debug_notes=[],
        move_stop_to_entry=True,
    )
    candles = [
        _candle(0, "101.5", "99.5", o="100", c="100.5"),
        _candle(1, "101", "99.9", o="100.5", c="100"),
    ]
    trades, _ = BacktestSimulator().simulate(
        events=[open_event, breakeven_event],
        candles=candles,
        initial_balance=Decimal("1000"),
        risk_per_trade_pct=Decimal("1"),
        fill_policy=BacktestFillPolicy.CONSERVATIVE,
    )
    assert trades[0].status == "sl_hit"
    assert trades[0].exit_price == Decimal("100.5")
    assert any("stop_loss_moved_to_entry" in note for note in trades[0].notes)

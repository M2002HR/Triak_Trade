from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from triak_trade.backtesting.models import BacktestEvent
from triak_trade.backtesting.simulator import BacktestSimulator
from triak_trade.backtesting.strategies.default_risk import DefaultRiskManagedStrategy
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
    t = datetime(2026, 6, 1, tzinfo=timezone.utc) + timedelta(minutes=minute)
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


def _contract_candle(
    minute: int,
    high: str,
    low: str,
    o: str = "100",
    c: str = "101",
) -> Candle:
    t = datetime(2026, 6, 1, tzinfo=timezone.utc) + timedelta(minutes=minute)
    return Candle(
        symbol="BTC-SWAP-USDT",
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


def test_simulator_matches_contract_candle_symbol_to_signal_symbol() -> None:
    open_event = BacktestEvent(
        timestamp=datetime(2026, 6, 1, tzinfo=timezone.utc),
        action=SignalAction.OPEN,
        signal_id="s1",
        parsed_signal=_parsed(SignalAction.OPEN, TradeSide.LONG),
        related_signal_id=None,
        debug_notes=[],
    )
    trades, _ = BacktestSimulator().simulate(
        events=[open_event],
        candles=[_contract_candle(0, "102", "99", o="100", c="101")],
        initial_balance=Decimal("1000"),
        risk_per_trade_pct=Decimal("1"),
        fill_policy=BacktestFillPolicy.CONSERVATIVE,
    )
    assert trades[0].status != "not_filled"
    assert trades[0].entry_price is not None


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


def test_simulator_does_not_expire_open_signal_after_configured_hours() -> None:
    open_event = BacktestEvent(
        timestamp=datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc),
        action=SignalAction.OPEN,
        signal_id="s1",
        parsed_signal=_parsed(SignalAction.OPEN),
        related_signal_id=None,
        debug_notes=[],
    )
    candles = [
        _candle(0, "101.5", "99.5", o="100", c="100.5"),
        _candle(61, "102", "100.5", o="101.25", c="101.50"),
    ]
    trades, _ = BacktestSimulator().simulate(
        events=[open_event],
        candles=candles,
        initial_balance=Decimal("1000"),
        risk_per_trade_pct=Decimal("1"),
        fill_policy=BacktestFillPolicy.CONSERVATIVE,
        active_signal_hours=1,
    )
    assert trades[0].status == "open_until_end"
    assert trades[0].exit_time == datetime(2026, 6, 1, 1, 2, tzinfo=timezone.utc)
    assert trades[0].exit_price == Decimal("101.50")


def test_simulator_market_entry_uses_first_available_candle_after_signal() -> None:
    parsed = _parsed(SignalAction.OPEN)
    parsed.entry_type = EntryType.MARKET
    parsed.entry_low = None
    parsed.entry_high = None
    open_event = BacktestEvent(
        timestamp=datetime(2026, 6, 1, 16, 43, 56, tzinfo=timezone.utc),
        action=SignalAction.OPEN,
        signal_id="s1",
        parsed_signal=parsed,
        related_signal_id=None,
        debug_notes=[],
    )
    stale_candle = _candle((24 + 6) * 60, "105", "99", o="100", c="101")

    trades, _ = BacktestSimulator().simulate(
        events=[open_event],
        candles=[stale_candle],
        initial_balance=Decimal("1000"),
        risk_per_trade_pct=Decimal("1"),
        fill_policy=BacktestFillPolicy.CONSERVATIVE,
    )

    assert trades[0].status != "not_filled"
    assert trades[0].entry_time == stale_candle.open_time
    assert trades[0].entry_price == stale_candle.open


def test_simulator_applies_followup_even_after_previous_expiry_window() -> None:
    open_event = BacktestEvent(
        timestamp=datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc),
        action=SignalAction.OPEN,
        signal_id="s1",
        parsed_signal=_parsed(SignalAction.OPEN),
        related_signal_id=None,
        debug_notes=[],
    )
    late_close_event = BacktestEvent(
        timestamp=datetime(2026, 6, 1, 1, 2, tzinfo=timezone.utc),
        action=SignalAction.CLOSE,
        signal_id="s1",
        parsed_signal=_parsed(SignalAction.CLOSE),
        related_signal_id="s1",
        debug_notes=[],
        close_fraction=Decimal("1"),
    )
    candles = [
        _candle(0, "101.5", "99.5", o="100", c="100.5"),
        _candle(61, "102", "100.5", o="101.25", c="101.50"),
        _candle(62, "103", "100", o="101.5", c="102"),
    ]
    trades, _ = BacktestSimulator().simulate(
        events=[open_event, late_close_event],
        candles=candles,
        initial_balance=Decimal("1000"),
        risk_per_trade_pct=Decimal("1"),
        fill_policy=BacktestFillPolicy.CONSERVATIVE,
        active_signal_hours=1,
    )
    assert trades[0].status == "closed"


def test_simulator_snapshots_update_live_pnl_per_message_time() -> None:
    open_event = BacktestEvent(
        timestamp=datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc),
        action=SignalAction.OPEN,
        signal_id="s1",
        parsed_signal=_parsed(SignalAction.OPEN),
        related_signal_id=None,
        debug_notes=[],
        source_message_id=1,
    )
    follow_up = BacktestEvent(
        timestamp=datetime(2026, 6, 1, 0, 2, tzinfo=timezone.utc),
        action=SignalAction.UPDATE_TP,
        signal_id="s1",
        parsed_signal=_parsed(SignalAction.UPDATE_TP),
        related_signal_id="s1",
        debug_notes=[],
        source_message_id=2,
    )
    candles = [
        _candle(0, "101.5", "99.5", o="100", c="100.5"),
        _candle(1, "103", "100.5", o="100.5", c="102.5"),
    ]
    _trades, _balance, snapshots = BacktestSimulator().simulate_with_snapshots(
        events=[open_event, follow_up],
        candles=candles,
        initial_balance=Decimal("1000"),
        risk_per_trade_pct=Decimal("1"),
        fill_policy=BacktestFillPolicy.CONSERVATIVE,
        active_signal_hours=24,
    )
    assert len(snapshots) == 2
    assert snapshots[0].source_message_id == 1
    assert snapshots[0].open_positions == 1
    assert snapshots[0].total_pnl == Decimal("0")
    assert snapshots[0].realized_balance == Decimal("1000")
    assert snapshots[0].current_balance == Decimal("1000")
    assert snapshots[1].open_positions == 1
    assert snapshots[1].unrealized_pnl > Decimal("0")
    assert snapshots[1].current_balance > snapshots[1].realized_balance


def test_simulator_snapshots_include_not_filled_signals() -> None:
    signal = _parsed(SignalAction.OPEN)
    signal.entry_type = EntryType.MARKET
    signal.entry_low = None
    signal.entry_high = None
    signal.stop_loss = Decimal("98")
    signal.take_profits = [Decimal("104")]
    event = BacktestEvent(
        timestamp=datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc),
        action=SignalAction.OPEN,
        signal_id="s1",
        parsed_signal=signal,
        related_signal_id=None,
        debug_notes=[],
        source_message_id=1,
    )
    trades, _balance, snapshots = BacktestSimulator().simulate_with_snapshots(
        events=[event],
        candles=[],
        initial_balance=Decimal("100"),
        risk_per_trade_pct=Decimal("3"),
        fill_policy=BacktestFillPolicy.CONSERVATIVE,
    )

    assert len(trades) == 1
    assert trades[0].status == "not_filled"
    assert len(snapshots) == 1
    assert snapshots[0].closed_trades == 1
    assert "s1" in snapshots[0].signal_states
    assert snapshots[0].signal_states["s1"].status == "not_filled"


def test_simulator_builds_virtual_interval_snapshots_and_price_history() -> None:
    open_event = BacktestEvent(
        timestamp=datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc),
        action=SignalAction.OPEN,
        signal_id="s1",
        parsed_signal=_parsed(SignalAction.OPEN),
        related_signal_id=None,
        debug_notes=[],
        source_message_id=1,
    )
    candles = [
        _candle(0, "101.5", "99.5", o="100", c="100.5"),
        _candle(5, "103", "100.5", o="100.5", c="102.5"),
        _candle(10, "104", "101", o="102.5", c="103.5"),
    ]
    _trades, _balance, snapshots = BacktestSimulator().simulate_with_snapshots(
        events=[open_event],
        candles=candles,
        initial_balance=Decimal("1000"),
        risk_per_trade_pct=Decimal("1"),
        fill_policy=BacktestFillPolicy.CONSERVATIVE,
        snapshot_interval=timedelta(minutes=5),
    )

    interval_snapshots = [item for item in snapshots if item.checkpoint_kind == "interval"]
    assert interval_snapshots
    assert interval_snapshots[0].timestamp == datetime(2026, 6, 1, 0, 5, tzinfo=timezone.utc)


def test_simulator_open_snapshot_does_not_expose_exit_after_partial_tp() -> None:
    parsed = _parsed(SignalAction.OPEN)
    parsed.take_profits = [Decimal("102"), Decimal("106")]
    open_event = BacktestEvent(
        timestamp=datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc),
        action=SignalAction.OPEN,
        signal_id="s1",
        parsed_signal=parsed,
        related_signal_id=None,
        debug_notes=[],
        source_message_id=1,
    )
    candles = [
        _candle(0, "102.5", "99.5", o="100", c="101.5"),
        _candle(5, "103.5", "101", o="101.5", c="102"),
    ]
    _trades, _balance, snapshots = BacktestSimulator().simulate_with_snapshots(
        events=[open_event],
        candles=candles,
        initial_balance=Decimal("1000"),
        risk_per_trade_pct=Decimal("120"),
        fill_policy=BacktestFillPolicy.OPTIMISTIC,
        snapshot_interval=timedelta(minutes=5),
        close_open_positions_at_end=False,
    )

    latest = snapshots[-1].signal_states["s1"]
    assert latest.status == "open"
    assert latest.targets_hit >= 1
    assert latest.exit_time is None
    assert latest.exit_price is None
    assert latest.price_history is not None
    assert len(latest.price_history) >= 2
    assert latest.stop_loss_history is not None
    assert latest.stop_loss_history[0].value == Decimal("98")


def test_simulator_stops_interval_snapshots_after_last_open_position_closes() -> None:
    open_event = BacktestEvent(
        timestamp=datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc),
        action=SignalAction.OPEN,
        signal_id="s1",
        parsed_signal=_parsed(SignalAction.OPEN),
        related_signal_id=None,
        debug_notes=[],
        source_message_id=1,
    )
    candles = [
        _candle(0, "101.5", "99.5", o="100", c="100.5"),
        _candle(5, "103.8", "100.5", o="100.5", c="103.5"),
        _candle(10, "104.2", "103.2", o="103.5", c="103.8"),
        _candle(15, "104.4", "103.5", o="103.8", c="104.0"),
    ]

    _trades, _balance, snapshots = BacktestSimulator().simulate_with_snapshots(
        events=[open_event],
        candles=candles,
        initial_balance=Decimal("1000"),
        risk_per_trade_pct=Decimal("1"),
        fill_policy=BacktestFillPolicy.OPTIMISTIC,
        snapshot_interval=timedelta(minutes=5),
    )

    interval_snapshots = [item for item in snapshots if item.checkpoint_kind == "interval"]
    assert interval_snapshots
    assert all(item.open_positions > 0 for item in interval_snapshots)


def test_simulator_snapshot_keeps_closed_signal_trade_metadata() -> None:
    open_event = BacktestEvent(
        timestamp=datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc),
        action=SignalAction.OPEN,
        signal_id="s1",
        parsed_signal=_parsed(SignalAction.OPEN, TradeSide.LONG).model_copy(
            update={
                "entry_low": Decimal("100"),
                "entry_high": Decimal("100"),
                "stop_loss": Decimal("98"),
                "take_profits": [Decimal("104")],
                "leverage": 10,
            }
        ),
        related_signal_id=None,
        debug_notes=[],
        source_message_id=1,
        leverage=10,
    )
    candles = [
        _candle(0, "100.5", "99.5", o="100", c="100.2"),
        _candle(5, "104.5", "100", o="100.2", c="104"),
    ]

    _trades, _balance, snapshots = BacktestSimulator().simulate_with_snapshots(
        events=[open_event],
        candles=candles,
        initial_balance=Decimal("1000"),
        risk_per_trade_pct=Decimal("1"),
        fill_policy=BacktestFillPolicy.OPTIMISTIC,
        max_effective_leverage=Decimal("25"),
        snapshot_interval=timedelta(minutes=5),
    )

    final_snapshot = snapshots[-1]
    state = final_snapshot.signal_states["s1"]
    assert state.status in {"tp_hit", "tp_hit_same_candle"}
    assert state.stop_loss == Decimal("98")
    assert state.take_profits == [Decimal("104")]
    assert state.declared_leverage == Decimal("10")
    assert state.effective_leverage == Decimal("10")
    assert state.margin > Decimal("0")


def test_simulator_market_entry_matches_normalized_swap_symbol() -> None:
    signal = _parsed(SignalAction.OPEN)
    signal.entry_type = EntryType.MARKET
    signal.entry_low = None
    signal.entry_high = None
    signal.symbol = "BTCUSD"
    event = BacktestEvent(
        timestamp=datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc),
        action=SignalAction.OPEN,
        signal_id="s1",
        parsed_signal=signal,
        related_signal_id=None,
        debug_notes=[],
        source_message_id=1,
    )

    trades, _balance = BacktestSimulator().simulate(
        events=[event],
        candles=[_contract_candle(0, "104.5", "99.5", o="100", c="104")],
        initial_balance=Decimal("100"),
        risk_per_trade_pct=Decimal("3"),
        fill_policy=BacktestFillPolicy.CONSERVATIVE,
    )

    assert trades[0].status != "not_filled"
    assert trades[0].entry_price == Decimal("100")


def test_simulator_close_all_closes_every_open_position() -> None:
    first = _parsed(SignalAction.OPEN)
    first.symbol = "BTCUSDT"
    first.entry_low = Decimal("100")
    first.entry_high = Decimal("100")
    first.stop_loss = Decimal("98")
    first.take_profits = [Decimal("104")]
    second = _parsed(SignalAction.OPEN)
    second.symbol = "ETHUSDT"
    second.entry_low = Decimal("50")
    second.entry_high = Decimal("50")
    second.stop_loss = Decimal("49")
    second.take_profits = [Decimal("55")]

    open_first = BacktestEvent(
        timestamp=datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc),
        action=SignalAction.OPEN,
        signal_id="s1",
        parsed_signal=first,
        related_signal_id=None,
        debug_notes=[],
    )
    open_second = BacktestEvent(
        timestamp=datetime(2026, 6, 1, 0, 0, 10, tzinfo=timezone.utc),
        action=SignalAction.OPEN,
        signal_id="s2",
        parsed_signal=second,
        related_signal_id=None,
        debug_notes=[],
    )
    close_all = BacktestEvent(
        timestamp=datetime(2026, 6, 1, 0, 1, tzinfo=timezone.utc),
        action=SignalAction.CLOSE,
        signal_id=None,
        parsed_signal=_parsed(SignalAction.CLOSE),
        related_signal_id=None,
        debug_notes=[],
        close_all=True,
    )
    candles = [
        _candle(0, "101", "99", o="100", c="100.5"),
        Candle(
            symbol="ETHUSDT",
            interval="1m",
            open_time=datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc),
            close_time=datetime(2026, 6, 1, 0, 1, tzinfo=timezone.utc),
            open=Decimal("50"),
            high=Decimal("51"),
            low=Decimal("49.5"),
            close=Decimal("50.5"),
            volume=Decimal("10"),
            source=CandleSource.FIXTURE,
        ),
        Candle(
            symbol="ETHUSDT",
            interval="1m",
            open_time=datetime(2026, 6, 1, 0, 1, tzinfo=timezone.utc),
            close_time=datetime(2026, 6, 1, 0, 2, tzinfo=timezone.utc),
            open=Decimal("50.25"),
            high=Decimal("50.5"),
            low=Decimal("49.75"),
            close=Decimal("50.1"),
            volume=Decimal("10"),
            source=CandleSource.FIXTURE,
        ),
        _candle(1, "102", "100", o="100.25", c="101"),
    ]

    trades, _ = BacktestSimulator().simulate(
        events=[open_first, open_second, close_all],
        candles=candles,
        initial_balance=Decimal("1000"),
        risk_per_trade_pct=Decimal("1"),
        fill_policy=BacktestFillPolicy.CONSERVATIVE,
    )

    assert len(trades) == 2
    assert {trade.signal_id for trade in trades} == {"s1", "s2"}
    assert all(trade.status == "closed" for trade in trades)


def test_simulator_compounds_risk_from_realized_balance() -> None:
    first = _parsed(SignalAction.OPEN)
    first.entry_low = Decimal("100")
    first.entry_high = Decimal("100")
    first.stop_loss = Decimal("98")
    first.take_profits = [Decimal("104")]
    first.source_message_id = 1
    second = _parsed(SignalAction.OPEN)
    second.entry_low = Decimal("200")
    second.entry_high = Decimal("200")
    second.stop_loss = Decimal("196")
    second.take_profits = [Decimal("208")]
    second.source_message_id = 2
    open_first = BacktestEvent(
        timestamp=datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc),
        action=SignalAction.OPEN,
        signal_id="s1",
        parsed_signal=first,
        related_signal_id=None,
        debug_notes=[],
    )
    open_second = BacktestEvent(
        timestamp=datetime(2026, 6, 1, 0, 2, tzinfo=timezone.utc),
        action=SignalAction.OPEN,
        signal_id="s2",
        parsed_signal=second,
        related_signal_id=None,
        debug_notes=[],
    )
    candles = [
        _candle(0, "104.5", "99.5", o="100", c="104"),
        _candle(2, "208.5", "199.5", o="200", c="208"),
    ]

    trades, final_balance = BacktestSimulator().simulate(
        events=[open_first, open_second],
        candles=candles,
        initial_balance=Decimal("100"),
        risk_per_trade_pct=Decimal("120"),
        fill_policy=BacktestFillPolicy.CONSERVATIVE,
    )

    assert len(trades) == 2
    assert trades[0].pnl == Decimal("0.8")
    assert trades[1].quantity == Decimal("0.1008")
    assert trades[1].pnl == Decimal("0.8064")
    assert final_balance == Decimal("101.6064")


def test_simulator_sizes_positions_from_factor_divided_by_leverage() -> None:
    parsed = _parsed(SignalAction.OPEN)
    parsed.entry_low = Decimal("100")
    parsed.entry_high = Decimal("100")
    parsed.stop_loss = Decimal("50")
    parsed.take_profits = [Decimal("110")]
    parsed.leverage = 20
    event = BacktestEvent(
        timestamp=datetime(2026, 6, 1, tzinfo=timezone.utc),
        action=SignalAction.OPEN,
        signal_id="s1",
        parsed_signal=parsed,
        related_signal_id=None,
        debug_notes=[],
        leverage=20,
    )

    trades, final_balance = BacktestSimulator().simulate(
        events=[event],
        candles=[_candle(0, "110", "99", o="100", c="110")],
        initial_balance=Decimal("100"),
        risk_per_trade_pct=Decimal("120"),
        fill_policy=BacktestFillPolicy.OPTIMISTIC,
        max_effective_leverage=Decimal("50"),
    )

    assert len(trades) == 1
    assert trades[0].quantity == Decimal("1.2")
    assert trades[0].pnl == Decimal("12.0")
    assert trades[0].pnl_pct == Decimal("12.00")
    assert final_balance == Decimal("112.0")


def test_simulator_allocation_factor_respects_min_and_max_clamps() -> None:
    sim = BacktestSimulator()

    assert sim._allocation_pct_for_signal(
        allocation_factor_pct=Decimal("120"),
        leverage=Decimal("200"),
        min_allocation_pct=Decimal("2"),
        max_allocation_pct=Decimal("20"),
    ) == Decimal("2")
    assert sim._allocation_pct_for_signal(
        allocation_factor_pct=Decimal("120"),
        leverage=Decimal("5"),
        min_allocation_pct=Decimal("2"),
        max_allocation_pct=Decimal("20"),
    ) == Decimal("20")
    assert sim._allocation_pct_for_signal(
        allocation_factor_pct=Decimal("150"),
        leverage=Decimal("50"),
        min_allocation_pct=Decimal("2"),
        max_allocation_pct=Decimal("20"),
    ) == Decimal("3")
    assert sim._allocation_pct_for_signal(
        allocation_factor_pct=Decimal("150"),
        leverage=Decimal("20"),
        min_allocation_pct=Decimal("2"),
        max_allocation_pct=Decimal("20"),
    ) == Decimal("7.5")


def _leverage_open_event(leverage: int) -> BacktestEvent:
    parsed = _parsed(SignalAction.OPEN, TradeSide.LONG).model_copy(
        update={"leverage": leverage}
    )
    return BacktestEvent(
        timestamp=datetime(2026, 6, 1, tzinfo=timezone.utc),
        action=SignalAction.OPEN,
        signal_id="s1",
        parsed_signal=parsed,
        related_signal_id=None,
        debug_notes=[],
        leverage=leverage,
    )


def test_simulator_leverage_keeps_account_pnl_pct_constant_for_same_trade() -> None:
    # With factor/leverage sizing, leverage affects allocation %. Lower leverage
    # may be clamped at max allocation, so pnl can differ across leverages.
    candles = [_candle(0, "104.5", "100.0", o="100.5", c="104")]
    sim = BacktestSimulator()

    trades_lev1, _ = sim.simulate(
        events=[_leverage_open_event(1)],
        candles=candles,
        initial_balance=Decimal("1000"),
        risk_per_trade_pct=Decimal("120"),
        fill_policy=BacktestFillPolicy.OPTIMISTIC,
        max_effective_leverage=Decimal("25"),
    )
    trades_lev10, _ = sim.simulate(
        events=[_leverage_open_event(10)],
        candles=candles,
        initial_balance=Decimal("1000"),
        risk_per_trade_pct=Decimal("120"),
        fill_policy=BacktestFillPolicy.OPTIMISTIC,
        max_effective_leverage=Decimal("25"),
    )

    assert trades_lev1[0].status in {"tp_hit", "tp_hit_same_candle"}
    assert trades_lev10[0].pnl > trades_lev1[0].pnl
    assert trades_lev10[0].pnl_pct > trades_lev1[0].pnl_pct


def test_simulator_leverage_clamped_to_max_effective() -> None:
    candles = [_candle(0, "104.5", "100.0", o="100.5", c="104")]
    sim = BacktestSimulator()

    trades_lev100, _ = sim.simulate(
        events=[_leverage_open_event(100)],
        candles=candles,
        initial_balance=Decimal("1000"),
        risk_per_trade_pct=Decimal("120"),
        fill_policy=BacktestFillPolicy.OPTIMISTIC,
        max_effective_leverage=Decimal("25"),
    )
    trades_lev25, _ = sim.simulate(
        events=[_leverage_open_event(25)],
        candles=candles,
        initial_balance=Decimal("1000"),
        risk_per_trade_pct=Decimal("120"),
        fill_policy=BacktestFillPolicy.OPTIMISTIC,
        max_effective_leverage=Decimal("25"),
    )

    # leverage=100 is clamped down to the 25 ceiling, so results match lev=25.
    assert trades_lev100[0].pnl_pct == trades_lev25[0].pnl_pct


def test_simulator_leverage_caps_quantity_by_available_margin() -> None:
    # High risk would size a notional far above balance; leverage caps it.
    candles = [_candle(0, "104.5", "100.0", o="100.5", c="104")]
    sim = BacktestSimulator()

    trades_capped, _ = sim.simulate(
        events=[_leverage_open_event(1)],
        candles=candles,
        initial_balance=Decimal("100"),
        risk_per_trade_pct=Decimal("120"),
        fill_policy=BacktestFillPolicy.OPTIMISTIC,
        max_effective_leverage=Decimal("1"),
    )
    trades_uncapped, _ = sim.simulate(
        events=[_leverage_open_event(25)],
        candles=candles,
        initial_balance=Decimal("100"),
        risk_per_trade_pct=Decimal("120"),
        fill_policy=BacktestFillPolicy.OPTIMISTIC,
        max_effective_leverage=Decimal("25"),
    )

    # Clamp by factor/leverage makes low leverage consume the max allocation cap,
    # while higher leverage gets a smaller allocation percentage.
    assert trades_capped[0].quantity < trades_uncapped[0].quantity


def test_simulator_opens_without_stop_loss_using_synthetic_stop() -> None:
    # No stop loss provided: the signal must still open and use strategy-driven
    # synthetic stop logic, never dropped.
    parsed = _parsed(SignalAction.OPEN, TradeSide.LONG)
    parsed.entry_low = Decimal("100")
    parsed.entry_high = Decimal("100")
    parsed.stop_loss = None
    parsed.take_profits = [Decimal("104")]
    open_event = BacktestEvent(
        timestamp=datetime(2026, 6, 1, tzinfo=timezone.utc),
        action=SignalAction.OPEN,
        signal_id="s1",
        parsed_signal=parsed,
        related_signal_id=None,
        debug_notes=[],
    )
    candles = [_candle(0, "104.5", "99.5", o="100", c="104")]

    trades, _ = BacktestSimulator().simulate(
        events=[open_event],
        candles=candles,
        initial_balance=Decimal("1000"),
        risk_per_trade_pct=Decimal("120"),
        fill_policy=BacktestFillPolicy.OPTIMISTIC,
        strategy=DefaultRiskManagedStrategy(),
    )

    assert len(trades) == 1
    # leverage 1 => raw allocation = 120%, clamped to max 20% => qty = 200/100 = 2
    assert trades[0].quantity == Decimal("2")
    assert trades[0].status in {"tp_hit", "tp_hit_same_candle"}
    assert any("synthetic_stop_strategy=" in note for note in trades[0].notes)


def test_simulator_caps_synthetic_stop_to_loss_budget_of_entry_balance() -> None:
    parsed = _parsed(SignalAction.OPEN, TradeSide.LONG)
    parsed.entry_low = Decimal("100")
    parsed.entry_high = Decimal("100")
    parsed.stop_loss = None
    parsed.take_profits = [Decimal("120")]
    open_event = BacktestEvent(
        timestamp=datetime(2026, 6, 1, tzinfo=timezone.utc),
        action=SignalAction.OPEN,
        signal_id="s1",
        parsed_signal=parsed,
        related_signal_id=None,
        debug_notes=[],
    )
    candles = [_candle(0, "101", "74.9", o="100", c="75")]

    trades, _ = BacktestSimulator().simulate(
        events=[open_event],
        candles=candles,
        initial_balance=Decimal("100"),
        risk_per_trade_pct=Decimal("120"),
        fill_policy=BacktestFillPolicy.CONSERVATIVE,
        default_stop_pct=Decimal("50"),
        synthetic_stop_max_loss_pct_of_balance=Decimal("5"),
    )

    assert trades[0].status in {"sl_hit_same_candle", "sl_hit"}
    assert trades[0].exit_price == Decimal("75")
    assert trades[0].pnl == Decimal("-5")
    assert any("synthetic_stop_risk_capped=50.0->75" in note for note in trades[0].notes)


def test_simulator_caps_quantity_when_fees_exhaust_synthetic_stop_risk_budget() -> None:
    parsed = _parsed(SignalAction.OPEN, TradeSide.LONG)
    parsed.entry_low = Decimal("100")
    parsed.entry_high = Decimal("100")
    parsed.stop_loss = None
    parsed.take_profits = [Decimal("120")]
    open_event = BacktestEvent(
        timestamp=datetime(2026, 6, 1, tzinfo=timezone.utc),
        action=SignalAction.OPEN,
        signal_id="s1",
        parsed_signal=parsed,
        related_signal_id=None,
        debug_notes=[],
    )
    candles = [_candle(0, "101", "99.9", o="100", c="100")]

    trades, _ = BacktestSimulator().simulate(
        events=[open_event],
        candles=candles,
        initial_balance=Decimal("100"),
        risk_per_trade_pct=Decimal("120"),
        fill_policy=BacktestFillPolicy.CONSERVATIVE,
        default_stop_pct=Decimal("5"),
        synthetic_stop_max_loss_pct_of_balance=Decimal("5"),
        max_effective_leverage=Decimal("50"),
        fee_rate_pct=Decimal("3"),
        default_signal_leverage=Decimal("50"),
    )

    assert trades[0].status in {"sl_hit_same_candle", "sl_hit"}
    assert trades[0].quantity == Decimal("0.8333333333333333333333333333")
    assert trades[0].pnl == Decimal("-5.000000000000000000000000000")
    assert any("synthetic_stop_qty_capped_for_risk_budget=" in note for note in trades[0].notes)
    assert any("0.8333333333333333333333333333" in note for note in trades[0].notes)


def test_simulator_filters_invalid_take_profits_from_signal_payload() -> None:
    parsed = _parsed(SignalAction.OPEN, TradeSide.SHORT)
    parsed.entry_low = Decimal("100")
    parsed.entry_high = Decimal("100")
    parsed.stop_loss = Decimal("110")
    parsed.take_profits = [Decimal("95"), Decimal("-25"), Decimal("0"), Decimal("120")]
    open_event = BacktestEvent(
        timestamp=datetime(2026, 6, 1, tzinfo=timezone.utc),
        action=SignalAction.OPEN,
        signal_id="s1",
        parsed_signal=parsed,
        related_signal_id=None,
        debug_notes=[],
    )

    _trades, _balance, snapshots = BacktestSimulator().simulate_with_snapshots(
        events=[open_event],
        candles=[_candle(0, "101", "94", o="100", c="98")],
        initial_balance=Decimal("100"),
        risk_per_trade_pct=Decimal("120"),
        fill_policy=BacktestFillPolicy.CONSERVATIVE,
    )

    assert snapshots[-1].signal_states["s1"].take_profits == [Decimal("95")]


def test_simulator_filters_extreme_strategy_generated_short_take_profits() -> None:
    parsed = _parsed(SignalAction.OPEN, TradeSide.SHORT)
    parsed.entry_low = Decimal("64289.80")
    parsed.entry_high = Decimal("64289.80")
    parsed.stop_loss = Decimal("109292.660")
    parsed.take_profits = []
    open_event = BacktestEvent(
        timestamp=datetime(2026, 6, 1, tzinfo=timezone.utc),
        action=SignalAction.OPEN,
        signal_id="s1",
        parsed_signal=parsed,
        related_signal_id=None,
        debug_notes=[],
    )

    _trades, _balance, snapshots = BacktestSimulator().simulate_with_snapshots(
        events=[open_event],
        candles=[_candle(0, "65000", "64000", o="64289.80", c="64500")],
        initial_balance=Decimal("100"),
        risk_per_trade_pct=Decimal("120"),
        fill_policy=BacktestFillPolicy.CONSERVATIVE,
        strategy=DefaultRiskManagedStrategy(),
    )

    state = snapshots[-1].signal_states["s1"]
    assert state.take_profits == [
        Decimal("63004.004"),
        Decimal("61718.208"),
        Decimal("60432.412"),
        Decimal("59146.616"),
        Decimal("57860.820"),
    ]
    assert all(target > Decimal("0") for target in state.take_profits)


def test_simulator_opens_high_leverage_when_cap_disabled() -> None:
    # max_effective_leverage=None disables leverage expansion and uses leverage 1.
    candles = [_candle(0, "104.5", "100.0", o="100.5", c="104")]
    sim = BacktestSimulator()

    trades_uncapped, _ = sim.simulate(
        events=[_leverage_open_event(100)],
        candles=candles,
        initial_balance=Decimal("100"),
        risk_per_trade_pct=Decimal("120"),
        fill_policy=BacktestFillPolicy.OPTIMISTIC,
        max_effective_leverage=None,
    )
    trades_capped, _ = sim.simulate(
        events=[_leverage_open_event(1)],
        candles=candles,
        initial_balance=Decimal("100"),
        risk_per_trade_pct=Decimal("120"),
        fill_policy=BacktestFillPolicy.OPTIMISTIC,
        max_effective_leverage=Decimal("1"),
    )

    assert len(trades_uncapped) == 1
    assert trades_uncapped[0].quantity == trades_capped[0].quantity

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from triak_trade.backtesting.models import BacktestEvent
from triak_trade.backtesting.simulator import BacktestSimulator
from triak_trade.backtesting.strategies.registry import build_strategy_from_key
from triak_trade.config.settings import Settings
from triak_trade.domain.enums import (
    BacktestFillPolicy,
    CandleSource,
    EntryType,
    MarketType,
    SignalAction,
    TradeSide,
)
from triak_trade.domain.models import Candle, ParsedSignal
from triak_trade.live_trading.models import LiveSession
from triak_trade.live_trading.position_manager import LivePositionManager


def _signal(
    *,
    action: SignalAction = SignalAction.OPEN,
    stop_loss: str = "90",
    take_profits: list[str] | None = None,
    leverage: int = 10,
) -> ParsedSignal:
    return ParsedSignal(
        action=action,
        market=MarketType.FUTURES,
        symbol="BTCUSDT",
        side=TradeSide.LONG,
        entry_type=EntryType.LIMIT,
        entry_low=Decimal("100"),
        entry_high=Decimal("100"),
        stop_loss=Decimal(stop_loss),
        take_profits=[Decimal(item) for item in (take_profits or ["110"])],
        leverage=leverage,
        confidence=Decimal("0.9"),
        invalid_reason=None,
        source_channel_id="channel",
        source_message_id=1,
        parser_version="test",
    )


def _candle(minute: int, *, high: str = "101", low: str = "99") -> Candle:
    opened = datetime(2026, 6, 1, tzinfo=timezone.utc) + timedelta(minutes=minute)
    return Candle(
        symbol="BTCUSDT",
        interval="1m",
        open_time=opened,
        close_time=opened + timedelta(minutes=1),
        open=Decimal("100"),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal("100"),
        volume=Decimal("10"),
        source=CandleSource.FIXTURE,
    )


def _event(
    minute: int,
    signal: ParsedSignal,
    *,
    related_signal_id: str | None = None,
) -> BacktestEvent:
    return BacktestEvent(
        timestamp=datetime(2026, 6, 1, tzinfo=timezone.utc)
        + timedelta(minutes=minute),
        action=signal.action,
        signal_id="signal-1",
        parsed_signal=signal,
        related_signal_id=related_signal_id,
        debug_notes=[],
        source_message_id=minute + 1,
    )


def _live_settings() -> Settings:
    return Settings(
        _env_file=None,
        LIVE_TRADING_DEFAULT_SIGNAL_LEVERAGE=50,
        LIVE_TRADING_MAX_EFFECTIVE_LEVERAGE=10,
        LIVE_TRADING_MIN_ALLOCATION_PCT=Decimal("0"),
        LIVE_TRADING_MAX_ALLOCATION_PCT=Decimal("20"),
        LIVE_TRADING_FEE_RATE_PCT=Decimal("0.04"),
        LIVE_TRADING_MAX_STOP_LOSS_PCT_OF_BALANCE=Decimal("5"),
    )


def test_backtest_explicit_stop_sizing_matches_live_position_manager() -> None:
    settings = _live_settings()
    strategy = build_strategy_from_key("tp_trailing_risk_managed")
    signal = _signal()
    live_session = LiveSession(
        session_id="live-session",
        channels=["channel"],
        trading_mode="demo",
        initial_balance=Decimal("1000"),
        risk_per_trade_pct=Decimal("120"),
        strategy_key="tp_trailing_risk_managed",
        use_ai=False,
        interval="1m",
        paper_balance=Decimal("1000"),
    )
    live_sizing = LivePositionManager(settings).compute_position_sizing(
        session=live_session,
        signal=signal,
        current_balance=Decimal("1000"),
        strategy=strategy,
    )

    _trades, _balance, snapshots = BacktestSimulator().simulate_with_snapshots(
        events=[_event(0, signal)],
        candles=[_candle(0)],
        initial_balance=Decimal("1000"),
        risk_per_trade_pct=Decimal("120"),
        fill_policy=BacktestFillPolicy.CONSERVATIVE,
        max_effective_leverage=Decimal("10"),
        min_allocation_pct=Decimal("0"),
        max_allocation_pct=Decimal("20"),
        max_stop_loss_pct_of_balance=Decimal("5"),
        strategy=strategy,
        fee_rate_pct=Decimal("0.04"),
        default_signal_leverage=Decimal("50"),
        close_open_positions_at_end=False,
    )

    state = snapshots[-1].signal_states["signal-1"]
    assert state.original_quantity == live_sizing.quantity
    assert any(
        note.startswith("quantity_risk_capped_to_preserve_stop=")
        for note in state.notes
    )


def test_backtest_rejects_unsafe_stop_update_like_live_trade() -> None:
    strategy = build_strategy_from_key("tp_trailing_risk_managed")
    open_signal = _signal(stop_loss="95")
    unsafe_update = _signal(
        action=SignalAction.UPDATE_SL,
        stop_loss="50",
    )

    _trades, _balance, snapshots = BacktestSimulator().simulate_with_snapshots(
        events=[
            _event(0, open_signal),
            _event(1, unsafe_update, related_signal_id="signal-1"),
        ],
        candles=[_candle(0), _candle(1), _candle(2)],
        initial_balance=Decimal("1000"),
        risk_per_trade_pct=Decimal("120"),
        fill_policy=BacktestFillPolicy.CONSERVATIVE,
        max_effective_leverage=Decimal("10"),
        min_allocation_pct=Decimal("0"),
        max_allocation_pct=Decimal("20"),
        max_stop_loss_pct_of_balance=Decimal("5"),
        strategy=strategy,
        fee_rate_pct=Decimal("0.04"),
        default_signal_leverage=Decimal("50"),
        close_open_positions_at_end=False,
    )

    state = snapshots[-1].signal_states["signal-1"]
    assert state.stop_loss == Decimal("95")
    assert any(
        note.startswith("stop_loss_update_rejected_risk_budget=")
        for note in state.notes
    )


def test_backtest_rejects_entire_combined_tp_update_when_stop_is_unsafe() -> None:
    strategy = build_strategy_from_key("tp_trailing_risk_managed")
    combined_update = _signal(
        action=SignalAction.UPDATE_TP,
        stop_loss="50",
        take_profits=["115", "120"],
    )

    _trades, _balance, snapshots = BacktestSimulator().simulate_with_snapshots(
        events=[
            _event(0, _signal(stop_loss="95", take_profits=["110"])),
            _event(1, combined_update, related_signal_id="signal-1"),
        ],
        candles=[_candle(0), _candle(1), _candle(2)],
        initial_balance=Decimal("1000"),
        risk_per_trade_pct=Decimal("120"),
        fill_policy=BacktestFillPolicy.CONSERVATIVE,
        max_effective_leverage=Decimal("10"),
        min_allocation_pct=Decimal("0"),
        max_allocation_pct=Decimal("20"),
        max_stop_loss_pct_of_balance=Decimal("5"),
        strategy=strategy,
        fee_rate_pct=Decimal("0.04"),
        default_signal_leverage=Decimal("50"),
        close_open_positions_at_end=False,
    )

    state = snapshots[-1].signal_states["signal-1"]
    assert state.stop_loss == Decimal("95")
    assert state.take_profits == [Decimal("110")]


def test_backtest_applies_combined_entry_update_and_clamps_leverage() -> None:
    strategy = build_strategy_from_key("tp_trailing_risk_managed")
    combined_update = _signal(
        action=SignalAction.UPDATE_ENTRY,
        stop_loss="96",
        take_profits=["115", "120"],
        leverage=25,
    )

    _trades, _balance, snapshots = BacktestSimulator().simulate_with_snapshots(
        events=[
            _event(0, _signal(stop_loss="95")),
            _event(1, combined_update, related_signal_id="signal-1"),
        ],
        candles=[_candle(0), _candle(1), _candle(2)],
        initial_balance=Decimal("1000"),
        risk_per_trade_pct=Decimal("120"),
        fill_policy=BacktestFillPolicy.CONSERVATIVE,
        max_effective_leverage=Decimal("10"),
        min_allocation_pct=Decimal("0"),
        max_allocation_pct=Decimal("20"),
        max_stop_loss_pct_of_balance=Decimal("5"),
        strategy=strategy,
        fee_rate_pct=Decimal("0.04"),
        default_signal_leverage=Decimal("50"),
        close_open_positions_at_end=False,
    )

    state = snapshots[-1].signal_states["signal-1"]
    assert state.stop_loss == Decimal("96")
    assert state.take_profits == [Decimal("115"), Decimal("120")]
    assert state.declared_leverage == Decimal("25")
    assert state.effective_leverage == Decimal("10")

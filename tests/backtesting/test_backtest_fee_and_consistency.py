"""Regression tests for the backtest accounting fixes.

Covers:
- B3: trading fees are subtracted from net PnL and balance (and are 0 by default).
- B2: sum(trade.pnl) stays equal to (final_balance - initial_balance).
- B6: win_rate denominator counts only filled trades (not not_filled/breakeven).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from triak_trade.backtesting.models import BacktestEvent
from triak_trade.backtesting.scoring import ChannelScorer
from triak_trade.backtesting.simulator import BacktestSimulator
from triak_trade.domain.enums import (
    BacktestFillPolicy,
    CandleSource,
    EntryType,
    MarketType,
    SignalAction,
    TradeSide,
)
from triak_trade.domain.models import Candle, ParsedSignal, SimulatedTrade


def _long_open() -> ParsedSignal:
    return ParsedSignal(
        action=SignalAction.OPEN,
        market=MarketType.FUTURES,
        symbol="BTCUSDT",
        side=TradeSide.LONG,
        entry_type=EntryType.LIMIT,
        entry_low=Decimal("100"),
        entry_high=Decimal("101"),
        stop_loss=Decimal("98"),
        take_profits=[Decimal("104")],
        leverage=2,
        confidence=Decimal("0.9"),
        invalid_reason=None,
        source_channel_id="c",
        source_message_id=1,
        parser_version="x",
    )


def _candle(minute: int, *, o: str, high: str, low: str, c: str) -> Candle:
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


def _tp_scenario() -> tuple[list[BacktestEvent], list[Candle]]:
    open_event = BacktestEvent(
        timestamp=datetime(2026, 6, 1, tzinfo=timezone.utc),
        action=SignalAction.OPEN,
        signal_id="s1",
        parsed_signal=_long_open(),
        related_signal_id=None,
        debug_notes=[],
    )
    candles = [
        # Fills the LIMIT entry at 100, does not reach TP (104).
        _candle(0, o="100", high="100.5", low="99.5", c="100"),
        # Hits TP at 104.
        _candle(1, o="100", high="104", low="100", c="104"),
    ]
    return [open_event], candles


def _assert_decimal_close(left: Decimal, right: Decimal) -> None:
    assert abs(left - right) <= Decimal("0.000000000000000000000001")


def test_fees_default_zero_keeps_pnl_gross() -> None:
    events, candles = _tp_scenario()
    trades, _balance = BacktestSimulator().simulate(
        events=events,
        candles=candles,
        initial_balance=Decimal("1000"),
        risk_per_trade_pct=Decimal("120"),
        fill_policy=BacktestFillPolicy.CONSERVATIVE,
    )
    assert trades[0].status.startswith("tp")
    # leverage=2 and factor=120 => raw allocation=60%, clamped to max 20%.
    assert trades[0].pnl == Decimal("6.965174129353233830845771144")
    assert trades[0].fees == Decimal("0")


def test_fees_reduce_net_pnl_and_balance() -> None:
    events, candles = _tp_scenario()
    sim = BacktestSimulator()
    gross_trades, gross_balance = sim.simulate(
        events=events,
        candles=candles,
        initial_balance=Decimal("1000"),
        risk_per_trade_pct=Decimal("120"),
        fill_policy=BacktestFillPolicy.CONSERVATIVE,
    )
    net_trades, net_balance = sim.simulate(
        events=events,
        candles=candles,
        initial_balance=Decimal("1000"),
        risk_per_trade_pct=Decimal("120"),
        fill_policy=BacktestFillPolicy.CONSERVATIVE,
        fee_rate_pct=Decimal("0.1"),
    )
    trade = net_trades[0]
    # entry=100.5, exit=104, qty=200*2/100.5, fee_rate=0.1%
    assert trade.fees == Decimal("0.4069651741293532338308457711")
    # Net pnl is gross minus fees, and balance reflects the net figure.
    assert trade.pnl == gross_trades[0].pnl - trade.fees
    _assert_decimal_close(net_balance, gross_balance - trade.fees)
    assert net_balance == Decimal("1000") + trade.pnl


def test_balance_equals_initial_plus_sum_of_trade_pnl_with_fees() -> None:
    events, candles = _tp_scenario()
    trades, balance = BacktestSimulator().simulate(
        events=events,
        candles=candles,
        initial_balance=Decimal("1000"),
        risk_per_trade_pct=Decimal("120"),
        fill_policy=BacktestFillPolicy.CONSERVATIVE,
        fee_rate_pct=Decimal("0.1"),
    )
    total_pnl = sum((t.pnl for t in trades), Decimal("0"))
    # The B2 invariant must survive fee netting.
    _assert_decimal_close(balance - Decimal("1000"), total_pnl)


def _trade(signal_id: str, *, pnl: str, status: str) -> SimulatedTrade:
    return SimulatedTrade(
        trade_id=f"t_{signal_id}",
        signal_id=signal_id,
        channel_id="c",
        symbol="BTCUSDT",
        side=TradeSide.LONG,
        entry_time=datetime(2026, 6, 1, tzinfo=timezone.utc),
        exit_time=datetime(2026, 6, 1, 1, tzinfo=timezone.utc),
        entry_price=Decimal("100"),
        exit_price=Decimal("100"),
        quantity=Decimal("1"),
        pnl=Decimal(pnl),
        pnl_pct=Decimal("0"),
        fees=Decimal("0"),
        status=status,
        notes=[],
    )


def test_win_rate_denominator_excludes_not_filled_and_breakeven() -> None:
    # 4 trades: 1 not_filled, 1 breakeven (filled), 1 win, 1 loss.
    # filled = 3, wins = 1  ->  win_rate = 1/3 (not 1/4).
    trades = [
        _trade("a", pnl="0", status="not_filled"),
        _trade("b", pnl="0", status="manual_close"),
        _trade("c", pnl="5", status="tp_hit"),
        _trade("d", pnl="-3", status="sl_hit"),
    ]
    events = [
        BacktestEvent(
            timestamp=datetime(2026, 6, 1, tzinfo=timezone.utc),
            action=SignalAction.OPEN,
            signal_id=sid,
            parsed_signal=_long_open(),
            related_signal_id=None,
            debug_notes=[],
        )
        for sid in ("a", "b", "c", "d")
    ]
    metrics, _score, _breakdown = ChannelScorer().score_with_breakdown(
        channel_id="c",
        events=events,
        trades=trades,
        total_pnl=Decimal("2"),
        conservative_pnl=Decimal("2"),
        optimistic_pnl=Decimal("2"),
        from_date=datetime(2026, 6, 1, tzinfo=timezone.utc),
        to_date=datetime(2026, 6, 2, tzinfo=timezone.utc),
        initial_balance=Decimal("1000"),
    )
    assert metrics.win_rate == Decimal("1") / Decimal("3")


# ---------------------------------------------------------------------------
# B4 regression: _first_candle_open_after must match cross-format symbols
# ---------------------------------------------------------------------------

def _swap_candle(minute: int, *, o: str, high: str, low: str) -> Candle:
    """Candle with Toobit contract symbol format (BTC-SWAP-USDT)."""
    t = datetime(2026, 6, 1, tzinfo=timezone.utc) + timedelta(minutes=minute)
    return Candle(
        symbol="BTC-SWAP-USDT",
        interval="1m",
        open_time=t,
        close_time=t + timedelta(minutes=1),
        open=Decimal(o),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(o),
        volume=Decimal("10"),
        source=CandleSource.FIXTURE,
    )


def test_close_uses_same_market_symbol_for_cross_format_candles() -> None:
    """B4: a manual CLOSE with Toobit contract candles (BTC-SWAP-USDT) must
    find the correct open price even though position.symbol is 'BTCUSDT'.
    Before the fix, _first_candle_open_after used == and fell back to
    entry_price, producing a zero-PnL close instead of the real market price.
    """
    open_sig = ParsedSignal(
        action=SignalAction.OPEN,
        market=MarketType.FUTURES,
        symbol="BTCUSDT",
        side=TradeSide.LONG,
        entry_type=EntryType.LIMIT,
        entry_low=Decimal("100"),
        entry_high=Decimal("101"),
        stop_loss=Decimal("98"),
        take_profits=[Decimal("110")],
        leverage=1,
        confidence=Decimal("0.9"),
        invalid_reason=None,
        source_channel_id="c",
        source_message_id=1,
        parser_version="x",
    )
    close_sig = ParsedSignal(
        action=SignalAction.CLOSE,
        market=MarketType.FUTURES,
        symbol="BTCUSDT",
        side=TradeSide.LONG,
        entry_type=EntryType.MARKET,
        entry_low=None,
        entry_high=None,
        stop_loss=None,
        take_profits=[],
        leverage=1,
        confidence=Decimal("0.9"),
        invalid_reason=None,
        source_channel_id="c",
        source_message_id=2,
        parser_version="x",
    )
    t0 = datetime(2026, 6, 1, tzinfo=timezone.utc)
    t2 = t0 + timedelta(minutes=2)
    events = [
        BacktestEvent(timestamp=t0, action=SignalAction.OPEN, signal_id="s1",
                      parsed_signal=open_sig, related_signal_id=None, debug_notes=[]),
        BacktestEvent(timestamp=t2, action=SignalAction.CLOSE, signal_id="s2",
                      parsed_signal=close_sig, related_signal_id="s1", debug_notes=[]),
    ]
    # Candles use the Toobit contract format — position.symbol is still BTCUSDT.
    candles = [
        _swap_candle(0, o="100", high="100", low="99.5"),   # fills entry at 100
        _swap_candle(1, o="100", high="101", low="99"),      # between open and close
        _swap_candle(2, o="105", high="106", low="104"),     # close candle: open=105
    ]
    trades, _balance = BacktestSimulator().simulate(
        events=events,
        candles=candles,
        initial_balance=Decimal("1000"),
        risk_per_trade_pct=Decimal("1"),
        fill_policy=BacktestFillPolicy.CONSERVATIVE,
    )
    trade = trades[0]
    # Position must be closed at 105 (candle 2 open), NOT at entry_price 100.
    # Before the fix (== comparison), exit_price would be 100 → pnl = 0.
    assert trade.exit_price == Decimal("105"), (
        f"Expected exit at 105 (candle open after CLOSE event), got {trade.exit_price}"
    )
    assert trade.pnl > Decimal("0"), "Close at real market price should be profitable"

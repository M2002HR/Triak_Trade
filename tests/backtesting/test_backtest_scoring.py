from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from triak_trade.backtesting.models import BacktestEvent
from triak_trade.backtesting.scoring import ChannelScorer
from triak_trade.domain.enums import EntryType, MarketType, SignalAction, TradeSide
from triak_trade.domain.models import ParsedSignal, SimulatedTrade


def test_scoring_range_and_metrics() -> None:
    parsed = ParsedSignal(
        action=SignalAction.OPEN,
        market=MarketType.FUTURES,
        symbol="BTCUSDT",
        side=TradeSide.LONG,
        entry_type=EntryType.MARKET,
        entry_low=None,
        entry_high=None,
        stop_loss=Decimal("99"),
        take_profits=[Decimal("105")],
        leverage=2,
        confidence=Decimal("0.8"),
        invalid_reason=None,
        source_channel_id="c",
        source_message_id=1,
        parser_version="x",
    )
    events = [
        BacktestEvent(
            timestamp=datetime(2026, 6, 1, tzinfo=timezone.utc),
            action=SignalAction.OPEN,
            signal_id="s1",
            parsed_signal=parsed,
            related_signal_id=None,
            debug_notes=[],
        )
    ]
    trades = [
        SimulatedTrade(
            trade_id="t1",
            signal_id="s1",
            channel_id="c",
            symbol="BTCUSDT",
            side=TradeSide.LONG,
            entry_time=datetime(2026, 6, 1, tzinfo=timezone.utc),
            exit_time=datetime(2026, 6, 1, 0, 1, tzinfo=timezone.utc),
            entry_price=Decimal("100"),
            exit_price=Decimal("101"),
            quantity=Decimal("1"),
            pnl=Decimal("1"),
            pnl_pct=Decimal("1"),
            fees=Decimal("0"),
            status="tp_hit",
            notes=[],
        )
    ]
    metrics, score = ChannelScorer().score(
        channel_id="c",
        events=events,
        trades=trades,
        total_pnl=Decimal("1"),
        conservative_pnl=Decimal("1"),
        optimistic_pnl=Decimal("2"),
        from_date=datetime(2026, 6, 1, tzinfo=timezone.utc),
        to_date=datetime(2026, 6, 2, tzinfo=timezone.utc),
    )
    assert Decimal("0") <= score <= Decimal("100")
    assert metrics.profit_factor is None

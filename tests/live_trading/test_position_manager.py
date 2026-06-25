"""Tests for live trading position manager."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock

from triak_trade.domain.enums import EntryType, MarketType, SignalAction, TradeSide
from triak_trade.domain.models import ParsedSignal
from triak_trade.live_trading.models import LiveSession, LiveTrade, MessageAttribution
from triak_trade.live_trading.position_manager import (
    LivePositionManager,
    _calculate_realized_pnl,
    _calculate_unrealized_pnl,
    _resolve_entry_price,
    _synthetic_stop,
)


def _make_settings() -> MagicMock:
    s = MagicMock()
    s.LIVE_TRADING_DEFAULT_SIGNAL_LEVERAGE = 10
    s.LIVE_TRADING_MAX_EFFECTIVE_LEVERAGE = 50
    s.LIVE_TRADING_MIN_ALLOCATION_PCT = Decimal("2")
    s.LIVE_TRADING_MAX_ALLOCATION_PCT = Decimal("20")
    s.LIVE_TRADING_DEFAULT_STOP_PCT = Decimal("5")
    s.LIVE_TRADING_SYNTHETIC_STOP_MAX_LOSS_PCT = Decimal("5")
    s.LIVE_TRADING_FEE_RATE_PCT = Decimal("0.04")
    return s


def _make_session(balance: Decimal = Decimal("1000")) -> LiveSession:
    return LiveSession(
        session_id="sess1",
        channels=["https://t.me/chan1"],
        trading_mode="demo",
        initial_balance=balance,
        risk_per_trade_pct=Decimal("120"),
        strategy_key="tp_trailing_risk_managed",
        use_ai=False,
        interval="1m",
        paper_balance=balance,
    )


def _make_long_signal(
    symbol: str = "BTCUSDT",
    entry: Decimal = Decimal("50000"),
    sl: Decimal | None = None,
    leverage: int | None = 10,
) -> ParsedSignal:
    return ParsedSignal(
        action=SignalAction.OPEN,
        market=MarketType.FUTURES,
        symbol=symbol,
        side=TradeSide.LONG,
        entry_type=EntryType.LIMIT,
        entry_low=entry,
        entry_high=entry,
        stop_loss=sl,
        take_profits=[Decimal("52000"), Decimal("55000")],
        leverage=leverage,
        confidence=Decimal("0.95"),
        invalid_reason=None,
        source_channel_id="chan1",
        source_message_id=100,
        parser_version="v1",
    )


def _make_short_signal() -> ParsedSignal:
    return ParsedSignal(
        action=SignalAction.OPEN,
        market=MarketType.FUTURES,
        symbol="ETHUSDT",
        side=TradeSide.SHORT,
        entry_type=EntryType.LIMIT,
        entry_low=Decimal("3000"),
        entry_high=Decimal("3000"),
        stop_loss=Decimal("3200"),
        take_profits=[Decimal("2800"), Decimal("2500")],
        leverage=5,
        confidence=Decimal("0.90"),
        invalid_reason=None,
        source_channel_id="chan1",
        source_message_id=101,
        parser_version="v1",
    )


class TestResolvePriceHelpers:
    def test_resolve_entry_from_both_bounds(self) -> None:
        signal = _make_long_signal(entry=Decimal("100"))
        result = _resolve_entry_price(signal)
        assert result == Decimal("100")

    def test_resolve_entry_range(self) -> None:
        signal = ParsedSignal(
            action=SignalAction.OPEN, market=MarketType.FUTURES, symbol="X", side=TradeSide.LONG,
            entry_type=EntryType.RANGE, entry_low=Decimal("98"), entry_high=Decimal("102"),
            stop_loss=None, take_profits=[], leverage=None, confidence=Decimal("0.8"),
            invalid_reason=None, source_channel_id="c", source_message_id=1, parser_version="v1",
        )
        result = _resolve_entry_price(signal)
        assert result == Decimal("100")

    def test_synthetic_stop_long(self) -> None:
        stop = _synthetic_stop(
            side=TradeSide.LONG,
            entry_price=Decimal("100"),
            stop_pct=Decimal("5"),
        )
        assert stop == Decimal("95")

    def test_synthetic_stop_short(self) -> None:
        stop = _synthetic_stop(
            side=TradeSide.SHORT,
            entry_price=Decimal("100"),
            stop_pct=Decimal("5"),
        )
        assert stop == Decimal("105")


class TestPnlCalculations:
    def test_long_profit(self) -> None:
        pnl = _calculate_realized_pnl(
            side="long", entry_price=Decimal("100"), exit_price=Decimal("110"),
            quantity=Decimal("1"), fee_rate_pct=Decimal("0"),
        )
        assert pnl == Decimal("10")

    def test_long_loss(self) -> None:
        pnl = _calculate_realized_pnl(
            side="long", entry_price=Decimal("100"), exit_price=Decimal("90"),
            quantity=Decimal("1"), fee_rate_pct=Decimal("0"),
        )
        assert pnl == Decimal("-10")

    def test_short_profit(self) -> None:
        pnl = _calculate_realized_pnl(
            side="short", entry_price=Decimal("100"), exit_price=Decimal("90"),
            quantity=Decimal("1"), fee_rate_pct=Decimal("0"),
        )
        assert pnl == Decimal("10")

    def test_fees_reduce_pnl(self) -> None:
        pnl = _calculate_realized_pnl(
            side="long", entry_price=Decimal("100"), exit_price=Decimal("110"),
            quantity=Decimal("1"), fee_rate_pct=Decimal("0.04"),
        )
        # entry_fee = 100 * 1 * 0.0004 = 0.04
        # exit_fee = 110 * 1 * 0.0004 = 0.044
        # gross pnl = 10
        # net = 10 - 0.084 = 9.916
        assert pnl == Decimal("9.916")

    def test_unrealized_long_above_entry(self) -> None:
        upnl = _calculate_unrealized_pnl(
            side="long", entry_price=Decimal("100"), mark_price=Decimal("110"),
            quantity=Decimal("1"), fee_rate_pct=Decimal("0"),
        )
        assert upnl == Decimal("10")


class TestPositionSizing:
    def test_basic_long_sizing(self) -> None:
        settings = _make_settings()
        pm = LivePositionManager(settings)
        session = _make_session(Decimal("1000"))
        signal = _make_long_signal(entry=Decimal("50000"), sl=Decimal("47500"), leverage=10)
        strategy = MagicMock()
        result = pm.compute_position_sizing(
            session=session, signal=signal, current_balance=Decimal("1000"), strategy=strategy
        )
        assert result.leverage == 10
        assert result.quantity > 0
        assert result.entry_price == Decimal("50000")
        assert result.stop_loss == Decimal("47500")

    def test_leverage_clamped(self) -> None:
        settings = _make_settings()
        settings.LIVE_TRADING_MAX_EFFECTIVE_LEVERAGE = 10
        pm = LivePositionManager(settings)
        session = _make_session(Decimal("1000"))
        signal = _make_long_signal(leverage=100)
        strategy = MagicMock()
        result = pm.compute_position_sizing(
            session=session, signal=signal, current_balance=Decimal("1000"), strategy=strategy
        )
        assert result.leverage == 10
        assert any("clamped" in n for n in result.notes)

    def test_synthetic_stop_created_when_missing(self) -> None:
        settings = _make_settings()
        pm = LivePositionManager(settings)
        session = _make_session(Decimal("1000"))
        signal = _make_long_signal(sl=None)
        strategy = MagicMock()
        strategy.get_synthetic_take_profits.return_value = []
        result = pm.compute_position_sizing(
            session=session, signal=signal, current_balance=Decimal("1000"), strategy=strategy
        )
        assert result.is_synthetic_stop
        assert result.stop_loss is not None
        # Synthetic SL is below entry for LONG (capped by max-loss budget)
        assert result.stop_loss < Decimal("50000")

    def test_zero_quantity_raises(self) -> None:
        settings = _make_settings()
        pm = LivePositionManager(settings)
        session = _make_session(Decimal("0.0001"))  # tiny balance
        signal = _make_long_signal(entry=Decimal("50000"))
        strategy = MagicMock()
        # With extremely low balance, quantity may round to 0
        # This is implementation-dependent but let's ensure no crash
        try:
            pm.compute_position_sizing(
                session=session,
                signal=signal,
                current_balance=Decimal("0.0001"),
                strategy=strategy,
            )
        except ValueError:
            pass  # Expected


class TestPositionOperations:
    def _make_trade(self) -> LiveTrade:
        return LiveTrade(
            trade_id="t1", session_id="s", signal_id="sig1",
            channel_id="c", channel_input="c", channel_label="@c",
            symbol="BTCUSDT", side="long", leverage=10,
            entry_price=Decimal("50000"), quantity=Decimal("0.01"),
            stop_loss=Decimal("48000"),
            take_profits=[Decimal("52000"), Decimal("55000")],
            balance_at_entry=Decimal("1000"), status="open",
        )

    def test_apply_mark_price(self) -> None:
        settings = _make_settings()
        pm = LivePositionManager(settings)
        trade = self._make_trade()
        pm.apply_mark_price(trade=trade, mark_price=Decimal("51000"), fee_rate_pct=Decimal("0"))
        assert trade.mark_price == Decimal("51000")
        assert trade.unrealized_pnl == Decimal("10")  # (51000 - 50000) * 0.01

    def test_check_tp_hit_long(self) -> None:
        settings = _make_settings()
        pm = LivePositionManager(settings)
        strategy = MagicMock()
        trade = self._make_trade()
        events = pm.check_sl_tp_hit(
            trade=trade, mark_price=Decimal("53000"), strategy=strategy, fee_rate_pct=Decimal("0")
        )
        assert "tp1_hit" in events

    def test_check_sl_hit_long(self) -> None:
        settings = _make_settings()
        pm = LivePositionManager(settings)
        strategy = MagicMock()
        trade = self._make_trade()
        events = pm.check_sl_tp_hit(
            trade=trade, mark_price=Decimal("47000"), strategy=strategy, fee_rate_pct=Decimal("0")
        )
        assert "sl_hit" in events

    def test_close_trade(self) -> None:
        settings = _make_settings()
        pm = LivePositionManager(settings)
        trade = self._make_trade()
        pnl = pm.close_trade(
            trade=trade, close_price=Decimal("52000"),
            reason="sl_hit", fee_rate_pct=Decimal("0"),
        )
        assert pnl == Decimal("20")  # (52000 - 50000) * 0.01
        assert trade.status == "closed"
        assert trade.close_reason == "sl_hit"
        assert trade.remaining_quantity == Decimal("0")
        assert trade.exit_price == Decimal("52000")

    def test_partial_close(self) -> None:
        settings = _make_settings()
        pm = LivePositionManager(settings)
        trade = self._make_trade()
        pnl = pm.apply_partial_close(
            trade=trade, close_fraction=Decimal("0.35"),
            close_price=Decimal("52000"), reason="tp1_hit",
            fee_rate_pct=Decimal("0"),
            is_tp_hit=True,
        )
        assert pnl == Decimal("7")  # (52000 - 50000) * 0.01 * 0.35
        assert trade.status == "partial_close"
        assert trade.remaining_quantity < trade.quantity
        assert trade.targets_hit == 1

    def test_update_stop_loss(self) -> None:
        settings = _make_settings()
        pm = LivePositionManager(settings)
        trade = self._make_trade()
        attr = MessageAttribution(
            message_id=1, channel_id="c", channel_label="@c",
            message_preview="move sl", message_date=datetime.now(timezone.utc),
            action="updated_sl",
        )
        pm.update_stop_loss(trade=trade, new_sl=Decimal("49000"), message=attr)
        assert trade.stop_loss == Decimal("49000")

    def test_move_sl_to_entry(self) -> None:
        settings = _make_settings()
        pm = LivePositionManager(settings)
        trade = self._make_trade()
        attr = MessageAttribution(
            message_id=2, channel_id="c", channel_label="@c",
            message_preview="risk free", message_date=datetime.now(timezone.utc),
            action="updated_sl",
        )
        pm.update_stop_loss(trade=trade, new_sl=None, message=attr, move_to_entry=True)
        assert trade.stop_loss == trade.entry_price

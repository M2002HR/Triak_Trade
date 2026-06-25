"""Tests for live trading store (persistence)."""

from __future__ import annotations

import tempfile
from decimal import Decimal

from triak_trade.live_trading.models import LiveSession, LiveTrade
from triak_trade.live_trading.store import LiveTradingStore


def _make_session(session_id: str = "sess1") -> LiveSession:
    return LiveSession(
        session_id=session_id,
        channels=["https://t.me/chan1"],
        trading_mode="demo",
        initial_balance=Decimal("100"),
        risk_per_trade_pct=Decimal("120"),
        strategy_key="k",
        use_ai=False,
        interval="1m",
    )


def _make_trade(session_id: str = "sess1", trade_id: str = "t1") -> LiveTrade:
    return LiveTrade(
        trade_id=trade_id,
        session_id=session_id,
        signal_id="sig1",
        channel_id="c",
        channel_input="c",
        channel_label="@c",
        symbol="BTCUSDT",
        side="long",
        leverage=10,
        entry_price=Decimal("50000"),
        quantity=Decimal("0.01"),
    )


class TestLiveTradingStore:
    def test_save_and_load_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = LiveTradingStore(tmpdir)
            session = _make_session()
            store.save_session(session)
            loaded = store.load_session("sess1")
            assert loaded is not None
            assert loaded.session_id == "sess1"

    def test_load_nonexistent_session_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = LiveTradingStore(tmpdir)
            assert store.load_session("nonexistent") is None

    def test_list_sessions_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = LiveTradingStore(tmpdir)
            assert store.list_sessions() == []

    def test_list_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = LiveTradingStore(tmpdir)
            store.save_session(_make_session("sess1"))
            store.save_session(_make_session("sess2"))
            sessions = store.list_sessions()
            assert len(sessions) == 2

    def test_get_active_session_returns_running(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = LiveTradingStore(tmpdir)
            session = _make_session()
            session.status = "running"
            store.save_session(session)
            active = store.get_active_session()
            assert active is not None
            assert active.session_id == "sess1"

    def test_get_active_session_ignores_stopped(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = LiveTradingStore(tmpdir)
            session = _make_session()
            session.status = "stopped"
            store.save_session(session)
            assert store.get_active_session() is None

    def test_save_and_load_trade(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = LiveTradingStore(tmpdir)
            trade = _make_trade()
            store.save_trade(trade)
            loaded = store.load_trade("sess1", "t1")
            assert loaded is not None
            assert loaded.trade_id == "t1"
            assert loaded.symbol == "BTCUSDT"

    def test_list_trades(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = LiveTradingStore(tmpdir)
            store.save_trade(_make_trade(trade_id="t1"))
            store.save_trade(_make_trade(trade_id="t2"))
            trades = store.list_trades("sess1")
            assert len(trades) == 2

    def test_list_open_trades(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = LiveTradingStore(tmpdir)
            t_open = _make_trade(trade_id="open1")
            t_open.status = "open"
            t_closed = _make_trade(trade_id="closed1")
            t_closed.status = "closed"
            store.save_trade(t_open)
            store.save_trade(t_closed)
            open_trades = store.list_open_trades("sess1")
            assert len(open_trades) == 1
            assert open_trades[0].trade_id == "open1"

    def test_list_closed_trades(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = LiveTradingStore(tmpdir)
            t_open = _make_trade(trade_id="open1")
            t_open.status = "open"
            t_closed = _make_trade(trade_id="closed1")
            t_closed.status = "closed"
            store.save_trade(t_open)
            store.save_trade(t_closed)
            closed = store.list_closed_trades("sess1")
            assert len(closed) == 1
            assert closed[0].trade_id == "closed1"

    def test_update_overwrites_existing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = LiveTradingStore(tmpdir)
            trade = _make_trade()
            store.save_trade(trade)
            trade.realized_pnl = Decimal("42.5")
            store.save_trade(trade)
            loaded = store.load_trade("sess1", "t1")
            assert loaded is not None
            assert loaded.realized_pnl == Decimal("42.5")

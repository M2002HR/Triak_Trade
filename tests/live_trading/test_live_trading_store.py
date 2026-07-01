"""Tests for live trading store (persistence)."""

from __future__ import annotations

import logging
import tempfile
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from triak_trade.db.base import Base
from triak_trade.live_trading.models import (
    LiveMessageTrace,
    LiveSession,
    LiveSignalSnapshot,
    LiveTrade,
)
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

    def test_save_and_list_signal_snapshots(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = LiveTradingStore(tmpdir)
            signal = LiveSignalSnapshot(
                signal_id="sig1",
                channel_id="@c",
                channel_label="@c",
                created_from_message_id=1,
                status="open",
                status_group="active",
                symbol="BTCUSDT",
                side="long",
                updated_at=datetime.now(timezone.utc),
            )
            store.save_signal_snapshot("sess1", signal)
            loaded = store.load_signal_snapshot("sess1", "sig1")
            assert loaded is not None
            assert loaded.signal_id == "sig1"
            assert store.list_signal_snapshots("sess1")[0].symbol == "BTCUSDT"

    def test_delete_trade_message_and_session_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = LiveTradingStore(tmpdir)
            store.save_session(_make_session())
            store.save_trade(_make_trade())
            store.save_message_trace(
                "sess1",
                LiveMessageTrace(
                    session_id="sess1",
                    message_id=10,
                    channel_id="@c",
                    channel_label="@c",
                    message_date=datetime.now(timezone.utc),
                ),
            )
            store.save_signal_snapshot(
                "sess1",
                LiveSignalSnapshot(
                    signal_id="sig1",
                    channel_id="@c",
                    channel_label="@c",
                    created_from_message_id=10,
                    status="open",
                    status_group="active",
                    updated_at=datetime.now(timezone.utc),
                ),
            )

            assert store.delete_trade("sess1", "t1") is True
            assert store.load_trade("sess1", "t1") is None
            assert store.delete_message_trace("sess1", 10, "@c") is True
            assert store.delete_session("sess1") is True
            assert store.load_session("sess1") is None
            assert store.list_signal_snapshots("sess1") == []

    def test_message_traces_support_url_channel_ids_and_legacy_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = LiveTradingStore(tmpdir)
            trace = LiveMessageTrace(
                session_id="sess1",
                message_id=10,
                channel_id="https://t.me/kiwibot_log",
                channel_label="@kiwibot_log",
                message_date=datetime.now(timezone.utc),
            )

            store.save_message_trace("sess1", trace)
            saved = store.list_message_traces("sess1")
            assert len(saved) == 1
            assert saved[0].channel_id == "https://t.me/kiwibot_log"

            legacy_path = (
                Path(tmpdir)
                / "messages"
                / "sess1"
                / "11_https:"
                / "t.me"
                / "kiwibot_log.json"
            )
            legacy_path.parent.mkdir(parents=True, exist_ok=True)
            legacy_trace = trace.model_copy(update={"message_id": 11})
            legacy_path.write_text(legacy_trace.model_dump_json(indent=2), encoding="utf-8")

            listed = store.list_message_traces("sess1")
            assert {item.message_id for item in listed} == {10, 11}
            assert store.delete_message_trace("sess1", 11, "https://t.me/kiwibot_log") is True

    def test_store_supports_database_backend(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            engine = create_engine(f"sqlite+pysqlite:///{Path(tmpdir) / 'live.db'}", future=True)
            Base.metadata.create_all(engine)
            factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
            store = LiveTradingStore(tmpdir, session_factory=factory)

            session = _make_session()
            trade = _make_trade()
            signal = LiveSignalSnapshot(
                signal_id="sig1",
                channel_id="@c",
                channel_label="@c",
                created_from_message_id=10,
                related_message_ids=[10],
                status="open",
                status_group="active",
                symbol="BTCUSDT",
                side="long",
                updated_at=datetime.now(timezone.utc),
            )
            trace = LiveMessageTrace(
                session_id="sess1",
                message_id=10,
                channel_id="@c",
                channel_username="c",
                channel_label="@c",
                reply_to_msg_id=None,
                message_date=datetime.now(timezone.utc),
                full_text="BUY BTCUSDT",
            )

            store.save_session(session)
            store.save_trade(trade)
            store.save_signal_snapshot("sess1", signal)
            store.save_message_trace("sess1", trace)

            assert store.load_session("sess1") is not None
            assert store.load_trade("sess1", "t1") is not None
            assert store.load_signal_snapshot("sess1", "sig1") is not None
            assert store.list_message_traces("sess1")[0].channel_username == "c"

    def test_store_emits_file_backend_logs(self, caplog) -> None:
        caplog.set_level(logging.DEBUG, logger="triak_trade.live_trading.store")
        with tempfile.TemporaryDirectory() as tmpdir:
            store = LiveTradingStore(tmpdir)
            store.save_session(_make_session())
            store.load_session("sess1")

        messages = [record.message for record in caplog.records]
        assert "live_trading_store.save_session_file" in messages
        assert "live_trading_store.load_session_file" in messages

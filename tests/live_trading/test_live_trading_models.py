"""Tests for live trading models."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from triak_trade.live_trading.models import (
    LiveAccountInfo,
    LiveSession,
    LiveSessionConfig,
    LiveTrade,
    LiveTradingSnapshot,
    MessageAttribution,
    build_live_session_label,
)


def _utc() -> datetime:
    return datetime.now(timezone.utc)


class TestMessageAttribution:
    def test_basic_creation(self) -> None:
        attr = MessageAttribution(
            message_id=100,
            channel_id="chan1",
            channel_label="@chan1",
            message_preview="BUY BTC",
            message_date=_utc(),
            action="opened",
        )
        assert attr.message_id == 100
        assert attr.action == "opened"

    def test_notes_default_empty(self) -> None:
        attr = MessageAttribution(
            message_id=1,
            channel_id="c",
            channel_label="@c",
            message_preview="x",
            message_date=_utc(),
            action="closed",
        )
        assert attr.notes == []


class TestLiveTrade:
    def _make_trade(self) -> LiveTrade:
        return LiveTrade(
            trade_id="lt_test001",
            session_id="sess1",
            signal_id="sig_abc",
            channel_id="chan1",
            channel_input="https://t.me/chan1",
            channel_label="@chan1",
            symbol="BTCUSDT",
            side="long",
            leverage=10,
            entry_price=Decimal("50000"),
            quantity=Decimal("0.01"),
            stop_loss=Decimal("48000"),
            take_profits=[Decimal("52000"), Decimal("55000")],
            balance_at_entry=Decimal("1000"),
            status="open",
        )

    def test_is_open_true_when_open(self) -> None:
        t = self._make_trade()
        assert t.is_open

    def test_is_open_false_when_closed(self) -> None:
        t = self._make_trade()
        t.status = "closed"
        assert not t.is_open

    def test_remaining_qty_defaults_to_quantity(self) -> None:
        t = self._make_trade()
        assert t.remaining_quantity == t.quantity

    def test_total_pnl_sums_realized_and_unrealized(self) -> None:
        t = self._make_trade()
        t.realized_pnl = Decimal("50")
        t.unrealized_pnl = Decimal("30")
        assert t.total_pnl == Decimal("80")

    def test_total_pnl_pct(self) -> None:
        t = self._make_trade()
        t.realized_pnl = Decimal("100")
        t.unrealized_pnl = Decimal("0")
        assert t.total_pnl_pct == Decimal("10")  # 100/1000 * 100

    def test_add_attribution(self) -> None:
        t = self._make_trade()
        attr = MessageAttribution(
            message_id=200,
            channel_id="chan1",
            channel_label="@chan1",
            message_preview="close",
            message_date=_utc(),
            action="closed",
        )
        t.add_attribution(attr)
        assert len(t.message_history) == 1
        assert t.last_attribution() == attr


class TestLiveSession:
    def _make_session(self) -> LiveSession:
        return LiveSession(
            session_id="sess_test",
            channels=["https://t.me/chan1"],
            trading_mode="demo",
            initial_balance=Decimal("100"),
            risk_per_trade_pct=Decimal("120"),
            strategy_key="tp_trailing_risk_managed",
            use_ai=False,
            interval="1m",
        )

    def test_paper_balance_defaults_to_zero_until_account_sync(self) -> None:
        s = self._make_session()
        assert s.paper_balance == Decimal("0")
        assert s.paper_initial_balance == Decimal("0")

    def test_is_running_when_status_running(self) -> None:
        s = self._make_session()
        s.status = "running"
        assert s.is_running

    def test_mark_stopped_sets_status(self) -> None:
        s = self._make_session()
        s.mark_running()
        assert s.is_running
        s.mark_stopped()
        assert s.status == "stopped"
        assert s.stopped_at is not None

    def test_mark_stopped_with_error(self) -> None:
        s = self._make_session()
        s.mark_stopped(error="something went wrong")
        assert s.status == "error"
        assert s.last_error == "something went wrong"
        assert len(s.errors) == 1

    def test_total_pnl(self) -> None:
        s = self._make_session()
        s.total_realized_pnl = Decimal("25")
        s.total_unrealized_pnl = Decimal("10")
        assert s.total_pnl == Decimal("35")


class TestLiveSessionConfig:
    def test_default_values(self) -> None:
        config = LiveSessionConfig(channels=["https://t.me/ch"])
        assert config.trading_mode == "demo"
        assert config.initial_balance == Decimal("0")
        assert config.use_ai

    def test_live_mode_locks_initial_balance(self) -> None:
        config = LiveSessionConfig(
            channels=["https://t.me/ch"],
            trading_mode="live",
            initial_balance=Decimal("500"),
        )
        assert config.trading_mode == "live"
        assert config.initial_balance == Decimal("0")

    def test_requires_exactly_one_channel(self) -> None:
        try:
            LiveSessionConfig(channels=[])
        except ValueError as exc:
            assert "exactly one channel" in str(exc)
        else:
            raise AssertionError("expected validation error for empty channel list")

    def test_rejects_multiple_channels(self) -> None:
        try:
            LiveSessionConfig(channels=["https://t.me/one", "https://t.me/two"])
        except ValueError as exc:
            assert "exactly one channel" in str(exc)
        else:
            raise AssertionError("expected validation error for multiple channels")

    def test_normalizes_blank_label_to_none(self) -> None:
        config = LiveSessionConfig(channels=["https://t.me/ch"], label="   ")
        assert config.label is None


class TestBuildLiveSessionLabel:
    def test_builds_label_from_public_link(self) -> None:
        assert (
            build_live_session_label("https://t.me/Tofan_Trade", "live")
            == "tofan_trade#live"
        )

    def test_builds_label_from_handle(self) -> None:
        assert build_live_session_label("@Ghahr", "demo") == "ghahr#demo"


class TestLiveAccountInfo:
    def test_is_valid_when_no_error(self) -> None:
        info = LiveAccountInfo(wallet_balance=Decimal("500"))
        assert info.is_valid

    def test_is_invalid_when_error(self) -> None:
        info = LiveAccountInfo(error="connection failed")
        assert not info.is_valid


class TestLiveTradingSnapshot:
    def test_total_unrealized_pnl(self) -> None:
        session = LiveSession(
            session_id="s",
            channels=[],
            trading_mode="demo",
            initial_balance=Decimal("100"),
            risk_per_trade_pct=Decimal("10"),
            strategy_key="k",
            use_ai=False,
            interval="1m",
        )
        t1 = LiveTrade(
            trade_id="t1", session_id="s", signal_id="sig1", channel_id="c", channel_input="c",
            channel_label="c", symbol="BTC", side="long", leverage=1, entry_price=Decimal("100"),
            quantity=Decimal("1"), unrealized_pnl=Decimal("5"),
        )
        t2 = LiveTrade(
            trade_id="t2", session_id="s", signal_id="sig2", channel_id="c", channel_input="c",
            channel_label="c", symbol="ETH", side="short", leverage=1, entry_price=Decimal("200"),
            quantity=Decimal("1"), unrealized_pnl=Decimal("-3"),
        )
        snap = LiveTradingSnapshot(session=session, open_trades=[t1, t2])
        assert snap.total_unrealized_pnl == Decimal("2")

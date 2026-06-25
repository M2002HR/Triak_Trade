"""Tests for dashboard live trading coordinator."""

from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

from triak_trade.dashboard.live_runtime import DashboardLiveCoordinator
from triak_trade.live_trading.models import (
    LiveMessageTrace,
    LiveSession,
    LiveSessionConfig,
    LiveTrade,
)


def _make_settings(live_enabled: bool = True) -> MagicMock:
    s = MagicMock()
    s.LIVE_TRADING_ENABLED = live_enabled
    s.LIVE_TRADING_RUNTIME_DIR = "/tmp/test_live"
    s.LIVE_TRADING_MODE = "demo"
    s.LIVE_TRADING_DEFAULT_INITIAL_BALANCE = Decimal("100")
    s.LIVE_TRADING_DEFAULT_RISK_PER_TRADE_PCT = Decimal("120")
    s.LIVE_TRADING_DEFAULT_STRATEGY_KEY = "tp_trailing_risk_managed"
    s.LIVE_TRADING_USE_AI = False
    s.LIVE_TRADING_DEFAULT_CHANNELS = []
    s.TELEGRAM_API_ID = 12345
    s.TELEGRAM_API_HASH = MagicMock()
    s.TELEGRAM_API_HASH.get_secret_value.return_value = "myhash"
    s.TELEGRAM_STRING_SESSION = MagicMock()
    s.TELEGRAM_STRING_SESSION.get_secret_value.return_value = "mysession"
    s.TELEGRAM_SESSION_DIR = "/tmp"
    s.TELEGRAM_SESSION_NAME = "test"
    s.TOOBIT_API_KEY = MagicMock()
    s.TOOBIT_API_KEY.get_secret_value.return_value = "mykey"
    s.TOOBIT_API_SECRET = MagicMock()
    s.TOOBIT_API_SECRET.get_secret_value.return_value = "mysecret"
    s.AI_GATEWAY_ENABLED = False
    s.AI_CLASSIFIER_ENABLED = False
    s.EXECUTION_MODE = "demo"
    return s


class TestDashboardLiveReadiness:
    def test_not_ready_when_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            settings = _make_settings(live_enabled=False)
            settings.LIVE_TRADING_RUNTIME_DIR = tmpdir
            coord = DashboardLiveCoordinator(settings=settings)
            readiness = coord.readiness()
            assert not readiness.ready
            assert any("disabled" in issue.lower() for issue in readiness.issues)

    def test_ready_when_all_configured(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            settings = _make_settings(live_enabled=True)
            settings.LIVE_TRADING_RUNTIME_DIR = tmpdir
            coord = DashboardLiveCoordinator(settings=settings)
            readiness = coord.readiness()
            # With all credentials mocked as present, should be ready
            assert readiness.live_trading_enabled
            assert readiness.telegram_configured
            assert readiness.toobit_configured

    def test_not_ready_when_toobit_unconfigured(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            settings = _make_settings()
            settings.LIVE_TRADING_RUNTIME_DIR = tmpdir
            settings.TOOBIT_API_KEY.get_secret_value.return_value = "replace_me"
            coord = DashboardLiveCoordinator(settings=settings)
            readiness = coord.readiness()
            assert not readiness.ready
            assert any("Toobit" in issue for issue in readiness.issues)


class TestDashboardLiveCoordinatorState:
    def test_is_running_false_when_no_engine(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            settings = _make_settings()
            settings.LIVE_TRADING_RUNTIME_DIR = tmpdir
            coord = DashboardLiveCoordinator(settings=settings)
            assert not coord.is_running()

    def test_get_current_session_none_when_no_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            settings = _make_settings()
            settings.LIVE_TRADING_RUNTIME_DIR = tmpdir
            coord = DashboardLiveCoordinator(settings=settings)
            assert coord.get_current_session() is None

    def test_get_snapshot_none_when_no_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            settings = _make_settings()
            settings.LIVE_TRADING_RUNTIME_DIR = tmpdir
            coord = DashboardLiveCoordinator(settings=settings)
            assert coord.get_snapshot() is None

    def test_list_sessions_empty_initially(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            settings = _make_settings()
            settings.LIVE_TRADING_RUNTIME_DIR = tmpdir
            coord = DashboardLiveCoordinator(settings=settings)
            assert coord.list_sessions() == []

    def test_bootstrap_structure(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            settings = _make_settings()
            settings.LIVE_TRADING_RUNTIME_DIR = tmpdir
            with patch(
                "triak_trade.dashboard.live_runtime.list_available_strategies",
                return_value=[],
            ):
                coord = DashboardLiveCoordinator(settings=settings)
                bootstrap = coord.bootstrap()
            assert "readiness" in bootstrap
            assert "is_running" in bootstrap
            assert "default_initial_balance" in bootstrap
            assert "available_strategies" in bootstrap
            assert bootstrap["live_initial_balance_locked"] is True

    def test_stop_nonexistent_session_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            settings = _make_settings()
            settings.LIVE_TRADING_RUNTIME_DIR = tmpdir
            coord = DashboardLiveCoordinator(settings=settings)
            result = coord.stop_session()
            assert result is None

    def test_start_session_rejects_when_feature_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            settings = _make_settings(live_enabled=False)
            settings.LIVE_TRADING_RUNTIME_DIR = tmpdir
            coord = DashboardLiveCoordinator(settings=settings)
            try:
                coord.start_session(
                    LiveSessionConfig(
                        channels=["https://t.me/demo"],
                        trading_mode="demo",
                    )
                )
            except ValueError as exc:
                assert "disabled" in str(exc).lower()
            else:
                raise AssertionError("Expected ValueError")

    def test_start_session_rejects_live_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            settings = _make_settings(live_enabled=True)
            settings.LIVE_TRADING_RUNTIME_DIR = tmpdir
            coord = DashboardLiveCoordinator(settings=settings)
            try:
                coord.start_session(
                    LiveSessionConfig(
                        channels=["https://t.me/live"],
                        trading_mode="live",
                    )
                )
            except ValueError as exc:
                assert "demo sessions only" in str(exc).lower()
            else:
                raise AssertionError("Expected ValueError")

    def test_overview_aggregates_multiple_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            settings = _make_settings()
            settings.LIVE_TRADING_RUNTIME_DIR = tmpdir
            coord = DashboardLiveCoordinator(settings=settings)

            session_one = LiveSession(
                session_id="ls_one",
                channels=["https://t.me/one"],
                channel_labels=["@one"],
                trading_mode="demo",
                initial_balance=Decimal("100"),
                risk_per_trade_pct=Decimal("120"),
                strategy_key="tp_trailing_risk_managed",
                use_ai=False,
                interval="1m",
                status="running",
                total_messages_processed=7,
                total_realized_pnl=Decimal("12.5"),
            )
            session_two = LiveSession(
                session_id="ls_two",
                channels=["https://t.me/two"],
                channel_labels=["@two"],
                trading_mode="live",
                initial_balance=Decimal("0"),
                risk_per_trade_pct=Decimal("95"),
                strategy_key="tp_trailing_risk_managed",
                use_ai=False,
                interval="1m",
                status="running",
                total_messages_processed=3,
                total_realized_pnl=Decimal("-2.5"),
            )
            coord.store.save_session(session_one)
            coord.store.save_session(session_two)
            coord.store.save_trade(
                LiveTrade(
                    trade_id="trade_open",
                    session_id="ls_one",
                    signal_id="sig-open",
                    channel_id="@one",
                    channel_input="https://t.me/one",
                    channel_label="@one",
                    symbol="BTCUSDT",
                    side="long",
                    leverage=5,
                    entry_price=Decimal("100"),
                    quantity=Decimal("1"),
                    status="open",
                )
            )
            closed_trade = LiveTrade(
                trade_id="trade_closed",
                session_id="ls_two",
                signal_id="sig-closed",
                channel_id="@two",
                channel_input="https://t.me/two",
                channel_label="@two",
                symbol="ETHUSDT",
                side="short",
                leverage=3,
                entry_price=Decimal("200"),
                quantity=Decimal("2"),
                status="closed",
                realized_pnl=Decimal("8"),
            )
            coord.store.save_trade(closed_trade)
            coord.store.save_message_trace(
                "ls_one",
                LiveMessageTrace(
                    session_id="ls_one",
                    message_id=1,
                    channel_id="@one",
                    channel_label="@one",
                    message_date=datetime.now(timezone.utc),
                    final_status="opened_trade",
                ),
            )

            overview = coord.get_overview()
            assert len(overview.active_sessions) == 2
            assert len(overview.open_trades) == 1
            assert overview.recent_closed_trades[0].trade_id == "trade_closed"
            assert overview.totals["messages_processed"] == 10
            assert overview.totals["realized_pnl"] == "10.0"

    def test_get_session_detail_is_session_specific(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            settings = _make_settings()
            settings.LIVE_TRADING_RUNTIME_DIR = tmpdir
            coord = DashboardLiveCoordinator(settings=settings)

            session = LiveSession(
                session_id="ls_detail",
                channels=["https://t.me/detail"],
                channel_labels=["@detail"],
                trading_mode="demo",
                initial_balance=Decimal("100"),
                risk_per_trade_pct=Decimal("120"),
                strategy_key="tp_trailing_risk_managed",
                use_ai=False,
                interval="1m",
                status="running",
            )
            coord.store.save_session(session)
            coord.store.save_trade(
                LiveTrade(
                    trade_id="trade_detail",
                    session_id="ls_detail",
                    signal_id="sig-detail",
                    channel_id="@detail",
                    channel_input="https://t.me/detail",
                    channel_label="@detail",
                    symbol="BTCUSDT",
                    side="long",
                    leverage=4,
                    entry_price=Decimal("25000"),
                    quantity=Decimal("0.1"),
                    status="open",
                )
            )
            coord.store.save_message_trace(
                "ls_detail",
                LiveMessageTrace(
                    session_id="ls_detail",
                    message_id=25,
                    channel_id="@detail",
                    channel_label="@detail",
                    message_date=datetime.now(timezone.utc),
                    preview_text="BUY BTC",
                    final_status="opened_trade",
                ),
            )

            detail = coord.get_session_detail("ls_detail")
            assert detail is not None
            assert detail.session.session_id == "ls_detail"
            assert len(detail.open_trades) == 1
            assert detail.messages[0].session_id == "ls_detail"

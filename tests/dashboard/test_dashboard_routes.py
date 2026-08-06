from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock

from triak_trade.backtesting.real_runner import RealBacktestMessageTrace
from triak_trade.config.settings import Settings
from triak_trade.dashboard.app import create_dashboard_app
from triak_trade.dashboard.backtest_runtime import DashboardBacktestRun
from triak_trade.dashboard.local_client import LocalASGIClient
from triak_trade.live_trading.models import (
    LiveMessageTrace,
    LiveSession,
    LiveSessionDetail,
    LiveTrade,
)


def settings(tmp_path: Path, *, live_mode_enabled: bool = False) -> Settings:
    runtime = tmp_path / "dashboard"
    return Settings(
        _env_file=None,
        DATABASE_URL=f"sqlite+pysqlite:///{tmp_path / 'dashboard.db'}",
        TEST_DATABASE_URL=f"sqlite+pysqlite:///{tmp_path / 'dashboard_test.db'}",
        DASHBOARD_ADMIN_TOKEN="test-token",
        DASHBOARD_SESSION_SECRET="session-secret",
        DASHBOARD_RUNTIME_DIR=str(runtime),
        LIVE_TRADING_RUNTIME_DIR=str(tmp_path / "live_trading"),
        DASHBOARD_PID_FILE=str(runtime / "dashboard.pid"),
        DASHBOARD_STATUS_FILE=str(runtime / "status.json"),
        DASHBOARD_LOG_FILE=str(runtime / "dashboard.log"),
        ROOT_ENV_FILE=str(tmp_path / ".env.local"),
        VERIFICATION_REPORT_DIR=str(tmp_path / "reports"),
        REAL_BACKTEST_REPORT_DIR=str(tmp_path / "backtests"),
        LIVE_TRADING_LIVE_MODE_ENABLED=live_mode_enabled,
    )


def client(tmp_path: Path) -> LocalASGIClient:
    return LocalASGIClient(create_dashboard_app(settings(tmp_path)))


def headers() -> dict[str, str]:
    return {"X-Triak-Admin-Token": "test-token"}


def test_dashboard_main_page_includes_status_cards(tmp_path: Path) -> None:
    response = client(tmp_path).get("/", headers=headers())
    assert response.status_code == 200
    assert "AI Gateway" in response.text
    assert "Kill Switch" in response.text
    assert "Auto Mode" in response.text
    assert "Operational State" in response.text
    assert "Control Surfaces" in response.text
    assert "/backtests" in response.text
    assert "Persistent Backtest Activity" in response.text
    assert "/backtests/analysis" in response.text


def test_backtest_form_renders_tofan_default(tmp_path: Path) -> None:
    response = client(tmp_path).get("/backtests", headers=headers())
    assert response.status_code == 200
    assert "https://t.me/Tofan_Trade" in response.text
    assert "Start Backtest" in response.text
    assert "Signal Replay Monitor" in response.text
    assert "Saved Channels" in response.text
    assert "Save Once, Reuse Anytime" in response.text
    assert "Add Channel To Saved List" in response.text
    assert "Choose Execution Strategy" in response.text
    assert 'id="backtest-strategy-key"' in response.text
    assert "Load Into Form" in response.text
    assert 'id="backtest-saved-channel-select"' in response.text
    assert 'id="backtest-save-channel-input"' in response.text
    assert 'id="backtest-run-type"' not in response.text
    assert 'method="post"' in response.text
    assert 'action="/backtests"' in response.text
    assert 'value="2026-' in response.text
    assert "Default Risk Managed" in response.text
    assert "Trailing TP Risk Managed" in response.text
    assert 'data-backtest-mode="backtest"' not in response.text
    assert 'id="backtest-config"' in response.text
    assert 'id="backtest-aggregate-preview"' in response.text
    assert 'id="phase-progress-grid"' in response.text
    assert "/static/dashboard.js?v=" in response.text
    assert "/static/dashboard.css?v=" in response.text
    assert 'id="backtest-send-log-channel"' not in response.text
    assert 'id="backtest-log-per-message"' not in response.text


def test_backtest_page_renders_dedicated_workspace(tmp_path: Path) -> None:
    response = client(tmp_path).get("/backtests", headers=headers())
    assert response.status_code == 200
    assert "Signal Replay Monitor" in response.text
    assert "Start Backtest" in response.text
    assert "https://t.me/Tofan_Trade" in response.text
    assert 'type="datetime-local"' in response.text
    assert "Only The Parameters That Affect Backtest Simulation" in response.text
    assert "Default Risk Managed" in response.text
    assert "Trailing TP Risk Managed" in response.text
    assert 'id="backtest-run-type"' not in response.text
    assert 'method="post"' in response.text
    assert 'action="/backtests"' in response.text
    assert 'id="backtest-config"' in response.text
    assert 'id="backtest-capital-per-signal"' in response.text
    assert 'id="phase-progress-grid"' in response.text
    assert 'id="backtest-default-stop-pct"' not in response.text
    assert 'id="backtest-initial-balance"' not in response.text
    assert 'id="backtest-send-log-channel"' not in response.text
    assert 'id="backtest-log-per-message"' not in response.text
    assert 'id="backtest-aggregate-preview"' in response.text
    assert 'href="/backtests/analysis"' in response.text


def test_backtest_analysis_page_renders_rankings_filters_and_charts(tmp_path: Path) -> None:
    response = client(tmp_path).get("/backtests/analysis", headers=headers())

    assert response.status_code == 200
    assert "Channel, Strategy &amp; Risk Analysis" in response.text
    assert 'id="backtest-analysis-filters"' in response.text
    assert 'id="analysis-channel-rankings"' in response.text
    assert 'id="analysis-risk-return-chart"' in response.text
    assert 'id="analysis-bootstrap-chart"' in response.text
    assert 'data-analysis-tab="parameters"' in response.text
    assert 'data-analysis-tab="methodology"' in response.text
    assert "/static/backtest_analysis.js?v=" in response.text


def test_backtest_form_post_starts_run_and_redirects(tmp_path: Path) -> None:
    app = create_dashboard_app(settings(tmp_path))
    service = app.state.dashboard_service
    service.backtest_readiness = lambda: {"ready": True, "issues": []}  # type: ignore[assignment]

    def _start_run(request, *, channel_input: str, strategy_key: str):
        return DashboardBacktestRun(
            run_id="backtest_form_run",
            run_type="backtest",
            channel_input=channel_input,
            channel_resolved=request.channel,
            from_date=request.from_date,
            to_date=request.to_date,
            interval=request.interval,
            max_messages=request.max_messages,
            initial_balance=request.initial_balance,
            risk_per_trade_pct=request.risk_per_trade_pct,
            use_ai=request.use_ai,
            send_log_channel=request.send_log_channel,
            log_per_message=request.log_per_message,
            status="queued",
            created_at=datetime.now(timezone.utc),
        )

    service.backtests.start_run = _start_run  # type: ignore[method-assign]
    client_obj = LocalASGIClient(app)

    response = client_obj.post(
        "/backtests",
        headers=headers(),
        data={
            "channel": "https://t.me/tonMiniAppc",
            "from_date": "2026-03-15T13:17",
            "to_date": "2026-07-16T13:17",
            "interval": "1m",
            "max_messages": "1000",
            "capital_per_signal": "100",
            "risk_per_trade_pct": "60",
            "fill_policy": "conservative",
            "leverage_source": "signal_or_default",
            "fixed_leverage": "50",
            "max_effective_leverage": "50",
            "default_signal_leverage": "50",
            "min_allocation_pct": "2",
            "max_allocation_pct": "20",
            "synthetic_stop_max_loss_pct": "5",
            "fee_rate_pct": "0.01",
            "lifecycle_refresh_interval": "15m",
            "max_parallel_signals": "2",
            "include_not_filled_signals": "on",
            "close_open_positions_at_end": "on",
            "strategy_key": "tp_trailing_risk_managed",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/backtests?run_id=backtest_form_run"


def test_backtest_run_messages_endpoint_returns_trace_list(tmp_path: Path) -> None:
    app = create_dashboard_app(settings(tmp_path))
    service = app.state.dashboard_service
    service.backtests.store.write(
        DashboardBacktestRun(
            run_id="backtest_trace_api",
            channel_input="@Tofan_Trade",
            channel_resolved="https://t.me/Tofan_Trade",
            from_date=datetime(2026, 6, 3, tzinfo=timezone.utc),
            to_date=datetime(2026, 6, 4, tzinfo=timezone.utc),
            interval="1m",
            max_messages=100,
            use_ai=False,
            send_log_channel=False,
            log_per_message=False,
            status="running",
            created_at=datetime(2026, 6, 4, tzinfo=timezone.utc),
            messages=[
                RealBacktestMessageTrace(
                    message_id=77,
                    channel_id="https://t.me/Tofan_Trade",
                    channel_username="Tofan_Trade",
                    message_date=datetime(2026, 6, 4, tzinfo=timezone.utc),
                    last_updated_at=datetime(2026, 6, 4, tzinfo=timezone.utc),
                    preview_text="BTCUSDT LONG",
                    classification="new_signal",
                    parsed_action="open",
                )
            ],
        )
    )
    client_obj = LocalASGIClient(app)

    response = client_obj.get(
        "/api/backtests/runs/backtest_trace_api/messages",
        headers=headers(),
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["limit"] == 500
    assert len(payload["messages"]) == 1
    assert payload["messages"][0]["message_id"] == 77


def test_latest_run_endpoint_prefers_active_run(
    tmp_path: Path,
) -> None:
    app = create_dashboard_app(settings(tmp_path))
    service = app.state.dashboard_service
    now = datetime(2026, 7, 20, tzinfo=timezone.utc)

    def _run(run_id: str, *, run_type: str, status: str, created_at: datetime):
        return DashboardBacktestRun(
            run_id=run_id,
            run_type=run_type,
            channel_input="@sample",
            channel_resolved="https://t.me/sample",
            from_date=now,
            to_date=now,
            interval="1m",
            max_messages=100,
            use_ai=False,
            send_log_channel=False,
            log_per_message=False,
            status=status,
            created_at=created_at,
            heartbeat_at=created_at,
        )

    service.backtests.store.write(
        _run(
            "backtest_completed",
            run_type="backtest",
            status="completed",
            created_at=now - timedelta(minutes=1),
        )
    )
    service.backtests.store.write(
        _run(
            "backtest_running",
            run_type="backtest",
            status="running",
            created_at=now - timedelta(minutes=2),
        )
    )
    client_obj = LocalASGIClient(app)

    latest = client_obj.get(
        "/api/backtests/runs/latest?prefer_active=true",
        headers=headers(),
    )
    listed = client_obj.get(
        "/api/backtests/runs?limit=10",
        headers=headers(),
    )

    assert latest.status_code == 200
    assert latest.json()["run"]["run_id"] == "backtest_running"
    assert listed.status_code == 200
    assert listed.json()["total_runs"] == 2
    assert {run["run_id"] for run in listed.json()["runs"]} == {
        "backtest_completed",
        "backtest_running",
    }


def test_backtest_analysis_api_reads_persisted_run_and_applies_channel_filter(
    tmp_path: Path,
) -> None:
    app = create_dashboard_app(settings(tmp_path))
    service = app.state.dashboard_service
    now = datetime(2026, 7, 20, tzinfo=timezone.utc)
    signals = [
        {
            "signal_id": f"signal_{index}",
            "symbol": "BTCUSDT",
            "side": "long",
            "status": "tp1_hit" if pnl > 0 else "stop_loss_hit",
            "status_group": "inactive",
            "entry_time": now.isoformat(),
            "exit_time": now.isoformat(),
            "initial_balance": "100",
            "total_pnl": str(pnl),
        }
        for index, pnl in enumerate([10, 8, -2, 4, 5], start=1)
    ]
    service.backtests.store.write(
        DashboardBacktestRun(
            run_id="backtest_analysis_run",
            run_type="backtest",
            channel_input="@analysis_channel",
            channel_resolved="https://t.me/analysis_channel",
            from_date=now - timedelta(days=30),
            to_date=now,
            interval="1m",
            max_messages=100,
            strategy_key="default_risk_managed",
            request_payload={
                "capital_per_signal": "100",
                "fill_policy": "conservative",
                "fee_rate_pct": "0.01",
            },
            use_ai=False,
            send_log_channel=False,
            log_per_message=False,
            status="completed",
            created_at=now,
            started_at=now,
            finished_at=now + timedelta(minutes=1),
            backtest_aggregate={
                "total_signals": 5,
                "filled_signals": 5,
                "closed_signals": 5,
                "wins": 4,
                "losses": 1,
                "total_pnl": "25",
                "total_initial_balance": "500",
                "max_drawdown": "2",
                "period_pnl": {"daily": [{"period": "2026-07-20", "pnl": "25"}]},
            },
            signals=signals,
        )
    )
    client_obj = LocalASGIClient(app)

    response = client_obj.get(
        "/api/backtests/analysis?channel=https%3A%2F%2Ft.me%2Fanalysis_channel",
        headers=headers(),
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["overview"]["completed_runs"] == 1
    assert payload["overview"]["signals"] == 5
    assert payload["channel_rankings"][0]["channel"] == "https://t.me/analysis_channel"
    assert payload["channel_rankings"][0]["average_profit"] == "6.75"
    assert payload["bootstrap"][0]["available"] is True
    assert payload["methodology"]["version"] == "backtest-score-v1"


def test_backtest_analysis_api_ingests_cli_report_only_runs(tmp_path: Path) -> None:
    app = create_dashboard_app(settings(tmp_path))
    service = app.state.dashboard_service
    now = datetime(2026, 7, 20, tzinfo=timezone.utc)
    signals = [
        {
            "signal_id": f"cli_signal_{index}",
            "symbol": "ETHUSDT",
            "side": "short",
            "status": "tp1_hit" if pnl > 0 else "stop_loss_hit",
            "status_group": "inactive",
            "entry_time": now.isoformat(),
            "exit_time": now.isoformat(),
            "initial_balance": "100",
            "total_pnl": str(pnl),
        }
        for index, pnl in enumerate([6, 5, -2, 3, 4], start=1)
    ]
    service.backtest_runner.report_store.write(
        {
            "run_type": "backtest",
            "success": True,
            "channel": "https://t.me/cli_channel",
            "from_date": (now - timedelta(days=14)).isoformat(),
            "to_date": now.isoformat(),
            "generated_at": now.isoformat(),
            "interval": "5m",
            "strategy_key": "tp_trailing_risk_managed",
            "ai_used": False,
            "request": {
                "capital_per_signal": "100",
                "fill_policy": "conservative",
                "fee_rate_pct": "0.02",
            },
            "signals": signals,
            "aggregate": {
                "total_signals": 5,
                "filled_signals": 5,
                "closed_signals": 5,
                "wins": 4,
                "losses": 1,
                "total_pnl": "16",
                "total_initial_balance": "500",
                "max_drawdown": "2",
                "period_pnl": {"daily": [{"period": "2026-07-20", "pnl": "16"}]},
            },
            "errors": [],
            "warnings": [],
        }
    )
    client_obj = LocalASGIClient(app)

    response = client_obj.get(
        "/api/backtests/analysis?channel=https%3A%2F%2Ft.me%2Fcli_channel",
        headers=headers(),
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["overview"]["runs"] == 1
    assert payload["overview"]["report_runs"] == 1
    assert payload["data_sources"]["report_only_runs"] == 1
    assert payload["run_comparison"][0]["source"] == "report"
    assert payload["run_comparison"][0]["strategy"] == "tp_trailing_risk_managed"


def test_backtest_analysis_deduplicates_dashboard_run_and_its_report(tmp_path: Path) -> None:
    app = create_dashboard_app(settings(tmp_path))
    service = app.state.dashboard_service
    now = datetime(2026, 7, 20, tzinfo=timezone.utc)
    signal = {
        "signal_id": "dedupe_signal",
        "symbol": "BTCUSDT",
        "side": "long",
        "status": "tp1_hit",
        "status_group": "inactive",
        "entry_time": now.isoformat(),
        "exit_time": now.isoformat(),
        "initial_balance": "100",
        "total_pnl": "5",
    }
    aggregate = {
        "total_signals": 1,
        "filled_signals": 1,
        "closed_signals": 1,
        "wins": 1,
        "losses": 0,
        "total_pnl": "5",
        "total_initial_balance": "100",
        "max_drawdown": "0",
        "period_pnl": {"daily": [{"period": "2026-07-20", "pnl": "5"}]},
    }
    stored = service.backtest_runner.report_store.write(
        {
            "run_type": "backtest",
            "success": True,
            "channel": "https://t.me/dedupe",
            "from_date": (now - timedelta(days=1)).isoformat(),
            "to_date": now.isoformat(),
            "generated_at": now.isoformat(),
            "interval": "1m",
            "strategy_key": "default_risk_managed",
            "request": {"capital_per_signal": "100", "fill_policy": "conservative"},
            "signals": [signal],
            "aggregate": aggregate,
        }
    )
    service.backtests.store.write(
        DashboardBacktestRun(
            run_id="dashboard_dedupe_run",
            run_type="backtest",
            channel_input="@dedupe",
            channel_resolved="https://t.me/dedupe",
            from_date=now - timedelta(days=1),
            to_date=now,
            interval="1m",
            max_messages=10,
            request_payload={"capital_per_signal": "100", "fill_policy": "conservative"},
            use_ai=False,
            send_log_channel=False,
            log_per_message=False,
            status="completed",
            created_at=now,
            signals=[signal],
            backtest_aggregate=aggregate,
            report_path=stored.json_path,
            markdown_report_path=stored.markdown_path,
        )
    )

    response = LocalASGIClient(app).get(
        "/api/backtests/analysis?channel=https%3A%2F%2Ft.me%2Fdedupe",
        headers=headers(),
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["overview"]["runs"] == 1
    assert payload["data_sources"]["report_only_runs"] == 0
    assert payload["run_comparison"][0]["source"] == "dashboard"


def test_logs_page_renders_log_channel_status(tmp_path: Path) -> None:
    response = client(tmp_path).get("/logs", headers=headers())
    assert response.status_code == 200
    assert "Telegram Log Channel" in response.text
    assert "@triak_logs" in response.text
    assert "Log Viewer" in response.text


def test_removed_backtest_reports_page_returns_not_found(tmp_path: Path) -> None:
    response = client(tmp_path).get("/reports", headers=headers())
    assert response.status_code == 404


def test_live_reports_page_reads_db_backed_sessions_and_trades(tmp_path: Path) -> None:
    app = create_dashboard_app(settings(tmp_path, live_mode_enabled=True))
    store = app.state.live_coordinator.store
    session = LiveSession(
        session_id="ls_live_report",
        channels=["https://t.me/sample_chan"],
        channel_labels=["@sample_chan"],
        trading_mode="demo",
        initial_balance=Decimal("0"),
        risk_per_trade_pct=Decimal("120"),
        strategy_key="tp_trailing_risk_managed",
        use_ai=False,
        interval="1m",
        status="running",
        total_messages_processed=14,
        total_signals_received=9,
        total_signals_opened=6,
        open_positions_count=1,
        closed_trades_count=1,
        wins=1,
        losses=0,
        total_realized_pnl=Decimal("4.25"),
        total_unrealized_pnl=Decimal("1.10"),
    )
    store.save_session(session)
    store.save_trade(
        LiveTrade(
            trade_id="lt_closed",
            session_id=session.session_id,
            signal_id="sig_closed",
            channel_id="https://t.me/sample_chan",
            channel_input="@sample_chan",
            channel_label="@sample_chan",
            symbol="BTCUSDT",
            side="long",
            leverage=5,
            entry_price=Decimal("100"),
            quantity=Decimal("1"),
            status="closed",
            realized_pnl=Decimal("4.25"),
            exit_price=Decimal("104.25"),
            balance_at_entry=Decimal("100"),
            closed_at=datetime(2026, 7, 4, tzinfo=timezone.utc),
        )
    )

    client_obj = LocalASGIClient(app)
    response = client_obj.get("/reports/live", headers=headers())

    assert response.status_code == 200
    assert "sample_chan" in response.text
    assert "tp_trailing_risk_managed" in response.text
    assert "+5.35" in response.text
    assert "Signals Received" in response.text


def test_status_json_contains_no_secrets(tmp_path: Path) -> None:
    response = client(tmp_path).get("/status", headers=headers())
    assert response.status_code == 200
    text = response.text
    assert "test-token" not in text
    assert "session-secret" not in text
    assert response.json()["live_trading_blocked"] is True


def test_status_json_reflects_live_mode_flag(tmp_path: Path) -> None:
    app = create_dashboard_app(settings(tmp_path, live_mode_enabled=True))
    client_obj = LocalASGIClient(app)
    response = client_obj.get("/status", headers=headers())
    assert response.status_code == 200
    assert response.json()["live_trading_blocked"] is False


def test_login_page_renders(tmp_path: Path) -> None:
    response = client(tmp_path).get("/login")
    assert response.status_code == 200
    assert "Dashboard Sign In" in response.text


def test_status_json_unauthorized_is_not_redirected(tmp_path: Path) -> None:
    response = client(tmp_path).get("/status", follow_redirects=False)
    assert response.status_code == 401


def test_settings_page_renders_ai_keyword_filters_tab(tmp_path: Path) -> None:
    response = client(tmp_path).get("/settings?tab=ai-keywords", headers=headers())
    assert response.status_code == 200
    assert "AI Keyword Filters" in response.text
    assert "Skip Keywords" in response.text
    assert "Force Include Keywords" in response.text


def test_settings_page_renders_backtest_lifecycle_controls(tmp_path: Path) -> None:
    response = client(tmp_path).get("/settings", headers=headers())
    assert response.status_code == 200
    assert "Signal Refresh Cadence" in response.text
    assert "Refresh Interval" in response.text
    assert "30m" in response.text
    assert "Operational Safety Envelope" in response.text
    assert "Operational Safety Envelope" in response.text


def test_live_trading_page_is_english_only(tmp_path: Path) -> None:
    response = client(tmp_path).get("/live-trading", headers=headers())
    assert response.status_code == 200
    text = response.text
    assert (
        "Monitor message flow, open risk, session health, and trade outcomes in one place"
        in text
    )
    assert "Per-Channel Runtime" in text
    assert "Account Information" in text
    assert "Concurrent Trading Sessions" in text
    assert "Search Sessions" in text
    assert "Attention First" in text
    assert "Needs Attention" not in text
    assert "Message Stream" in text
    assert "Recent Closed Trades" in text
    assert "/static/live_trading.js?v=" in text
    assert "اطلاعات" not in text
    assert "سشن" not in text


def test_live_trading_page_renders_session_filters_and_sorting_controls(tmp_path: Path) -> None:
    response = client(tmp_path).get("/live-trading", headers=headers())
    assert response.status_code == 200
    text = response.text
    assert 'id="lt-session-search"' in text
    assert 'data-session-mode-filter="demo"' in text
    assert 'data-session-state-filter="attention"' in text
    assert 'id="lt-session-sort"' in text
    assert "Session Name (A-Z)" in text
    assert "Highest Realized PnL" in text
    assert "Asia/Tehran" in text
    assert "Delete Stopped History" in text


def test_live_trading_page_disables_live_option_without_flag(tmp_path: Path) -> None:
    response = client(tmp_path).get("/live-trading", headers=headers())
    assert response.status_code == 200
    assert 'option value="live" disabled' in response.text


def test_live_trading_page_enables_live_option_with_flag(tmp_path: Path) -> None:
    app = create_dashboard_app(settings(tmp_path, live_mode_enabled=True))
    client_obj = LocalASGIClient(app)
    response = client_obj.get("/live-trading", headers=headers())
    assert response.status_code == 200
    assert 'option value="live"' in response.text
    assert 'option value="live" disabled' not in response.text


def test_live_session_start_uses_submitted_balance_in_demo_mode(
    tmp_path: Path, monkeypatch
) -> None:
    app = create_dashboard_app(settings(tmp_path))
    client_obj = LocalASGIClient(app)
    live_coordinator = app.state.live_coordinator

    def fake_start_session(config):
        assert config.trading_mode == "demo"
        assert config.initial_balance == Decimal("0")
        assert config.label == "chan#demo"
        return LiveSession(
            session_id="ls_test",
            channels=config.channels,
            channel_labels=["@chan"],
            trading_mode=config.trading_mode,
            initial_balance=config.initial_balance,
            risk_per_trade_pct=config.risk_per_trade_pct,
            strategy_key=config.strategy_key,
            use_ai=config.use_ai,
            interval=config.interval,
            label=config.label,
        )

    monkeypatch.setattr(live_coordinator, "start_session", fake_start_session)

    response = client_obj.post(
        "/api/live/sessions/start",
        headers=headers(),
        json={
            "channels": ["https://t.me/chan"],
            "trading_mode": "demo",
            "initial_balance": "9999",
            "risk_per_trade_pct": "120",
            "strategy_key": "tp_trailing_risk_managed",
            "use_ai": False,
        },
    )
    assert response.status_code == 202
    assert response.json()["session"]["initial_balance"] == "0"
    assert response.json()["session"]["label"] == "chan#demo"


def test_live_session_start_rejects_live_mode(tmp_path: Path) -> None:
    response = client(tmp_path).post(
        "/api/live/sessions/start",
        headers=headers(),
        json={
            "channels": ["https://t.me/chan"],
            "trading_mode": "live",
            "initial_balance": "9999",
            "risk_per_trade_pct": "120",
            "strategy_key": "tp_trailing_risk_managed",
            "use_ai": False,
        },
    )
    assert response.status_code == 400
    assert any(
        item in response.json()["detail"].lower()
        for item in ("blocked", "disabled", "live_trading_live_mode_enabled")
    )


def test_live_session_start_accepts_live_mode_when_flag_enabled(
    tmp_path: Path, monkeypatch
) -> None:
    app = create_dashboard_app(settings(tmp_path, live_mode_enabled=True))
    client_obj = LocalASGIClient(app)
    live_coordinator = app.state.live_coordinator

    def fake_start_session(config):
        assert config.trading_mode == "live"
        assert config.initial_balance == Decimal("0")
        assert config.use_ai is True
        assert config.label == "chan#live"
        return LiveSession(
            session_id="ls_live",
            channels=config.channels,
            channel_labels=["@chan"],
            trading_mode=config.trading_mode,
            initial_balance=config.initial_balance,
            risk_per_trade_pct=config.risk_per_trade_pct,
            strategy_key=config.strategy_key,
            use_ai=config.use_ai,
            interval=config.interval,
            label=config.label,
        )

    monkeypatch.setattr(live_coordinator, "start_session", fake_start_session)

    response = client_obj.post(
        "/api/live/sessions/start",
        headers=headers(),
        json={
            "channels": ["https://t.me/chan"],
            "trading_mode": "live",
            "risk_per_trade_pct": "120",
            "strategy_key": "tp_trailing_risk_managed",
            "use_ai": True,
        },
    )
    assert response.status_code == 202
    assert response.json()["session"]["trading_mode"] == "live"
    assert response.json()["session"]["label"] == "chan#live"


def test_live_session_start_rejects_multiple_channels(tmp_path: Path) -> None:
    response = client(tmp_path).post(
        "/api/live/sessions/start",
        headers=headers(),
        json={
            "channels": ["https://t.me/one", "https://t.me/two"],
            "trading_mode": "demo",
            "initial_balance": "100",
            "risk_per_trade_pct": "120",
            "strategy_key": "tp_trailing_risk_managed",
            "use_ai": False,
        },
    )
    assert response.status_code == 400
    assert "exactly one channel" in response.json()["detail"]


def test_live_overview_endpoint_returns_aggregate_payload(tmp_path: Path) -> None:
    app = create_dashboard_app(settings(tmp_path))
    client_obj = LocalASGIClient(app)
    live_coordinator = app.state.live_coordinator

    session = LiveSession(
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
        total_messages_processed=4,
    )
    live_coordinator.store.save_session(session)

    response = client_obj.get("/api/live/overview", headers=headers())
    assert response.status_code == 200
    payload = response.json()["overview"]
    assert payload["recent_sessions"][0]["session_id"] == "ls_one"
    assert payload["totals"]["messages_processed"] == 4


def test_live_overview_refreshes_active_exchange_states(
    tmp_path: Path, monkeypatch
) -> None:
    app = create_dashboard_app(settings(tmp_path))
    client_obj = LocalASGIClient(app)
    live_coordinator = app.state.live_coordinator
    refresh = AsyncMock()
    monkeypatch.setattr(live_coordinator, "refresh_active_exchange_states", refresh)

    response = client_obj.get("/api/live/overview", headers=headers())

    assert response.status_code == 200
    refresh.assert_awaited_once()


def test_live_session_detail_endpoint_is_session_specific(tmp_path: Path, monkeypatch) -> None:
    app = create_dashboard_app(settings(tmp_path))
    client_obj = LocalASGIClient(app)
    live_coordinator = app.state.live_coordinator

    session = LiveSession(
        session_id="ls_modal",
        channels=["https://t.me/modal"],
        channel_labels=["@modal"],
        trading_mode="demo",
        initial_balance=Decimal("100"),
        risk_per_trade_pct=Decimal("120"),
        strategy_key="tp_trailing_risk_managed",
        use_ai=False,
        interval="1m",
        status="running",
    )
    detail = LiveSessionDetail(
        session=session,
        messages=[
            LiveMessageTrace(
                session_id="ls_modal",
                message_id=99,
                channel_id="@modal",
                channel_label="@modal",
                preview_text="BUY BTC",
                message_date=session.started_at,
                final_status="opened_trade",
            )
        ],
    )
    monkeypatch.setattr(
        live_coordinator,
        "get_session_detail",
        lambda session_id: detail if session_id == "ls_modal" else None,
    )
    refresh_exchange_state = AsyncMock(return_value=True)
    monkeypatch.setattr(live_coordinator, "refresh_exchange_state", refresh_exchange_state)

    response = client_obj.get("/api/live/sessions/ls_modal", headers=headers())
    assert response.status_code == 200
    payload = response.json()["detail"]
    assert payload["session"]["session_id"] == "ls_modal"
    assert payload["messages"][0]["session_id"] == "ls_modal"
    refresh_exchange_state.assert_not_awaited()


def test_live_session_stop_requires_confirmation_for_open_trades(
    tmp_path: Path, monkeypatch
) -> None:
    app = create_dashboard_app(settings(tmp_path))
    client_obj = LocalASGIClient(app)
    live_coordinator = app.state.live_coordinator

    def blocked_stop(session_id: str, *, force: bool = False):
        assert session_id == "ls_open"
        assert force is False
        raise ValueError("Session stop is blocked while open or pending trades exist: BTCUSDT")

    monkeypatch.setattr(live_coordinator, "stop_session", blocked_stop)

    response = client_obj.post(
        "/api/live/sessions/ls_open/stop",
        headers=headers(),
        json={"force": False},
    )

    assert response.status_code == 409
    assert response.json()["confirmation_required"] is True
    assert "BTCUSDT" in response.json()["detail"]


def test_live_session_stop_passes_force_confirmation(tmp_path: Path, monkeypatch) -> None:
    app = create_dashboard_app(settings(tmp_path))
    client_obj = LocalASGIClient(app)
    live_coordinator = app.state.live_coordinator
    session = LiveSession(
        session_id="ls_force",
        channels=["https://t.me/demo"],
        trading_mode="live",
        initial_balance=Decimal("0"),
        risk_per_trade_pct=Decimal("120"),
        strategy_key="tp_trailing_risk_managed",
        use_ai=True,
        interval="1m",
        status="stopped",
    )

    def forced_stop(session_id: str, *, force: bool = False):
        assert session_id == "ls_force"
        assert force is True
        return session

    monkeypatch.setattr(live_coordinator, "stop_session", forced_stop)

    response = client_obj.post(
        "/api/live/sessions/ls_force/stop",
        headers=headers(),
        json={"force": True},
    )

    assert response.status_code == 200
    assert response.json()["stopped"] is True


def test_live_session_history_delete_endpoint(tmp_path: Path, monkeypatch) -> None:
    app = create_dashboard_app(settings(tmp_path))
    client_obj = LocalASGIClient(app)
    live_coordinator = app.state.live_coordinator
    monkeypatch.setattr(
        live_coordinator,
        "delete_session_history",
        lambda session_id: session_id == "ls_x",
    )

    response = client_obj.delete("/api/live/sessions/ls_x", headers=headers())
    assert response.status_code == 200
    assert response.json()["deleted"] is True


def test_live_session_history_delete_endpoint_rejects_running_session(
    tmp_path: Path, monkeypatch
) -> None:
    app = create_dashboard_app(settings(tmp_path))
    client_obj = LocalASGIClient(app)
    live_coordinator = app.state.live_coordinator

    def _raise(session_id: str) -> bool:
        raise ValueError("session_must_be_stopped")

    monkeypatch.setattr(live_coordinator, "delete_session_history", _raise)

    response = client_obj.delete("/api/live/sessions/ls_running", headers=headers())
    assert response.status_code == 409
    assert response.json()["detail"] == "session_must_be_stopped"


def test_live_telegram_forward_message_endpoint(tmp_path: Path, monkeypatch) -> None:
    app = create_dashboard_app(settings(tmp_path))
    client_obj = LocalASGIClient(app)
    live_coordinator = app.state.live_coordinator

    async def _forward_test_message(
        *,
        message_link: str,
        destination_channel: str,
    ) -> LiveMessageTrace:
        assert message_link == "https://t.me/kiwibot_log/14779"
        assert destination_channel == "@kiwibot_log"
        return LiveMessageTrace(
            session_id="telegram_test_forward",
            message_id=14780,
            channel_id="@kiwibot_log",
            channel_label="@kiwibot_log",
            preview_text="forwarded",
            full_text="forwarded",
            message_date=datetime.now(timezone.utc),
            final_status="forwarded_for_live_test",
        )

    monkeypatch.setattr(live_coordinator, "forward_test_message", _forward_test_message)

    response = client_obj.post(
        "/api/live/telegram/forward-message",
        headers=headers(),
        json={
            "message_link": "https://t.me/kiwibot_log/14779",
            "destination_channel": "@kiwibot_log",
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["forwarded"] is True
    assert payload["message"]["message_id"] == 14780


def test_live_trade_record_delete_endpoint(tmp_path: Path, monkeypatch) -> None:
    app = create_dashboard_app(settings(tmp_path))
    client_obj = LocalASGIClient(app)
    live_coordinator = app.state.live_coordinator
    monkeypatch.setattr(
        live_coordinator,
        "delete_trade_record",
        lambda session_id, trade_id: session_id == "ls_x" and trade_id == "tr_1",
    )

    response = client_obj.delete("/api/live/sessions/ls_x/trades/tr_1", headers=headers())
    assert response.status_code == 200
    assert response.json()["trade_id"] == "tr_1"


def test_live_message_record_delete_endpoint(tmp_path: Path, monkeypatch) -> None:
    app = create_dashboard_app(settings(tmp_path))
    client_obj = LocalASGIClient(app)
    live_coordinator = app.state.live_coordinator
    monkeypatch.setattr(
        live_coordinator,
        "delete_message_record",
        lambda session_id, message_id, channel_id: (
            session_id == "ls_x" and message_id == 10 and channel_id == "@chan"
        ),
    )

    response = client_obj.delete(
        "/api/live/sessions/ls_x/messages/10?channel_id=%40chan",
        headers=headers(),
    )
    assert response.status_code == 200
    assert response.json()["message_id"] == 10

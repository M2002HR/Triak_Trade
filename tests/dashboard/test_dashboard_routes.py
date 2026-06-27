from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from triak_trade.config.settings import Settings
from triak_trade.dashboard.app import create_dashboard_app
from triak_trade.dashboard.local_client import LocalASGIClient
from triak_trade.live_trading.models import (
    LiveMessageTrace,
    LiveSession,
    LiveSessionDetail,
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


def test_backtest_form_renders_tofan_default(tmp_path: Path) -> None:
    response = client(tmp_path).get("/backtests", headers=headers())
    assert response.status_code == 200
    assert "https://t.me/Tofan_Trade" in response.text
    assert "Start Backtest" in response.text
    assert "Live Telegram Backtest Monitor" in response.text
    assert "Saved Channels" in response.text
    assert "Save Once, Reuse Anytime" in response.text
    assert "Add Channel To Saved List" in response.text
    assert "Choose Execution Strategy" in response.text
    assert 'id="backtest-strategy-key"' in response.text
    assert "Load Into Form" in response.text
    assert 'id="backtest-saved-channel-select"' in response.text
    assert 'id="backtest-save-channel-input"' in response.text
    send_log_slice = response.text.split('id="backtest-send-log-channel"', 1)[1][:160]
    log_per_message_slice = response.text.split('id="backtest-log-per-message"', 1)[1][:160]
    assert "checked" in send_log_slice
    assert "checked" in log_per_message_slice


def test_logs_page_renders_log_channel_status(tmp_path: Path) -> None:
    response = client(tmp_path).get("/logs", headers=headers())
    assert response.status_code == 200
    assert "Telegram Log Channel" in response.text
    assert "@triak_logs" in response.text


def test_reports_page_handles_no_reports(tmp_path: Path) -> None:
    response = client(tmp_path).get("/reports", headers=headers())
    assert response.status_code == 200
    assert "No real backtest reports found" in response.text


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


def test_live_trading_page_is_english_only(tmp_path: Path) -> None:
    response = client(tmp_path).get("/live-trading", headers=headers())
    assert response.status_code == 200
    text = response.text
    assert "Run multiple independent sessions in parallel" in text
    assert "Account Information" in text
    assert "Concurrent Trading Sessions" in text
    assert "Incoming Messages" in text
    assert "اطلاعات" not in text
    assert "سشن" not in text


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

    response = client_obj.get("/api/live/sessions/ls_modal", headers=headers())
    assert response.status_code == 200
    payload = response.json()["detail"]
    assert payload["session"]["session_id"] == "ls_modal"
    assert payload["messages"][0]["session_id"] == "ls_modal"


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

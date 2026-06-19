from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from triak_trade.config.settings import Settings
from triak_trade.dashboard.app import create_dashboard_app


def settings(tmp_path: Path) -> Settings:
    runtime = tmp_path / "dashboard"
    return Settings(
        _env_file=None,
        DASHBOARD_ADMIN_TOKEN="test-token",
        DASHBOARD_SESSION_SECRET="session-secret",
        DASHBOARD_RUNTIME_DIR=str(runtime),
        DASHBOARD_PID_FILE=str(runtime / "dashboard.pid"),
        DASHBOARD_STATUS_FILE=str(runtime / "status.json"),
        DASHBOARD_LOG_FILE=str(runtime / "dashboard.log"),
        ROOT_ENV_FILE=str(tmp_path / ".env.local"),
        VERIFICATION_REPORT_DIR=str(tmp_path / "reports"),
        REAL_BACKTEST_REPORT_DIR=str(tmp_path / "backtests"),
    )


def client(tmp_path: Path) -> TestClient:
    return TestClient(create_dashboard_app(settings(tmp_path)))


def headers() -> dict[str, str]:
    return {"X-Triak-Admin-Token": "test-token"}


def test_dashboard_main_page_includes_status_cards(tmp_path: Path) -> None:
    response = client(tmp_path).get("/", headers=headers())
    assert response.status_code == 200
    assert "Admin Bot" in response.text
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


def test_approvals_page_renders_empty_state(tmp_path: Path) -> None:
    response = client(tmp_path).get("/approvals", headers=headers())
    assert response.status_code == 200
    assert "No pending proposed actions" in response.text


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
    assert "5m" in response.text

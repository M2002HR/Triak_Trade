from __future__ import annotations

import os
from pathlib import Path

from fastapi.testclient import TestClient

from triak_trade.config.settings import Settings
from triak_trade.dashboard.app import create_dashboard_app
from triak_trade.dashboard.runtime import (
    dashboard_safe_config,
    dashboard_smoke_test,
    dashboard_status,
    start_dashboard_process,
    stop_dashboard_process,
)
from triak_trade.dashboard.services import DashboardStateService


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
    )


def test_dashboard_safe_config_does_not_print_secrets(tmp_path: Path) -> None:
    payload = dashboard_safe_config(settings(tmp_path))
    assert payload["admin_token_present"] is True
    assert "test-token" not in str(payload)


def test_auto_mode_and_kill_switch_toggle_runtime_state(tmp_path: Path) -> None:
    service = DashboardStateService(settings(tmp_path))
    auto = service.set_auto_mode(enabled=True, updated_by="test", reason="testing")
    kill = service.set_kill_switch(enabled=True, updated_by="test", reason="maintenance")
    assert auto.enabled is True
    assert "future Risk Engine" in auto.reason or auto.reason == "testing"
    assert kill.enabled is True
    assert service.get_auto_mode().enabled is True
    assert service.get_kill_switch().reason == "maintenance"


def test_settings_page_does_not_show_secrets(tmp_path: Path) -> None:
    client = TestClient(create_dashboard_app(settings(tmp_path)))
    response = client.get("/settings", headers={"X-Triak-Admin-Token": "test-token"})
    assert response.status_code == 200
    assert "test-token" not in response.text
    assert "session-secret" not in response.text


def test_dashboard_runtime_duplicate_start_and_stop_safe(tmp_path: Path) -> None:
    cfg = settings(tmp_path)
    Path(cfg.DASHBOARD_RUNTIME_DIR).mkdir(parents=True, exist_ok=True)
    Path(cfg.DASHBOARD_PID_FILE).write_text(str(os.getpid()), encoding="utf-8")
    started = start_dashboard_process(cfg)
    Path(cfg.DASHBOARD_PID_FILE).unlink()
    stopped = stop_dashboard_process(cfg)
    assert started["already_running"] is True
    assert stopped["stopped"] is True


def test_dashboard_status_and_smoke_test(tmp_path: Path) -> None:
    cfg = settings(tmp_path)
    status = dashboard_status(cfg)
    smoke = dashboard_smoke_test(cfg)
    assert status["url"].startswith("http://")
    assert smoke["unauthorized_blocked"] is True
    assert smoke["dashboard_authorized"] is True
    assert smoke["backtest_fixture_ok"] is True

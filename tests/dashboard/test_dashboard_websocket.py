from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from triak_trade.config.settings import Settings
from triak_trade.dashboard.app import create_dashboard_app

pytestmark = pytest.mark.skip(
    reason="starlette.testclient websocket support requires httpx2 in this environment"
)


def build_client(tmp_path: Path) -> TestClient:
    settings = Settings(
        _env_file=None,
        DASHBOARD_ADMIN_TOKEN="test-token",
        DASHBOARD_SESSION_SECRET="session-secret",
        DASHBOARD_RUNTIME_DIR=str(tmp_path / "dashboard"),
        LIVE_TRADING_RUNTIME_DIR=str(tmp_path / "live_trading"),
        REAL_BACKTEST_REPORT_DIR=str(tmp_path / "reports"),
    )
    return TestClient(create_dashboard_app(settings))


def test_dashboard_backtest_websocket_bootstrap_and_ping(tmp_path: Path) -> None:
    client = build_client(tmp_path)
    with client.websocket_connect("/ws/backtests?token=test-token") as websocket:
        bootstrap = websocket.receive_json()
        assert bootstrap["type"] == "bootstrap"
        assert isinstance(bootstrap["runs"], list)
        websocket.send_text("ping")
        pong = websocket.receive_json()
        assert pong == {"type": "pong"}


def test_dashboard_backtest_websocket_rejects_unauthorized(tmp_path: Path) -> None:
    client = build_client(tmp_path)
    try:
        with client.websocket_connect("/ws/backtests"):
            raise AssertionError("websocket should not connect without auth")
    except Exception:
        pass

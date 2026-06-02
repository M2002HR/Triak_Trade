from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from triak_trade.config.settings import Settings
from triak_trade.dashboard.app import create_dashboard_app


def build_client(tmp_path: Path, *, real_guard: int = 0) -> TestClient:
    settings = Settings(
        _env_file=None,
        DASHBOARD_ADMIN_TOKEN="test-token",
        DASHBOARD_SESSION_SECRET="session-secret",
        RUN_BACKTEST_INTEGRATION_TESTS=real_guard,
        DASHBOARD_RUNTIME_DIR=str(tmp_path / "dashboard"),
    )
    return TestClient(create_dashboard_app(settings))


def test_backtest_fixture_post_runs_summary(tmp_path: Path) -> None:
    response = build_client(tmp_path).post(
        "/backtests/run",
        headers={"X-Triak-Admin-Token": "test-token"},
        data={
            "channel": "https://t.me/Tofan_Trade",
            "interval": "1m",
            "initial_balance": "1000",
            "risk_per_trade_pct": "1",
            "fill_policy": "conservative",
        },
    )
    assert response.status_code == 200
    assert "Backtest Result" in response.text
    assert "total_pnl" in response.text


def test_real_backtest_route_blocked_without_guard(tmp_path: Path) -> None:
    response = build_client(tmp_path).post(
        "/backtests/run",
        headers={"X-Triak-Admin-Token": "test-token"},
        data={
            "channel": "https://t.me/Tofan_Trade",
            "interval": "1m",
            "initial_balance": "1000",
            "risk_per_trade_pct": "1",
            "fill_policy": "conservative",
            "real_mode": "on",
        },
    )
    assert response.status_code == 200
    assert "Blocked" in response.text
    assert "Real backtest guard is disabled" in response.text

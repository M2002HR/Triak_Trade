from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient

from triak_trade.backtesting.real_runner import RealBacktestResult
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
    assert "Legacy Result" in response.text
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
    assert "Real backtest is not ready" in response.text


def test_real_backtest_route_runs_with_fake_runner(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class FakeRunner:
        def __init__(self, settings: Settings) -> None:
            self.settings = settings

        def readiness(self) -> object:
            class Readiness:
                def __init__(self) -> None:
                    self.ready = True
                    self.issues: list[str] = []

                def model_dump(self, mode: str = "json") -> dict[str, object]:
                    return {"ready": True, "issues": []}

            return Readiness()

        def run_sync(self, request: object) -> RealBacktestResult:
            return RealBacktestResult(
                success=True,
                channel="https://t.me/Tofan_Trade",
                from_date=datetime(2026, 6, 1, tzinfo=timezone.utc),
                to_date=datetime(2026, 6, 2, tzinfo=timezone.utc),
                interval="1m",
                real_telegram_used=True,
                real_market_data_used=True,
                ai_used=False,
                regex_fallback_used=True,
                total_messages=10,
                classified_messages=10,
                parsed_signals=2,
                valid_signals=1,
                invalid_signals=1,
                ignored_messages=7,
                ambiguous_messages=0,
                symbols_found=["BTCUSDT"],
                candles_fetched=100,
                trades_simulated=1,
                trades_filled=1,
                wins=1,
                losses=0,
                win_rate=Decimal("1"),
                total_pnl=Decimal("25"),
                profit_factor=Decimal("2"),
                max_drawdown=Decimal("5"),
                conservative_pnl=Decimal("20"),
                optimistic_pnl=Decimal("30"),
                channel_score=Decimal("75"),
                generated_at=datetime(2026, 6, 2, tzinfo=timezone.utc),
                report_path="runtime/reports/backtests/report.json",
                markdown_report_path="runtime/reports/backtests/report.md",
            )

    monkeypatch.setattr("triak_trade.dashboard.services.RealBacktestRunner", FakeRunner)

    response = build_client(tmp_path).post(
        "/backtests/run",
        headers={"X-Triak-Admin-Token": "test-token"},
        data={
            "channel": "https://t.me/Tofan_Trade",
            "interval": "1m",
            "real_mode": "on",
            "lookback_hours": "24",
            "max_messages": "1000",
        },
    )
    assert response.status_code == 200
    assert "real_telegram_used" in response.text
    assert "runtime/reports/backtests/report.json" in response.text

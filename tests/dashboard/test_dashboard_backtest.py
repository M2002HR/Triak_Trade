from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from triak_trade.backtesting.isolated_runner import IsolatedBacktestRunRequest
from triak_trade.backtesting.real_runner import RealBacktestResult
from triak_trade.config.settings import Settings
from triak_trade.dashboard.backtest_runtime import DashboardBacktestRun
from triak_trade.dashboard.services import DashboardService


def build_settings(tmp_path: Path, *, real_guard: int = 0) -> Settings:
    return Settings(
        _env_file=None,
        DATABASE_URL=f"sqlite+pysqlite:///{tmp_path / 'dashboard.db'}",
        TEST_DATABASE_URL=f"sqlite+pysqlite:///{tmp_path / 'dashboard_test.db'}",
        DASHBOARD_ADMIN_TOKEN="test-token",
        DASHBOARD_SESSION_SECRET="session-secret",
        RUN_BACKTEST_INTEGRATION_TESTS=real_guard,
        DASHBOARD_RUNTIME_DIR=str(tmp_path / "dashboard"),
        LIVE_TRADING_RUNTIME_DIR=str(tmp_path / "live_trading"),
    )


def test_backtest_fixture_run_returns_summary(tmp_path: Path) -> None:
    result = DashboardService(build_settings(tmp_path)).run_fixture_backtest_from_form(
        {
            "channel": "https://t.me/Tofan_Trade",
            "interval": "1m",
            "initial_balance": "1000",
            "risk_per_trade_pct": "1",
            "fill_policy": "conservative",
        }
    )
    assert result["blocked"] is False
    assert "summary" in result
    assert "total_pnl" in result["summary"]


def test_real_backtest_service_blocked_without_guard(tmp_path: Path) -> None:
    result = DashboardService(build_settings(tmp_path)).run_fixture_backtest_from_form(
        {
            "channel": "https://t.me/Tofan_Trade",
            "interval": "1m",
            "initial_balance": "1000",
            "risk_per_trade_pct": "1",
            "fill_policy": "conservative",
            "real_mode": "on",
        }
    )
    assert result["blocked"] is True
    assert result["reason"] == "Real backtest is not ready."


def test_real_backtest_service_runs_with_fake_runner(
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

    result = DashboardService(build_settings(tmp_path)).run_fixture_backtest_from_form(
        {
            "channel": "https://t.me/Tofan_Trade",
            "interval": "1m",
            "real_mode": "on",
            "lookback_hours": "24",
            "max_messages": "1000",
        }
    )
    assert result["blocked"] is False
    assert result["summary"]["real_telegram_used"] is True
    assert result["summary"]["report_path"] == "runtime/reports/backtests/report.json"


def test_dashboard_service_parses_naive_datetime_as_tehran_utc() -> None:
    parsed = DashboardService._parse_datetime("2026-06-04T15:30:00")

    assert parsed is not None
    assert parsed.isoformat() == "2026-06-04T12:00:00+00:00"


def test_dashboard_service_builds_isolated_backtest_request(tmp_path: Path) -> None:
    service = DashboardService(build_settings(tmp_path, real_guard=1))
    captured: dict[str, object] = {}

    def _start_run(request, *, channel_input: str, strategy_key: str, run_type: str):
        captured["request"] = request
        captured["channel_input"] = channel_input
        captured["run_type"] = run_type
        return DashboardBacktestRun(
            run_id="backtest_demo",
            run_type=run_type,
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
    service.real_backtest_readiness = lambda: {"ready": True, "issues": []}  # type: ignore[assignment]

    result = service.start_live_backtest(
        {
            "run_type": "isolated",
            "channel": "https://t.me/Tofan_Trade",
            "from_date": "2026-06-04T15:30:00+03:30",
            "to_date": "2026-06-04T18:30:00+03:30",
            "interval": "1m",
            "max_messages": 50,
            "initial_balance": "100",
            "capital_per_signal": "150",
            "risk_per_trade_pct": "120",
            "fill_policy": "conservative",
            "leverage_source": "fixed",
            "fixed_leverage": "8",
            "use_ai": False,
            "send_log_channel": False,
            "log_per_message": False,
        }
    )

    assert result["started"] is True
    assert captured["run_type"] == "isolated"
    assert isinstance(captured["request"], IsolatedBacktestRunRequest)
    request = captured["request"]
    assert isinstance(request, IsolatedBacktestRunRequest)
    assert request.capital_per_signal == Decimal("150")
    assert request.leverage_source == "fixed"
    assert request.fixed_leverage == 8

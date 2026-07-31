from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from triak_trade.backtesting.backtest_runner import BacktestRunRequest
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


def test_dashboard_service_parses_naive_datetime_as_tehran_utc() -> None:
    parsed = DashboardService._parse_datetime("2026-06-04T15:30:00")

    assert parsed is not None
    assert parsed.isoformat() == "2026-06-04T12:00:00+00:00"


def test_dashboard_service_builds_backtest_request(tmp_path: Path) -> None:
    service = DashboardService(build_settings(tmp_path, real_guard=1))
    captured: dict[str, object] = {}

    def _start_run(request, *, channel_input: str, strategy_key: str):
        captured["request"] = request
        captured["channel_input"] = channel_input
        return DashboardBacktestRun(
            run_id="backtest_demo",
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
    service.backtest_readiness = lambda: {"ready": True, "issues": []}  # type: ignore[assignment]

    result = service.start_live_backtest(
        {
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
    assert isinstance(captured["request"], BacktestRunRequest)
    request = captured["request"]
    assert isinstance(request, BacktestRunRequest)
    assert request.capital_per_signal == Decimal("150")
    assert request.leverage_source == "fixed"
    assert request.fixed_leverage == 8

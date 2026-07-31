from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from typer.testing import CliRunner

from triak_trade.backtesting.backtest_runner import (
    BacktestResult,
    BacktestRunRequest,
)
from triak_trade.cli import app
from triak_trade.config.settings import Settings

runner = CliRunner()


class _FakeRunner:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.last_request: object | None = None

    def readiness(self) -> object:
        class _Readiness:
            def model_dump(self, mode: str = "json") -> dict[str, object]:
                return {
                    "ready": True,
                    "issues": [],
                    "real_backtest_enabled": True,
                    "telegram_credentials_present": True,
                    "telegram_session_configured": True,
                    "toobit_public_market_ready": True,
                    "ai_gateway_enabled": False,
                    "regex_fallback_enabled": True,
                    "report_dir": "runtime/reports/backtests",
                    "log_channel_enabled": False,
                }

        return _Readiness()

    def run_sync(self, request: object) -> object:
        self.last_request = request
        return BacktestResult(
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
            channel_score=Decimal("68"),
            generated_at=datetime(2026, 6, 2, tzinfo=timezone.utc),
            report_path="runtime/reports/backtests/report.json",
            markdown_report_path="runtime/reports/backtests/report.md",
        )

    def latest_report_summary(self) -> dict[str, object] | None:
        return {
            "channel": "https://t.me/Tofan_Trade",
            "real_telegram_used": True,
            "real_market_data_used": True,
            "report_path": "runtime/reports/backtests/report.json",
        }


class _FakeBacktestRunner(_FakeRunner):
    def run_sync(self, request: object) -> object:
        self.last_request = request
        return BacktestResult(
            success=True,
            channel="https://t.me/Tofan_Trade",
            from_date=datetime(2026, 6, 1, tzinfo=timezone.utc),
            to_date=datetime(2026, 6, 2, tzinfo=timezone.utc),
            interval="1m",
            real_telegram_used=True,
            real_market_data_used=True,
            ai_used=False,
            regex_fallback_used=True,
            total_messages=12,
            classified_messages=12,
            parsed_signals=3,
            valid_signals=3,
            invalid_signals=0,
            ignored_messages=9,
            ambiguous_messages=0,
            ai_failed_messages=0,
            symbols_found=["BTCUSDT", "ETHUSDT"],
            candles_fetched=240,
            trades_simulated=3,
            trades_filled=2,
            wins=1,
            losses=1,
            win_rate=Decimal("0.5"),
            total_pnl=Decimal("12.5"),
            profit_factor=Decimal("1.8"),
            max_drawdown=Decimal("4"),
            conservative_pnl=Decimal("12.5"),
            optimistic_pnl=Decimal("12.5"),
            channel_score=Decimal("0"),
            generated_at=datetime(2026, 6, 2, tzinfo=timezone.utc),
            report_path="runtime/reports/backtests/backtest.report.json",
            markdown_report_path="runtime/reports/backtests/backtest.report.md",
            aggregate={
                "total_signals": 3,
                "filled_signals": 2,
                "closed_signals": 2,
                "open_signals": 1,
                "wins": 1,
                "losses": 1,
                "total_pnl": "12.5",
            },
            signals=[
                {
                    "signal_id": "sig_iso_1",
                    "symbol": "BTCUSDT",
                    "status": "tp2_hit",
                    "total_pnl": "15",
                },
                {
                    "signal_id": "sig_iso_2",
                    "symbol": "ETHUSDT",
                    "status": "stop_loss_hit",
                    "total_pnl": "-2.5",
                },
            ],
        )


def test_backtest_cli_commands_with_fake_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_runner = _FakeBacktestRunner(Settings(_env_file=None))
    monkeypatch.setattr(
        "triak_trade.cli._build_backtest_runner",
        lambda settings: fake_runner,
    )

    check = runner.invoke(app, ["backtest-check"])
    assert check.exit_code == 0
    assert '"ready": true' in check.stdout

    run = runner.invoke(
        app,
        [
            "backtest-run",
            "--channel",
            "https://t.me/Tofan_Trade",
            "--from",
            "2026-06-04T15:30:00",
            "--to",
            "2026-06-04T16:30:00",
            "--interval",
            "5m",
            "--max-messages",
            "250",
            "--initial-balance",
            "500",
            "--risk-per-trade-pct",
            "2.5",
            "--capital-per-signal",
            "125",
            "--fill-policy",
            "optimistic",
            "--leverage-source",
            "fixed",
            "--fixed-leverage",
            "12",
            "--max-effective-leverage",
            "30",
            "--default-signal-leverage",
            "8",
            "--min-allocation-pct",
            "3",
            "--max-allocation-pct",
            "18",
            "--default-stop-pct",
            "4",
            "--synthetic-stop-max-loss-pct",
            "6",
            "--fee-rate-pct",
            "0.1",
            "--lifecycle-refresh-interval",
            "15m",
            "--max-parallel-signals",
            "6",
            "--exclude-not-filled-signals",
            "--leave-open-positions-at-end",
            "--no-send-telegram-summary",
            "--no-send-log-channel",
            "--no-ai",
            "--include-signals",
        ],
    )
    assert run.exit_code == 0
    assert '"run_type": "backtest"' in run.stdout
    assert '"report_path": "runtime/reports/backtests/backtest.report.json"' in run.stdout
    assert '"signals": [' in run.stdout

    request = fake_runner.last_request
    assert isinstance(request, BacktestRunRequest)
    assert request.from_date is not None
    assert request.to_date is not None
    assert request.from_date.isoformat() == "2026-06-04T12:00:00+00:00"
    assert request.to_date.isoformat() == "2026-06-04T13:00:00+00:00"
    assert request.interval == "5m"
    assert request.max_messages == 250
    assert request.initial_balance == Decimal("500")
    assert request.risk_per_trade_pct == Decimal("2.5")
    assert request.capital_per_signal == Decimal("125")
    assert request.fill_policy.value == "optimistic"
    assert request.leverage_source == "fixed"
    assert request.fixed_leverage == 12
    assert request.max_effective_leverage == Decimal("30")
    assert request.default_signal_leverage == Decimal("8")
    assert request.min_allocation_pct == Decimal("3")
    assert request.max_allocation_pct == Decimal("18")
    assert request.default_stop_pct == Decimal("4")
    assert request.synthetic_stop_max_loss_pct_of_balance == Decimal("6")
    assert request.fee_rate_pct == Decimal("0.1")
    assert request.lifecycle_refresh_interval == "15m"
    assert request.max_parallel_signals == 6
    assert request.include_not_filled_signals is False
    assert request.close_open_positions_at_end is False

    default = runner.invoke(
        app,
        ["backtest-tofan", "--hours", "24", "--no-ai", "--include-signals"],
    )
    assert default.exit_code == 0
    assert '"channel": "https://t.me/Tofan_Trade"' in default.stdout

    latest = runner.invoke(app, ["backtest-show-latest"])
    assert latest.exit_code == 0
    assert '"report_path": "runtime/reports/backtests/report.json"' in latest.stdout


def test_backtest_cli_uses_cpu_count_for_default_parallel_workers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(_env_file=None)
    fake_runner = _FakeBacktestRunner(settings)
    monkeypatch.setattr("triak_trade.cli._load_settings", lambda: settings)
    monkeypatch.setattr(
        "triak_trade.cli._build_backtest_runner",
        lambda _settings: fake_runner,
    )
    monkeypatch.setattr("triak_trade.cli.default_backtest_parallel_workers", lambda: 12)

    run = runner.invoke(
        app,
        [
            "backtest-run",
            "--channel",
            "https://t.me/Tofan_Trade",
            "--hours",
            "1",
            "--interval",
            "1m",
            "--max-messages",
            "10",
            "--no-send-telegram-summary",
            "--no-send-log-channel",
            "--no-ai",
        ],
    )

    assert run.exit_code == 0
    request = fake_runner.last_request
    assert isinstance(request, BacktestRunRequest)
    assert request.max_parallel_signals == 12


def test_backtest_cli_rejects_fixed_leverage_without_a_value() -> None:
    result = runner.invoke(
        app,
        [
            "backtest-run",
            "--channel",
            "https://t.me/Tofan_Trade",
            "--hours",
            "1",
            "--leverage-source",
            "fixed",
            "--no-ai",
            "--no-send-log-channel",
            "--no-send-telegram-summary",
        ],
    )

    assert result.exit_code == 2
    assert "--fixed-leverage" in result.output
    assert "must be a positive integer" in result.output

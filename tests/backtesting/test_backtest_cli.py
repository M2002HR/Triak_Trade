from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from typer.testing import CliRunner

from triak_trade.backtesting.real_runner import RealBacktestResult
from triak_trade.cli import app
from triak_trade.config.settings import Settings

runner = CliRunner()


def test_backtest_cli_fixture_and_dry_run() -> None:
    fixture = runner.invoke(app, ["backtest-fixture"])
    assert fixture.exit_code == 0
    assert "Backtest Report" in fixture.stdout

    dry = runner.invoke(
        app,
        [
            "backtest-dry-run",
            "--channel",
            "https://t.me/Tofan_Trade",
            "--from",
            "2026-06-01",
            "--to",
            "2026-06-02",
            "--interval",
            "1m",
        ],
    )
    assert dry.exit_code == 0
    assert '"channel": "https://t.me/Tofan_Trade"' in dry.stdout


def test_backtest_cli_real_guarded() -> None:
    blocked = runner.invoke(
        app,
        [
            "backtest-dry-run",
            "--channel",
            "https://t.me/Tofan_Trade",
            "--from",
            "2026-06-01",
            "--to",
            "2026-06-02",
            "--interval",
            "1m",
            "--real",
        ],
    )
    assert blocked.exit_code == 2


class _FakeRunner:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

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


def test_real_backtest_cli_commands_with_fake_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "triak_trade.cli._build_real_backtest_runner",
        lambda settings: _FakeRunner(settings),
    )

    check = runner.invoke(app, ["real-backtest-check"])
    assert check.exit_code == 0
    assert '"ready": true' in check.stdout

    run = runner.invoke(
        app,
        [
            "real-backtest-run",
            "--channel",
            "https://t.me/Tofan_Trade",
            "--hours",
            "24",
            "--interval",
            "1m",
            "--max-messages",
            "1000",
            "--no-send-telegram-summary",
            "--no-send-log-channel",
            "--no-ai",
        ],
    )
    assert run.exit_code == 0
    assert '"real_telegram_used": true' in run.stdout
    assert '"report_path": "runtime/reports/backtests/report.json"' in run.stdout

    default = runner.invoke(app, ["real-backtest-tofan", "--hours", "24", "--no-ai"])
    assert default.exit_code == 0
    assert '"channel": "https://t.me/Tofan_Trade"' in default.stdout

    latest = runner.invoke(app, ["backtest-show-latest"])
    assert latest.exit_code == 0
    assert '"report_path": "runtime/reports/backtests/report.json"' in latest.stdout

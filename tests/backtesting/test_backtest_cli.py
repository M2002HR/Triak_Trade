from __future__ import annotations

from typer.testing import CliRunner

from triak_trade.cli import app

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

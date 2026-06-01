from __future__ import annotations

from typer.testing import CliRunner

from triak_trade.cli import app

runner = CliRunner()


def test_cli_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == "0.1.0"


def test_cli_health() -> None:
    result = runner.invoke(app, ["health"])
    assert result.exit_code == 0
    assert '"status": "ok"' in result.stdout
    assert '"config": "ok"' in result.stdout


def test_cli_config_check() -> None:
    result = runner.invoke(app, ["config-check"])
    assert result.exit_code == 0
    assert "Configuration is valid" in result.stdout

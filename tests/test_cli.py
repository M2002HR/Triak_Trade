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


def test_cli_db_check() -> None:
    result = runner.invoke(app, ["db-check"])
    assert result.exit_code == 0
    assert "DB engine configured" in result.stdout


def test_cli_parse_message_valid() -> None:
    result = runner.invoke(
        app,
        [
            "parse-message",
            "BTCUSDT LONG Entry: 68000 - 68200 SL: 67400 TP: 69000 / 70000 Leverage: 5x",
        ],
    )
    assert result.exit_code == 0
    assert '"action": "open"' in result.stdout
    assert '"symbol": "BTCUSDT"' in result.stdout
    assert '"side": "long"' in result.stdout


def test_cli_parse_message_ambiguous() -> None:
    result = runner.invoke(app, ["parse-message", "BTC looking good"])
    assert result.exit_code == 0
    assert '"proposal_valid": false' in result.stdout
    assert "validation_errors" in result.stdout

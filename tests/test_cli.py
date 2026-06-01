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


def test_cli_agent_dry_run() -> None:
    result = runner.invoke(app, ["agent-dry-run"])
    assert result.exit_code == 0
    assert '"tick_actions"' in result.stdout
    assert '"action_type": "create_order"' in result.stdout


def test_cli_ai_classify_dry_run_signal() -> None:
    result = runner.invoke(
        app,
        [
            "ai-classify-dry-run",
            "BTCUSDT LONG Entry: 68000 - 68200 SL: 67400 TP: 69000 / 70000",
        ],
    )
    assert result.exit_code == 0
    assert '"real_gateway_used": false' in result.stdout
    assert '"parsed_action": "open"' in result.stdout


def test_cli_ai_classify_dry_run_profit_and_ad() -> None:
    profit = runner.invoke(app, ["ai-classify-dry-run", "TP1 hit ✅ +120% profit"])
    assert profit.exit_code == 0
    assert '"parsed_action": "ignore"' in profit.stdout

    ad = runner.invoke(app, ["ai-classify-dry-run", "This is a promo giveaway join now"])
    assert ad.exit_code == 0
    assert '"parsed_action": "ignore"' in ad.stdout

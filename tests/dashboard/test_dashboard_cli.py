from __future__ import annotations

from typer.testing import CliRunner

from triak_trade.cli import app

runner = CliRunner()


def test_dashboard_check_does_not_print_secrets() -> None:
    result = runner.invoke(app, ["dashboard-check"])
    assert result.exit_code == 0
    assert "admin_token_present" in result.stdout
    assert "DASHBOARD_ADMIN_TOKEN" not in result.stdout


def test_dashboard_smoke_test_passes() -> None:
    result = runner.invoke(app, ["dashboard-smoke-test"])
    assert result.exit_code == 0
    assert '"unauthorized_blocked": true' in result.stdout
    assert '"dashboard_authorized": true' in result.stdout
    assert '"status_api_unauthorized": true' in result.stdout


def test_dashboard_token_hint_does_not_print_token() -> None:
    result = runner.invoke(app, ["dashboard-token-hint"])
    assert result.exit_code == 0
    assert "DASHBOARD_ADMIN_TOKEN is in root .env.local" in result.stdout

from __future__ import annotations

from typer.testing import CliRunner

from triak_trade.admin_bot.auth import AdminAuthService
from triak_trade.admin_bot.service import AdminApprovalService
from triak_trade.admin_bot.telegram_bot import TelegramAdminBot
from triak_trade.cli import app
from triak_trade.config.settings import Settings

runner = CliRunner()


def test_admin_backtest_menu_and_progress_flow() -> None:
    service = AdminApprovalService(
        auth=AdminAuthService(["@we_are_waiting_for_him"]),
        bot=TelegramAdminBot(bot_token="x", parse_mode="HTML", disable_web_preview=True),
    )
    menu = service.backtest_menu("@we_are_waiting_for_him")
    run = service.run_backtest_dry("@we_are_waiting_for_him")
    assert "📊 Backtest Menu" in menu["text"]
    assert "backtest:run" in menu["callbacks"]
    assert run["progress"][0].startswith("🔎")


def test_admin_real_backtest_service_blocked_without_settings() -> None:
    service = AdminApprovalService(
        auth=AdminAuthService(["@we_are_waiting_for_him"]),
        bot=TelegramAdminBot(bot_token="x", parse_mode="HTML", disable_web_preview=True),
        settings=Settings(_env_file=None),
    )
    result = service.run_real_backtest("@we_are_waiting_for_him", hours=24)
    assert result["blocked"] is True


def test_admin_backtest_dry_run_cli_authorized_and_unauthorized() -> None:
    ok = runner.invoke(app, ["admin-backtest-dry-run", "--username", "@we_are_waiting_for_him"])
    assert ok.exit_code == 0
    assert "Backtest Menu" in ok.stdout
    assert "Backtest Report" in ok.stdout

    bad = runner.invoke(app, ["admin-backtest-dry-run", "--username", "@not_allowed"])
    assert bad.exit_code == 2

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from triak_trade.cli import app
from triak_trade.config.settings import get_settings

runner = CliRunner()


def test_admin_check_no_token_print() -> None:
    result = runner.invoke(app, ["admin-check"])
    assert result.exit_code == 0
    assert "bot_token_present" in result.stdout
    assert "replace_me" not in result.stdout


def test_admin_format_dry_run_works() -> None:
    result = runner.invoke(app, ["admin-format-dry-run"])
    assert result.exit_code == 0
    assert "Demo only / no live execution" in result.stdout


def test_admin_callback_dry_run_auth_and_unauth() -> None:
    ok = runner.invoke(
        app,
        [
            "admin-callback-dry-run",
            "admin:approve:test_action_123",
            "--username",
            "@we_are_waiting_for_him",
        ],
    )
    assert ok.exit_code == 0
    assert '"decision": "approve"' in ok.stdout

    bad = runner.invoke(
        app,
        [
            "admin-callback-dry-run",
            "admin:approve:test_action_123",
            "--username",
            "@not_allowed",
        ],
    )
    assert bad.exit_code != 0


def test_admin_send_test_blocked_without_guard() -> None:
    result = runner.invoke(app, ["admin-send-test"])
    assert result.exit_code == 2


def test_admin_bot_smoke_test_cli_works() -> None:
    result = runner.invoke(app, ["admin-bot-smoke-test"])
    assert result.exit_code == 0
    assert '"mode": "fake-smoke"' in result.stdout
    assert "TELEGRAM_BOT_TOKEN" not in result.stdout
    assert "replace_me" not in result.stdout


def test_run_admin_bot_once_cli_uses_fake_mode() -> None:
    result = runner.invoke(app, ["run-admin-bot", "--once"])
    assert result.exit_code == 0
    assert '"mode": "fake"' in result.stdout
    assert '"real": false' in result.stdout


def test_admin_bot_status_and_logs_cli_work() -> None:
    status = runner.invoke(app, ["admin-bot-status"])
    logs = runner.invoke(app, ["admin-bot-logs", "--lines", "5"])

    assert status.exit_code == 0
    assert "status_file" in status.stdout
    assert logs.exit_code == 0
    assert "TELEGRAM_BOT_TOKEN" not in status.stdout + logs.stdout


def test_run_admin_bot_real_mode_blocked_without_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_BOT_RUNTIME_ENABLED", "false")
    get_settings.cache_clear()
    result = runner.invoke(app, ["run-admin-bot", "--real", "--once"])
    get_settings.cache_clear()
    assert result.exit_code != 0
    assert "ADMIN_BOT_RUNTIME_ENABLED" in result.output

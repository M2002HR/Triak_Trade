from __future__ import annotations

from typer.testing import CliRunner

from triak_trade.cli import app

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

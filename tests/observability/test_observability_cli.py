from __future__ import annotations

import pytest
from typer.testing import CliRunner

from triak_trade.cli import app
from triak_trade.config.settings import get_settings

runner = CliRunner()


def test_log_channel_check_does_not_print_token() -> None:
    result = runner.invoke(app, ["log-channel-check"])

    assert result.exit_code == 0
    assert "log_channel_username" in result.stdout
    assert "TELEGRAM_BOT_TOKEN" not in result.stdout
    assert "bot_token_present" in result.stdout


def test_log_channel_format_dry_run_prints_english_report() -> None:
    result = runner.invoke(app, ["log-channel-format-dry-run"])

    assert result.exit_code == 0
    assert "Message Processing Report" in result.stdout
    assert "Source: @Tofan_Trade" in result.stdout
    assert "Classification:" in result.stdout
    assert "Decision:" in result.stdout


def test_log_channel_send_test_blocked_without_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_LOG_CHANNEL_ENABLED", "false")
    monkeypatch.setenv("PROCESSING_AUDIT_SEND_TO_LOG_CHANNEL", "false")
    monkeypatch.setenv("RUN_TELEGRAM_LOG_CHANNEL_INTEGRATION_TESTS", "0")
    get_settings.cache_clear()

    result = runner.invoke(app, ["log-channel-send-test"])

    get_settings.cache_clear()
    assert result.exit_code != 0
    assert "Blocked by default" in result.output


def test_process_message_audit_dry_run_works() -> None:
    result = runner.invoke(app, ["process-message-audit-dry-run"])

    assert result.exit_code == 0
    assert "formatted_message" in result.stdout
    assert "NEW_SIGNAL" in result.stdout
    assert "pending_consolidation" in result.stdout

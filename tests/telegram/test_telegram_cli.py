from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from triak_trade.cli import app
from triak_trade.config.settings import Settings

runner = CliRunner()


def test_telegram_check_masks_secrets() -> None:
    result = runner.invoke(app, ["telegram-check"])
    assert result.exit_code == 0
    assert "api_id_present" in result.stdout
    assert "replace_me" not in result.stdout


def test_telegram_history_dry_run_fake_mode() -> None:
    result = runner.invoke(
        app,
        ["telegram-history-dry-run", "https://t.me/Tofan_Trade", "--limit", "5"],
    )
    assert result.exit_code == 0
    assert '"mode": "fake"' in result.stdout
    assert '"count": 5' in result.stdout


def test_telegram_history_dry_run_real_guarded() -> None:
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        "triak_trade.cli._load_settings",
        lambda: Settings(_env_file=None, RUN_TELEGRAM_INTEGRATION_TESTS=0),
    )
    result = runner.invoke(
        app,
        ["telegram-history-dry-run", "https://t.me/Tofan_Trade", "--limit", "1", "--real"],
    )
    assert result.exit_code == 2
    monkeypatch.undo()


def test_telegram_tofan_dry_run_guarded() -> None:
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        "triak_trade.cli._load_settings",
        lambda: Settings(_env_file=None, RUN_TELEGRAM_INTEGRATION_TESTS=0),
    )
    result = runner.invoke(app, ["telegram-tofan-dry-run", "--limit", "5", "--real"])
    assert result.exit_code == 2
    monkeypatch.undo()


def test_session_files_are_gitignored() -> None:
    content = Path(".gitignore").read_text(encoding="utf-8")
    assert ".sessions/" in content
    assert "*.session" in content

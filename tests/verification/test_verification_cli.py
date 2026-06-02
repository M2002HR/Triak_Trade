from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

import triak_trade.cli as cli_module
from triak_trade.cli import app
from triak_trade.config.settings import Settings

runner = CliRunner()


def test_verify_system_safe_cli(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    settings = Settings(VERIFICATION_REPORT_DIR=str(tmp_path))
    monkeypatch.setattr(cli_module, "get_settings", lambda: settings)
    result = runner.invoke(app, ["verify-system", "--mode", "safe", "--write-report"])
    assert result.exit_code == 0
    assert "Triak_Trade System Verification" in result.stdout
    assert list(Path(tmp_path).glob("*.report.md"))
    assert "replace_me" not in result.stdout


def test_verify_real_guarded_cli(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    settings = Settings(VERIFICATION_REPORT_DIR=str(tmp_path), RUN_SYSTEM_REAL_SMOKE_TESTS=0)
    monkeypatch.setattr(cli_module, "get_settings", lambda: settings)
    result = runner.invoke(app, ["verify-real"])
    assert result.exit_code == 0
    assert "Real smoke guard is disabled" in result.stdout


def test_show_last_report_handles_none_and_existing(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    settings = Settings(VERIFICATION_REPORT_DIR=str(tmp_path))
    monkeypatch.setattr(cli_module, "get_settings", lambda: settings)
    none = runner.invoke(app, ["show-last-report"])
    assert none.exit_code == 0
    assert "No verification reports found" in none.stdout

    (Path(tmp_path) / "x.report.md").write_text("# Report\nok", encoding="utf-8")
    got = runner.invoke(app, ["show-last-report"])
    assert got.exit_code == 0
    assert "x.report.md" in got.stdout

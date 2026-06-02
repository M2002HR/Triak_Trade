from __future__ import annotations

import pytest
from typer.testing import CliRunner

from triak_trade.cli import app
from triak_trade.config.settings import Settings

runner = CliRunner()


def test_market_data_dry_run_fake_safe() -> None:
    result = runner.invoke(
        app,
        ["market-data-dry-run", "BTCUSDT", "--interval", "1m", "--minutes", "5"],
    )
    assert result.exit_code == 0
    assert '"candle_count": 5' in result.stdout
    assert '"source": "fixture"' in result.stdout
    assert "replace_me" not in result.stdout


def test_toobit_klines_dry_run_real_guarded() -> None:
    result = runner.invoke(
        app,
        ["toobit-klines-dry-run", "BTCUSDT", "--interval", "1m", "--minutes", "5"],
    )
    assert result.exit_code == 2


def test_toobit_klines_dry_run_requires_env_guard_with_real() -> None:
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        "triak_trade.cli._load_settings",
        lambda: Settings(_env_file=None, RUN_TOOBIT_MARKETDATA_INTEGRATION_TESTS=0),
    )
    result = runner.invoke(
        app,
        [
            "toobit-klines-dry-run",
            "BTCUSDT",
            "--interval",
            "1m",
            "--minutes",
            "5",
            "--real",
        ],
    )
    assert result.exit_code == 2
    monkeypatch.undo()

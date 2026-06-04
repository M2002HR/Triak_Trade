from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from typer.testing import CliRunner

from triak_trade.cli import app
from triak_trade.config.settings import Settings
from triak_trade.domain.enums import CandleSource
from triak_trade.domain.models import Candle

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


def test_binance_public_klines_dry_run_real_guarded() -> None:
    result = runner.invoke(
        app,
        ["binance-public-klines-dry-run", "BTCUSDT", "--interval", "1m", "--minutes", "5"],
    )
    assert result.exit_code == 2


def test_binance_public_klines_dry_run_with_mock_provider() -> None:
    class FakeProvider:
        async def get_klines(
            self,
            symbol: str,
            interval: str,
            start_time: datetime,
            end_time: datetime,
        ) -> list[Candle]:
            return [
                Candle(
                    symbol=symbol,
                    interval=interval,
                    open_time=start_time,
                    close_time=start_time + timedelta(minutes=1),
                    open=Decimal("1"),
                    high=Decimal("2"),
                    low=Decimal("0.5"),
                    close=Decimal("1.5"),
                    volume=Decimal("10"),
                    source=CandleSource.BINANCE,
                )
            ]

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        "triak_trade.cli._load_settings",
        lambda: Settings(_env_file=None, RUN_BINANCE_PUBLIC_MARKETDATA_INTEGRATION_TESTS=1),
    )
    monkeypatch.setattr(
        "triak_trade.cli._build_binance_public_provider",
        lambda settings: FakeProvider(),
    )
    result = runner.invoke(
        app,
        [
            "binance-public-klines-dry-run",
            "BTCUSDT",
            "--interval",
            "1m",
            "--minutes",
            "5",
            "--real",
        ],
    )
    assert result.exit_code == 0
    assert '"source": "binance"' in result.stdout
    assert "replace_me" not in result.stdout
    monkeypatch.undo()

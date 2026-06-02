from __future__ import annotations

from typing import Any

from typer.testing import CliRunner

import triak_trade.cli as cli_module
from triak_trade.cli import app

runner = CliRunner()


def test_toobit_check_does_not_print_secrets() -> None:
    result = runner.invoke(app, ["toobit-check"])
    assert result.exit_code == 0
    assert "api_key_present" in result.stdout
    assert "replace_me" not in result.stdout


def test_toobit_signed_check_blocked_without_guard() -> None:
    result = runner.invoke(app, ["toobit-signed-check"])
    assert result.exit_code == 2


def test_toobit_public_check_with_mock_client(monkeypatch: Any) -> None:
    class FakeClient:
        async def get_server_time(self) -> dict[str, object]:
            return {"serverTime": 1}

        async def get_exchange_info(self) -> dict[str, object]:
            return {"symbols": []}

    monkeypatch.setattr(cli_module, "_build_toobit_client", lambda settings: FakeClient())
    result = runner.invoke(app, ["toobit-public-check"])
    assert result.exit_code == 0
    assert '"public_check_success": true' in result.stdout


def test_toobit_order_test_blocked_without_guard() -> None:
    result = runner.invoke(
        app,
        [
            "toobit-order-test",
            "--symbol",
            "BTCUSDT",
            "--side",
            "BUY",
            "--type",
            "LIMIT",
            "--quantity",
            "0.001",
            "--price",
            "1",
        ],
    )
    assert result.exit_code == 2

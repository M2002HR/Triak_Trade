from __future__ import annotations

from pathlib import Path

from triak_trade.cli import app
from triak_trade.config.settings import Settings

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
ENV_EXAMPLE = ROOT / ".env.example"


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_readme_lists_only_real_cli_commands() -> None:
    readme = _read_text(README)
    documented = {
        line.strip().split()[1]
        for line in readme.splitlines()
        if line.strip().startswith("triak-trade ")
    }
    actual = {command.name for command in app.registered_commands if command.name}

    assert documented <= actual


def test_readme_does_not_reference_removed_admin_bot_commands() -> None:
    readme = _read_text(README)
    assert "admin-bot-smoke-test" not in readme
    assert "run-admin-bot" not in readme
    assert "admin-bot-status" not in readme
    assert "admin-bot-logs" not in readme


def test_env_example_matches_selected_settings_defaults() -> None:
    env_text = _read_text(ENV_EXAMPLE)
    values: dict[str, str] = {}
    for line in env_text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key] = value

    settings = Settings(_env_file=None)
    expected = {
        "BACKTEST_DEFAULT_RISK_PER_TRADE_PCT": str(settings.BACKTEST_DEFAULT_RISK_PER_TRADE_PCT),
        "BACKTEST_MIN_ALLOCATION_PCT": str(settings.BACKTEST_MIN_ALLOCATION_PCT),
        "BACKTEST_MAX_ALLOCATION_PCT": str(settings.BACKTEST_MAX_ALLOCATION_PCT),
        "BACKTEST_DEFAULT_STOP_PCT": str(settings.BACKTEST_DEFAULT_STOP_PCT),
        "BACKTEST_SYNTHETIC_STOP_MAX_LOSS_PCT_OF_BALANCE": str(
            settings.BACKTEST_SYNTHETIC_STOP_MAX_LOSS_PCT_OF_BALANCE
        ),
        "BACKTEST_FEE_RATE_PCT": str(settings.BACKTEST_FEE_RATE_PCT),
        "REAL_BACKTEST_ENABLED": str(settings.REAL_BACKTEST_ENABLED).lower(),
        "REAL_BACKTEST_MAX_CANDLES_PER_SYMBOL": str(settings.REAL_BACKTEST_MAX_CANDLES_PER_SYMBOL),
        "REAL_BACKTEST_ACTIVE_SIGNAL_HOURS": str(settings.REAL_BACKTEST_ACTIVE_SIGNAL_HOURS),
        "REAL_BACKTEST_FOLLOWUP_LAST_RESORT_ATTACH": str(
            settings.REAL_BACKTEST_FOLLOWUP_LAST_RESORT_ATTACH
        ).lower(),
        "REAL_BACKTEST_LIVE_SIM_UPDATE_EVERY_N": str(
            settings.REAL_BACKTEST_LIVE_SIM_UPDATE_EVERY_N
        ),
        "LIVE_TRADING_LIVE_MODE_ENABLED": str(settings.LIVE_TRADING_LIVE_MODE_ENABLED).lower(),
        "LIVE_TRADING_DEFAULT_RISK_PER_TRADE_PCT": str(
            settings.LIVE_TRADING_DEFAULT_RISK_PER_TRADE_PCT
        ),
        "LIVE_TRADING_FEE_RATE_PCT": str(settings.LIVE_TRADING_FEE_RATE_PCT),
        "TOOBIT_DEMO_PRIVATE_SYMBOL_MODE": settings.TOOBIT_DEMO_PRIVATE_SYMBOL_MODE,
    }

    for key, expected_value in expected.items():
        assert values.get(key) == expected_value


def test_env_example_removes_stale_admin_bot_keys() -> None:
    env_text = _read_text(ENV_EXAMPLE)
    assert "ADMIN_BOT_RUNTIME_ENABLED=" not in env_text
    assert "REAL_BACKTEST_SEND_TO_ADMIN_BOT=" not in env_text

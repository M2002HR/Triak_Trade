from __future__ import annotations

from triak_trade.config.settings import Settings
from triak_trade.verification import checks
from triak_trade.verification.models import VerificationStatus


def test_safe_checks_pass_without_real_services() -> None:
    settings = Settings()
    for check in [
        checks.config_check,
        checks.python_package_check,
        checks.db_engine_check,
        checks.redis_client_factory_check,
        checks.parser_check,
        checks.channel_agent_check,
        checks.ai_dry_run_check,
        checks.telegram_dry_run_check,
        checks.market_data_fake_check,
        checks.toobit_safety_check,
        checks.backtest_fixture_check,
    ]:
        result = check(settings)
        assert result.status is VerificationStatus.PASS, result.name


def test_real_checks_skip_without_guards() -> None:
    settings = Settings(RUN_SYSTEM_REAL_SMOKE_TESTS=0)
    for check in checks.real_checks():
        result = check(settings)
        assert result.status is VerificationStatus.SKIP

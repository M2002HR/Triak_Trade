from __future__ import annotations

from unittest.mock import patch

from triak_trade.cache.redis_client import create_redis_client
from triak_trade.config.settings import Settings
from triak_trade.core.health import run_health_checks
from triak_trade.db.engine import create_db_engine


def test_health_config_only() -> None:
    settings = Settings()
    result = run_health_checks(settings=settings, include_services=False)
    assert result.status == "ok"
    assert result.checks["config"] == "ok"


def test_redis_client_factory_without_connect() -> None:
    client = create_redis_client("redis://localhost:6379/0")
    assert client is not None


def test_db_engine_factory_without_connect() -> None:
    engine = create_db_engine("sqlite+pysqlite:///:memory:")
    assert engine is not None


def test_optional_service_checks_are_mockable() -> None:
    settings = Settings()
    with (
        patch("triak_trade.core.health.check_database", return_value="ok"),
        patch("triak_trade.core.health.check_redis", return_value="ok"),
    ):
        result = run_health_checks(settings=settings, include_services=True)
    assert result.status == "ok"
    assert result.checks["database"] == "ok"
    assert result.checks["redis"] == "ok"

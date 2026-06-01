"""Health checks."""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from triak_trade.cache.redis_client import create_redis_client
from triak_trade.config.settings import Settings
from triak_trade.db.engine import create_db_engine


@dataclass(slots=True)
class HealthResult:
    """Health check output model."""

    app_name: str
    status: str
    checks: dict[str, str] = field(default_factory=dict)


def check_config(settings: Settings) -> str:
    """Validate loaded config."""
    settings.model_dump()
    return "ok"


def check_database(database_url: str) -> str:
    """Optional DB connectivity check."""
    try:
        engine = create_db_engine(database_url)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return "ok"
    except SQLAlchemyError:
        return "error"


def check_redis(redis_url: str) -> str:
    """Optional Redis connectivity check."""
    try:
        client = create_redis_client(redis_url)
        client.ping()
        return "ok"
    except Exception:
        return "error"


def run_health_checks(settings: Settings, include_services: bool = False) -> HealthResult:
    """Run core health checks."""
    checks: dict[str, str] = {"config": check_config(settings)}

    if include_services:
        checks["database"] = check_database(settings.DATABASE_URL)
        checks["redis"] = check_redis(settings.REDIS_URL)

    status = "ok" if all(value == "ok" for value in checks.values()) else "error"
    return HealthResult(app_name=settings.APP_NAME, status=status, checks=checks)

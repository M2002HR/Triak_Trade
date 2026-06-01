"""SQLAlchemy engine/session factories."""

from __future__ import annotations

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from triak_trade.config.settings import Settings


def create_db_engine(database_url: str) -> Engine:
    """Create SQLAlchemy engine without connecting."""
    return create_engine(database_url, pool_pre_ping=True, future=True)


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Create session factory bound to provided engine."""
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


def build_engine_from_settings(settings: Settings) -> Engine:
    """Build DB engine from settings."""
    return create_db_engine(settings.DATABASE_URL)

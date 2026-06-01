"""Redis client factory."""

from __future__ import annotations

from redis import Redis

from triak_trade.config.settings import Settings


def create_redis_client(redis_url: str) -> Redis:
    """Create Redis client without network I/O."""
    return Redis.from_url(redis_url, decode_responses=True)


def build_redis_from_settings(settings: Settings) -> Redis:
    """Build Redis client from settings."""
    return create_redis_client(settings.REDIS_URL)

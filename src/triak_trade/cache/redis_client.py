"""Redis client factory."""

from __future__ import annotations

import logging

from redis import Redis

from triak_trade.config.settings import Settings
from triak_trade.core.logging import log_event

log = logging.getLogger(__name__)


def create_redis_client(redis_url: str) -> Redis:
    """Create Redis client without network I/O."""
    log_event(
        log,
        logging.INFO,
        "cache.redis_client_created",
        redis_url_present=bool(redis_url),
    )
    return Redis.from_url(redis_url, decode_responses=True)


def build_redis_from_settings(settings: Settings) -> Redis:
    """Build Redis client from settings."""
    log_event(
        log,
        logging.INFO,
        "cache.redis_client_build_from_settings",
        redis_url_present=bool(settings.REDIS_URL),
    )
    return create_redis_client(settings.REDIS_URL)

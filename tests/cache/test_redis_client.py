from __future__ import annotations

import logging

from triak_trade.cache.redis_client import build_redis_from_settings, create_redis_client
from triak_trade.config.settings import Settings


def test_create_redis_client_emits_log(caplog) -> None:
    with caplog.at_level(logging.INFO):
        client = create_redis_client("redis://localhost:6379/0")

    assert client.get_encoder().decode_responses is True
    record = next(rec for rec in caplog.records if rec.msg == "cache.redis_client_created")
    assert record.redis_url_present is True


def test_build_redis_from_settings_emits_log(caplog) -> None:
    settings = Settings(_env_file=None, REDIS_URL="redis://localhost:6379/5")

    with caplog.at_level(logging.INFO):
        client = build_redis_from_settings(settings)

    assert client.connection_pool.connection_kwargs["db"] == 5
    record = next(
        rec for rec in caplog.records if rec.msg == "cache.redis_client_build_from_settings"
    )
    assert record.redis_url_present is True

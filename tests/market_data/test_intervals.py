from __future__ import annotations

import pytest

from triak_trade.market_data.intervals import (
    interval_to_milliseconds,
    interval_to_seconds,
    validate_interval,
)


@pytest.mark.parametrize(
    ("interval", "seconds"),
    [
        ("1m", 60),
        ("3m", 180),
        ("5m", 300),
        ("15m", 900),
        ("30m", 1800),
        ("1h", 3600),
        ("2h", 7200),
        ("4h", 14400),
        ("6h", 21600),
        ("8h", 28800),
        ("12h", 43200),
        ("1d", 86400),
        ("1w", 604800),
    ],
)
def test_supported_intervals(interval: str, seconds: int) -> None:
    assert validate_interval(interval) == interval
    assert interval_to_seconds(interval) == seconds
    assert interval_to_milliseconds(interval) == seconds * 1000


def test_invalid_interval_rejected() -> None:
    with pytest.raises(ValueError):
        validate_interval("10m")

"""Candle interval utilities."""

from __future__ import annotations

SUPPORTED_INTERVALS: dict[str, int] = {
    "1m": 60,
    "3m": 180,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "2h": 7200,
    "4h": 14400,
    "6h": 21600,
    "8h": 28800,
    "12h": 43200,
    "1d": 86400,
    "1w": 604800,
}


def validate_interval(interval: str) -> str:
    value = interval.strip().lower()
    if value not in SUPPORTED_INTERVALS:
        raise ValueError(f"Unsupported interval: {interval}")
    return value


def interval_to_seconds(interval: str) -> int:
    return SUPPORTED_INTERVALS[validate_interval(interval)]


def interval_to_milliseconds(interval: str) -> int:
    return interval_to_seconds(interval) * 1000

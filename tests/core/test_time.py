from __future__ import annotations

from datetime import timezone

from triak_trade.core.time import parse_user_datetime_to_utc


def test_parse_user_datetime_to_utc_interprets_naive_values_as_tehran() -> None:
    parsed = parse_user_datetime_to_utc("2026-06-04T15:30:00")

    assert parsed.tzinfo is timezone.utc
    assert parsed.isoformat() == "2026-06-04T12:00:00+00:00"


def test_parse_user_datetime_to_utc_preserves_aware_values() -> None:
    parsed = parse_user_datetime_to_utc("2026-06-04T12:00:00+00:00")

    assert parsed.tzinfo is timezone.utc
    assert parsed.isoformat() == "2026-06-04T12:00:00+00:00"

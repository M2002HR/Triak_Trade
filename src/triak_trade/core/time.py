"""Time parsing helpers for user-facing local timestamps."""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

TEHRAN_TZ = ZoneInfo("Asia/Tehran")


def parse_user_datetime_to_utc(value: str | datetime) -> datetime:
    """Parse a user-facing datetime and normalize it to UTC.

    Naive datetimes are interpreted in the project's local operational timezone,
    which is Asia/Tehran for this deployment.
    """

    raw = value if isinstance(value, datetime) else datetime.fromisoformat(
        str(value).replace("Z", "+00:00")
    )
    if raw.tzinfo is None:
        raw = raw.replace(tzinfo=TEHRAN_TZ)
    return raw.astimezone(timezone.utc)

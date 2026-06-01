"""Clock abstractions for deterministic tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class FakeClock:
    def __init__(self, initial: datetime) -> None:
        self._now = initial

    def now(self) -> datetime:
        return self._now

    def advance(self, *, seconds: int = 0, hours: int = 0) -> None:
        self._now = self._now + timedelta(seconds=seconds, hours=hours)

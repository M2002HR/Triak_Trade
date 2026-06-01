"""SQLAlchemy declarative base and shared helpers."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


def utc_now() -> datetime:
    """Return timezone-aware UTC timestamp."""
    return datetime.now(timezone.utc)


class TimestampMixin:
    """Adds created_at/updated_at timestamps."""

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
    )

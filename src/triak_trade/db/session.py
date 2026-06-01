"""Session helpers."""

from __future__ import annotations

from collections.abc import Generator

from sqlalchemy.orm import Session, sessionmaker


def get_session(session_factory: sessionmaker[Session]) -> Generator[Session, None, None]:
    """Yield a transactional session."""
    session = session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

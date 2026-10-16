"""Engine, session factory, and the FastAPI session dependency.

SQLite is used synchronously on purpose: the app performs no runtime network
I/O, so there is nothing to await and fewer moving parts to reason about.
"""

from collections.abc import Iterator
from pathlib import Path
from typing import Optional

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session

from .config import get_settings
from .models import Base

_engine: Optional[Engine] = None


def make_engine(database_path: Path) -> Engine:
    """Build an engine for one SQLite file (also used directly by the tests)."""
    database_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{database_path}", future=True)

    # WAL keeps readers from blocking the writer; foreign keys are off by
    # default in SQLite and we do want them enforced.
    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine


def get_engine() -> Engine:
    """Process-wide engine for the configured database."""
    global _engine
    if _engine is None:
        _engine = make_engine(get_settings().database_path)
    return _engine


def reset_engine() -> None:
    """Drop the cached engine — used by tests and whenever config changes."""
    global _engine
    if _engine is not None:
        _engine.dispose()
    _engine = None


def create_schema(engine: Engine) -> None:
    """Create tables if they are missing.

    create_all is deliberate for M0: single user, local file. Real migrations
    will replace this before the app is deployed.
    """
    Base.metadata.create_all(engine)


def get_session() -> Iterator[Session]:
    """FastAPI dependency: one session per request."""
    with Session(get_engine()) as session:
        yield session

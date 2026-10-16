"""Shared test fixtures.

Every test gets a throwaway SQLite file and a TestClient wired to it, so the
suite never touches a developer's real database. The dictionary/frequency
tables are seeded with a handful of rows instead of the real 517k-entry import.
"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import insert
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import create_schema, get_session, make_engine, reset_engine
from app.main import app
from app.models import DictEntry, WordFreq

FIXTURES = Path(__file__).parent / "fixtures"

# A small stand-in for the real dictionary, matching what the reader will meet
# in the sample text used by the API tests.
SAMPLE_DICT = [
    {"lemma": "laufen", "pos": "v", "translation": "to run; to walk", "source": "test"},
    {"lemma": "Haus", "pos": "n", "translation": "house", "source": "test"},
    {"lemma": "Wolf", "pos": "n", "translation": "wolf", "source": "test"},
    {"lemma": "Mädchen", "pos": "n", "translation": "girl", "source": "test"},
]

SAMPLE_FREQ = [
    {"word": "haus", "rank": 120, "count": 420000},
    {"word": "wolf", "rank": 900, "count": 51000},
    {"word": "laufen", "rank": 700, "count": 88000},
]


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "test.sqlite3"


@pytest.fixture
def engine(db_path: Path):
    engine = make_engine(db_path)
    create_schema(engine)
    return engine


@pytest.fixture
def session(engine) -> Iterator[Session]:
    """A session on a fresh schema — empty until a test seeds it."""
    with Session(engine) as session:
        yield session


@pytest.fixture
def seeded(engine) -> None:
    """Seed the sample dictionary and frequency rows."""
    with Session(engine) as session:
        session.execute(insert(DictEntry), SAMPLE_DICT)
        session.execute(insert(WordFreq), SAMPLE_FREQ)
        session.commit()


@pytest.fixture
def client(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """TestClient bound to the temp database (real spaCy model, real HTTP stack).

    The environment is pointed at the temp database before the app starts, so
    even the startup hook prepares the throwaway file rather than the real one.
    """
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    get_settings.cache_clear()
    reset_engine()

    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        reset_engine()
        get_settings.cache_clear()


@pytest.fixture
def client_with_dict(client: TestClient, seeded: None) -> TestClient:
    """A client whose database already holds the sample dictionary."""
    return client

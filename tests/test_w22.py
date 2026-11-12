"""W-22: the dwell day-cap — one encounter credit per lemma per calendar day.

The cap exists because W-10's dwell rule credited every word on a page on every
load: eight reloads walked a whole page to `known` in about as many seconds. The
tests below pin the cap, the visible count it reports, and the thing it must not
break — that a word still reaches `known` by being read, just across days.
"""

from datetime import date, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_engine
from app.knowledge import credit_day, record_lookup, record_read_event
from app.models import TextRecord, WordStatus

TEXT = "Der Wolf lief durch den dunklen Wald."
DAY = date(2026, 1, 1)


def _create_text(client: TestClient) -> int:
    response = client.post(
        "/texts", data={"title": "Wald", "text": TEXT}, follow_redirects=False
    )
    assert response.status_code == 303
    return int(response.headers["location"].rsplit("/", 1)[-1])


def _seed_word(lemma: str, status: str = "new", encounters: int = 0) -> None:
    with Session(get_engine()) as session:
        session.add(WordStatus(lemma=lemma, status=status, source="placement", encounters=encounters))
        session.commit()


def _word(lemma: str) -> tuple[str, int, date | None]:
    with Session(get_engine()) as session:
        row = session.get(WordStatus, lemma)
        return (row.status, row.encounters, row.last_credited_day)


def _read(client: TestClient, text_id: int, monkeypatch, day: date) -> dict:
    monkeypatch.setattr("app.knowledge.credit_day", lambda: day)
    response = client.post("/api/read-events", json={"text_id": text_id, "page": 1})
    assert response.status_code == 200
    return response.json()


def test_eight_same_day_read_events_credit_the_word_once(client: TestClient, monkeypatch):
    """Check 1a: a page reloaded all evening is still one meeting."""
    text_id = _create_text(client)
    _seed_word("wald")

    bodies = [_read(client, text_id, monkeypatch, DAY) for _ in range(8)]

    status, encounters, credited = _word("wald")
    assert (status, encounters, credited) == ("new", 1, DAY)
    assert bodies[0]["skipped_same_day"] == 0
    assert all(body["skipped_same_day"] == 1 for body in bodies[1:])


def test_the_next_day_credits_a_second_encounter(client: TestClient, monkeypatch):
    """Check 1b: the cap is a day cap, not a permanent one.

    Uses a `learning` word (threshold 3) so the second credit is visible as
    `encounters == 2`; a `new` word would promote at 2 and reset the counter,
    which is W-10's schedule working, not the cap failing.
    """
    text_id = _create_text(client)
    _seed_word("wald", status="learning")

    for _ in range(8):
        _read(client, text_id, monkeypatch, DAY)
    assert _word("wald") == ("learning", 1, DAY)

    _read(client, text_id, monkeypatch, DAY + timedelta(days=1))

    status, encounters, credited = _word("wald")
    assert encounters == 2
    assert credited == DAY + timedelta(days=1)
    assert status == "learning"  # three credits are needed to reach `familiar`


def test_a_new_word_promotes_on_its_second_day_not_its_second_reload(client: TestClient, monkeypatch):
    """The cap and the schedule together: day 2 promotes, day 1 does not."""
    text_id = _create_text(client)
    _seed_word("wald", status="new")

    _read(client, text_id, monkeypatch, DAY)
    _read(client, text_id, monkeypatch, DAY)  # same day: refused
    assert _word("wald") == ("new", 1, DAY)

    _read(client, text_id, monkeypatch, DAY + timedelta(days=1))
    assert _word("wald") == ("learning", 0, DAY + timedelta(days=1))  # promoted, counter reset


def test_route_reports_the_refusal_instead_of_hiding_it(client: TestClient, monkeypatch):
    """Check 2: `skipped_same_day` on the wire, and the row it refers to."""
    text_id = _create_text(client)
    _seed_word("wald")

    first = _read(client, text_id, monkeypatch, DAY)
    second = _read(client, text_id, monkeypatch, DAY)

    assert first["skipped_same_day"] == 0
    assert second["skipped_same_day"] == 1
    # every pre-existing key keeps its meaning
    for key in ("words", "seen", "unrated", "promoted", "promoted_lemmas", "page", "total_pages"):
        assert key in second
    assert second["seen"] == 1
    assert second["unrated"] == first["unrated"]  # the other lemmas have no status
    assert second["promoted"] == 0


def test_known_is_still_reachable_in_eight_days(session: Session):
    """Check 3: W-10's acceptance is slowed, not undone.

    Schedule with one credit per day: new needs 2, learning 3, familiar 3.
    """
    session.add(WordStatus(lemma="wald", status="new", source="placement", encounters=0))
    session.commit()

    seen: list[tuple[int, str, int]] = []
    for offset in range(8):
        record_read_event(session, ["Wald"], day=DAY + timedelta(days=offset))
        row = session.get(WordStatus, "wald")
        seen.append((offset + 1, row.status, row.encounters))

    assert seen == [
        (1, "new", 1),
        (2, "learning", 0),
        (3, "learning", 1),
        (4, "learning", 2),
        (5, "familiar", 0),
        (6, "familiar", 1),
        (7, "familiar", 2),
        (8, "known", 0),
    ]


def test_a_second_text_on_the_same_day_does_not_credit_again(session: Session):
    """The cap is per lemma per day, not per text: pages across texts share it."""
    session.add(WordStatus(lemma="wald", status="new", source="placement", encounters=0))
    session.commit()

    record_read_event(session, ["Wald"], day=DAY)
    second = record_read_event(session, ["Wald"], day=DAY)  # a different page, same word

    row = session.get(WordStatus, "wald")
    assert row.encounters == 1
    assert second["skipped_same_day"] == 1


def test_unrated_words_are_counted_and_gain_no_row(client: TestClient, monkeypatch):
    text_id = _create_text(client)  # no statuses at all

    body = _read(client, text_id, monkeypatch, DAY)

    assert body["unrated"] > 0
    assert body["seen"] == 0
    assert body["skipped_same_day"] == 0
    with Session(get_engine()) as session:
        assert session.scalar(select(func.count()).select_from(WordStatus)) == 0


def test_credit_day_is_the_utc_day_of_the_same_clock(session: Session):
    """One definition of "today", documented for CET readers."""
    assert credit_day() == credit_day()
    assert isinstance(credit_day(), date)


def test_explicit_lookups_are_not_day_capped(session: Session):
    """Only silent encounters are capped; asking for a translation is a signal."""
    session.add(WordStatus(lemma="wald", status="new", source="placement", encounters=0))
    session.commit()

    record_lookup(session, "Wald")
    record_lookup(session, "Wald")

    row = session.get(WordStatus, "wald")
    assert row.status == "learning"
    assert row.encounters == 2  # both taps counted, cap does not apply


def test_migration_adds_the_column_to_an_existing_word_status_table(tmp_path):
    """Her 264 MB database must open without a manual step."""
    import sqlite3

    from app.db import create_schema, make_engine

    path = tmp_path / "old.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE word_status (
            lemma VARCHAR(200) PRIMARY KEY,
            status VARCHAR(20),
            band_index INTEGER,
            source VARCHAR(20),
            encounters INTEGER,
            updated_at DATETIME
        );
        INSERT INTO word_status VALUES ('wald', 'new', 0, 'placement', 0, '2026-01-01 00:00:00');
        """
    )
    connection.commit()
    connection.close()

    engine = make_engine(path)
    create_schema(engine)  # the same call the app's startup makes

    check = sqlite3.connect(path)
    columns = {row[1] for row in check.execute("PRAGMA table_info(word_status)")}
    kept = check.execute("select lemma, status from word_status").fetchall()
    check.close()

    assert "last_credited_day" in columns
    assert kept == [("wald", "new")]  # migration preserves data

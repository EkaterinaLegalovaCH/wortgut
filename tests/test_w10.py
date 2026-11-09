"""W-10: the learning loop — read events and the reader's "I know this".

Both are proven through the real HTTP stack, not only through the functions:
`promote()` existed and was unit-tested before this card, yet nothing called it,
so coverage could only fall. These tests fail if the routes stop being wired.
"""

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_engine
from app.knowledge import PROMOTION_ENCOUNTERS, seed_from_placement
from app.models import PlacementRun, TextRecord, Token, WordFreq, WordStatus

TEXT = "Der Wolf lief durch den dunklen Wald."


def _create_text(client: TestClient) -> int:
    response = client.post(
        "/texts", data={"title": "Wald", "text": TEXT}, follow_redirects=False
    )
    assert response.status_code == 303
    return int(response.headers["location"].rsplit("/", 1)[-1])


def _set_status(lemma: str, status: str, encounters: int = 0, source: str = "placement") -> None:
    with Session(get_engine()) as session:
        row = session.get(WordStatus, lemma)
        if row is None:
            row = WordStatus(lemma=lemma)
            session.add(row)
        row.status = status
        row.encounters = encounters
        row.source = source
        session.commit()


def _row(lemma: str) -> tuple[str, str, int] | None:
    with Session(get_engine()) as session:
        row = session.get(WordStatus, lemma)
        return None if row is None else (row.status, row.source, row.encounters)


def _counts() -> tuple[int, int, int]:
    with Session(get_engine()) as session:
        return (
            session.scalar(select(func.count()).select_from(TextRecord)) or 0,
            session.scalar(select(func.count()).select_from(Token)) or 0,
            session.scalar(select(func.count()).select_from(WordStatus)) or 0,
        )


def test_read_event_promotes_a_word_after_repeated_reads(client: TestClient):
    """Acceptance 1: the route itself moves a word, not a unit test of promote()."""
    text_id = _create_text(client)
    _set_status("wolf", "new", 0)

    first = client.post("/api/read-events", json={"text_id": text_id, "page": 1})
    assert first.status_code == 200
    body = first.json()
    assert body["page"] == 1 and body["total_pages"] == 1
    assert body["seen"] > 0
    assert _row("wolf") == ("new", "placement", 1)  # one meeting is not enough

    second = client.post("/api/read-events", json={"text_id": text_id, "page": 1})
    promoted = second.json()["promoted_lemmas"]
    assert "wolf" in promoted
    assert _row("wolf") == ("learning", "encounter", 0)


def test_read_event_reaches_the_next_step_at_the_configured_threshold(client: TestClient):
    """The route uses PROMOTION_ENCOUNTERS, not a number of its own."""
    text_id = _create_text(client)
    _set_status("wald", "learning", 0)

    for _ in range(PROMOTION_ENCOUNTERS["learning"] - 1):
        client.post("/api/read-events", json={"text_id": text_id, "page": 1})
    assert _row("wald") == ("learning", "placement", PROMOTION_ENCOUNTERS["learning"] - 1)

    client.post("/api/read-events", json={"text_id": text_id, "page": 1})
    assert _row("wald") == ("familiar", "encounter", 0)


def test_read_event_counts_unrated_words_without_inventing_a_status(client: TestClient):
    """"No opinion" stays no opinion, and the response says how many were skipped."""
    text_id = _create_text(client)

    body = client.post("/api/read-events", json={"text_id": text_id, "page": 1}).json()

    assert body["unrated"] > 0
    assert body["seen"] == 0
    with Session(get_engine()) as session:
        assert session.scalar(select(func.count()).select_from(WordStatus)) == 0


def test_read_event_is_read_only_for_texts_and_tokens(client: TestClient):
    text_id = _create_text(client)
    before = _counts()

    client.post("/api/read-events", json={"text_id": text_id, "page": 1})

    assert _counts() == before


def test_read_event_rejects_an_unknown_text(client: TestClient):
    response = client.post("/api/read-events", json={"text_id": 9999, "page": 1})
    assert response.status_code == 404


def test_read_event_clamps_a_page_past_the_end(client: TestClient):
    text_id = _create_text(client)
    body = client.post("/api/read-events", json={"text_id": text_id, "page": 999}).json()
    assert body["page"] == body["total_pages"] == 1


def test_know_route_marks_the_word_familiar_from_the_reader(client: TestClient):
    """Acceptance 2: familiar + source='reader', visible in the data."""
    response = client.post("/api/know", json={"lemma": "Wolf"})

    assert response.status_code == 200
    assert response.json() == {"lemma": "wolf", "status": "familiar", "source": "reader"}
    assert _row("wolf") == ("familiar", "reader", 0)


def test_know_route_works_for_a_lemma_with_no_rank_and_no_entry(client_with_dict: TestClient):
    """The point of the feature: Geißlein is archaic, unranked, and unknown to the dictionary."""
    with Session(get_engine()) as session:
        assert session.scalar(select(func.count()).select_from(WordFreq).where(WordFreq.word == "geißlein")) == 0

    response = client_with_dict.post("/api/know", json={"lemma": "Geißlein"})

    assert response.status_code == 200
    assert response.json()["status"] == "familiar"
    with Session(get_engine()) as session:
        row = session.get(WordStatus, "geißlein")
        assert row is not None
        assert row.status == "familiar"
        assert row.source == "reader"
        assert row.band_index is None


def test_know_route_rejects_a_blank_lemma(client: TestClient):
    assert client.post("/api/know", json={"lemma": "   "}).status_code == 422


def test_reseeding_keeps_a_word_the_reader_marked_known(session: Session, seeded: None):
    """A new placement run must not overwrite her own verdict."""
    session.add(WordStatus(lemma="wolf", status="familiar", source="reader", encounters=0))
    session.commit()

    run = PlacementRun(
        seed=1,
        label="test",
        level_low=0,
        level_high=0,
        known_bands=0,
        nonword_hits=0,
        nonword_total=0,
    )
    session.add(run)
    session.commit()
    seed_from_placement(session, run)

    row = session.get(WordStatus, "wolf")
    assert row is not None
    assert (row.status, row.source) == ("familiar", "reader")


def test_reseeding_keeps_rows_promoted_by_encounters(session: Session, seeded: None):
    """Regression: a promoted row is not a seed row, so re-seeding must not crash.

    `seed_from_placement` deletes only `source='placement'` rows, then inserts the
    ranked words again — a preserved `encounter` row for a ranked word would
    collide on the primary key unless the seeding treats it as evidence.
    """
    session.add(WordStatus(lemma="wolf", status="learning", source="encounter", encounters=2))
    session.commit()

    run = PlacementRun(
        seed=2,
        label="test",
        level_low=0,
        level_high=0,
        known_bands=0,
        nonword_hits=0,
        nonword_total=0,
    )
    session.add(run)
    session.commit()
    seed_from_placement(session, run)  # must not raise IntegrityError

    row = session.get(WordStatus, "wolf")
    assert row is not None
    assert (row.status, row.source) == ("learning", "encounter")

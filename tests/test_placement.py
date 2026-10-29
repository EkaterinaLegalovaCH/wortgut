"""Tests for the placement check: item selection, scoring, and the read-only guard.

The scoring tests use fabricated profiles (no database) because the mapping is a
pure function; the selection and page tests use the seeded sample data.
"""

import re
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import PROJECT_ROOT
from app.db import get_engine
from app.models import (
    DictEntry,
    PlacementItem,
    PlacementResponse,
    PlacementRun,
    TextRecord,
    Token,
    UserWord,
    WordFreq,
)
from app.placement import (
    BANDS,
    BAND_KNOWN_RATIO,
    DISCLAIMER,
    ITEMS_PER_BAND,
    KNOWN_BANDS_TO_LEVEL,
    TOTAL_ITEMS,
    Item,
    choose_word_class,
    dict_lookup_params,
    is_pool_candidate,
    select_items,
    score,
)

REAL_DB = PROJECT_ROOT / "data" / "wortgut.sqlite3"


# ---------------------------------------------------------------- fake profiles

def fake_items() -> list[Item]:
    """A full run's worth of items, shaped exactly like select_items' output."""
    items: list[Item] = []
    for band_index, count in enumerate(ITEMS_PER_BAND):
        for offset in range(count):
            items.append(Item(word=f"wort{band_index}x{offset}", band_index=band_index))
    return items


def answers_for(bands_known: int) -> list[bool | None]:
    """Claim known for every word in the first `bands_known` bands, no elsewhere."""
    return [item.band_index < bands_known for item in fake_items()]


# ---------------------------------------------------------------- scoring

@pytest.mark.parametrize(
    ("bands_known", "expected_label"),
    [
        (0, "A1"),
        (1, "A1"),
        (2, "A2"),
        (3, "A2"),
        (4, "B1"),
        (5, "B1"),
        (6, "B2"),
    ],
)
def test_known_band_count_maps_through_the_table(bands_known: int, expected_label: str):
    """Every possible band count has a row in KNOWN_BANDS_TO_LEVEL, and it is used."""
    profile = score(fake_items(), answers_for(bands_known))

    assert profile.known_bands == bands_known
    assert KNOWN_BANDS_TO_LEVEL[bands_known] == profile.level_low == profile.level_high
    assert profile.label == expected_label
    assert profile.flags == []
    assert profile.thin_sample is False


def test_label_table_covers_every_possible_count():
    assert set(KNOWN_BANDS_TO_LEVEL) == set(range(len(BANDS) + 1))


def test_band_threshold_is_sixty_percent():
    """60% decides a band: 5 of 7 passes, 4 of 7 does not."""
    items = fake_items()
    yes_counts = {0: 5, 1: 4, 2: 0}  # band -> how many of its items are claimed
    answers: list[bool | None] = []
    seen_per_band: dict[int, int] = {}

    for item in items:
        seen = seen_per_band.get(item.band_index, 0)
        seen_per_band[item.band_index] = seen + 1
        answers.append(seen < yes_counts.get(item.band_index, 0))

    profile = score(items, answers)

    assert profile.bands[0].known == 5 and profile.bands[0].known_band is True   # 5/7 = 71%
    assert profile.bands[1].known == 4 and profile.bands[1].known_band is False  # 4/7 = 57%
    assert profile.bands[2].known == 0 and profile.bands[2].known_band is False
    assert BAND_KNOWN_RATIO == 0.6


def test_thin_band_is_reported_and_widens_the_label():
    items = fake_items()
    answers = answers_for(6)
    for index, item in enumerate(items):
        if item.band_index == 3:
            answers[index] = None

    profile = score(items, answers)

    band = profile.bands[3]
    assert band.attempted == 0
    assert band.enough_data is False
    assert band.known_band is False
    assert profile.thin_sample is True
    assert "thin_sample" in profile.flags
    # 5 bands known -> B1 centre, widened to A2-B2
    assert profile.label == "A2–B2"


def test_unanswered_items_are_ignored_not_counted_as_unknown():
    """A skipped word shrinks the sample; it never counts as a "no"."""
    items = fake_items()
    answers: list[bool | None] = [
        None if index % 2 == 0 else answer for index, answer in enumerate(answers_for(1))
    ]

    profile = score(items, answers)

    for band in profile.bands:
        answered_in_band = [
            answer
            for item, answer in zip(items, answers)
            if item.band_index == band.index and answer is not None
        ]
        assert band.attempted == len(answered_in_band)
        assert band.known == sum(1 for answer in answered_in_band if answer)


def test_mismatched_lengths_are_rejected():
    with pytest.raises(ValueError):
        score(fake_items(), [True])


def test_result_payload_has_every_field_the_board_asks_for():
    profile = score(fake_items(), answers_for(3))
    payload = profile.as_dict()

    for field in (
        "bands",
        "known_bands",
        "level_low",
        "level_high",
        "label",
        "thin_sample",
        "flags",
        "disclaimer",
    ):
        assert field in payload

    assert len(payload["bands"]) == len(BANDS)
    assert payload["disclaimer"] == DISCLAIMER
    assert "not a certified CEFR test" in payload["disclaimer"]
    assert str(TOTAL_ITEMS) in payload["disclaimer"]
    for band in payload["bands"]:
        assert {"index", "low", "high", "attempted", "known", "enough_data", "known_band"} <= set(band)


# ---------------------------------------------------------------- item selection

def test_same_seed_gives_the_same_item_set(seeded: None, session: Session):
    first = select_items(session, 4242)
    second = select_items(session, 4242)

    assert [(item.word, item.band_index) for item in first] == [
        (item.word, item.band_index) for item in second
    ]


def test_item_count_and_band_distribution(seeded: None, session: Session):
    """40 items, distributed 7/7/7/7/6/6 (capped by what the pool holds)."""
    items = select_items(session, 7)
    per_band = {index: 0 for index in range(len(BANDS))}
    for item in items:
        per_band[item.band_index] += 1

    pool_counts = dict(
        session.execute(
            select(PlacementItem.band_index, func.count()).group_by(PlacementItem.band_index)
        ).all()
    )
    for index, expected in enumerate(ITEMS_PER_BAND):
        assert per_band[index] == min(expected, pool_counts.get(index, 0))

    assert len(items) == sum(per_band.values())
    assert all(0 <= item.band_index < len(BANDS) for item in items)


def test_every_item_is_a_real_pool_word(seeded: None, session: Session):
    """No invented words in the check: every item must come from placement_items."""
    pool = {word for (word,) in session.execute(select(PlacementItem.word)).all()}
    items = select_items(session, 2026)

    assert items
    assert all(item.word in pool for item in items)


def test_real_items_come_from_their_own_frequency_band(seeded: None, session: Session):
    items = select_items(session, 99)

    ranks = dict(session.execute(select(WordFreq.word, WordFreq.rank)).all())
    for item in items:
        low, high = BANDS[item.band_index]
        assert low <= ranks[item.word] <= high


def test_selection_reports_a_missing_pool(client: TestClient):
    """No pool built -> 503 that tells you the command, never a silent fallback."""
    response = client.get("/placement?seed=1")

    assert response.status_code == 503
    assert "build_placement_pool.py" in response.json()["detail"]


# ---------------------------------------------------------------- curated pool

@pytest.mark.parametrize(
    ("word", "pos", "expected"),
    [
        ("maus", "NOUN", True),
        ("laufen", "VERB", True),
        ("müde", "ADJ", True),
        ("ahh", "INTJ", False),          # interjection
        ("der", "DET", False),           # function word
        ("muss", "AUX", False),          # function word
        ("chffffff", "X", False),        # stray token
        ("dyke", "NOUN", False),         # editorially excluded
    ],
)
def test_pool_filter_keeps_only_real_vocabulary(word: str, pos: str, expected: bool):
    assert is_pool_candidate(word=word, pos=pos, has_translation=True) is expected


def test_pool_filter_requires_a_dictionary_entry():
    assert is_pool_candidate(word="maus", pos="NOUN", has_translation=False) is False


def test_pool_filter_rejects_short_words():
    assert is_pool_candidate(word="oh", pos="INTJ", has_translation=True) is False
    assert is_pool_candidate(word="abc", pos="NOUN", has_translation=True) is True


@pytest.mark.parametrize(
    ("dict_positions", "spacy_pos", "word", "expected"),
    [
        # the dictionary decides the class
        ({"n"}, "NOUN", "Maske", "noun"),
        ({"v"}, "VERB", "gehen", "verb"),
        ({"adj"}, "ADJ", "gleich", "adj"),
        ({"adj", "adv"}, "ADV", "gleich", "adj"),
        # any function-word tag vetoes the word, even alongside a noun tag
        ({"art", "n"}, "NOUN", "ein", None),
        ({"pron", "adj"}, "ADJ", "dein", None),
        ({"int"}, "INTJ", "nein", None),
        ({"conj"}, "CCONJ", "aber", None),
        # no dictionary tag means no ticket into the pool
        (set(), "NOUN", "irgendwas", None),
        # adverb-only entries are not vocabulary either
        ({"adv"}, "ADV", "noch", None),
        # verbs must be in citation form
        ({"v"}, "VERB", "abgestempelt", None),
        ({"v"}, "VERB", "laufen", "verb"),
        # a spaCy proper-noun tag is a veto, never a ticket
        ({"n"}, "PROPN", "Chevy", None),
    ],
)
def test_word_class_comes_from_the_dictionary_first(
    dict_positions: set[str], spacy_pos: str, word: str, expected: str | None
):
    assert choose_word_class(dict_positions=dict_positions, spacy_pos=spacy_pos, word=word) == expected


@pytest.mark.skipif(not REAL_DB.exists(), reason="real dictionary database not imported on this machine")
def test_real_tags_and_dictionary_reject_the_reported_words():
    """The words flagged in the use-test must be rejected by the rule itself.

    Note which half of the rule does the work: spaCy tags a bare "frodo" as a
    noun (there is no sentence context), so the *dictionary* check is what
    excludes names. The test therefore runs the real dictionary lookup instead
    of asserting a POS tag the model does not produce.
    """
    import spacy

    connection = sqlite3.connect(f"file:{REAL_DB}?mode=ro", uri=True)
    nlp = spacy.load("de_core_news_sm")
    try:
        for word, should_keep in [
            ("frodo", False),
            ("ahh", False),
            ("der", False),
            ("maus", True),
            ("laufen", True),
        ]:
            pos = nlp(word)[0].pos_
            attested = (
                connection.execute(
                    "select 1 from dict_entries where lemma = ? or lemma = ? limit 1",
                    dict_lookup_params(word),
                ).fetchone()
                is not None
            )
            kept = is_pool_candidate(word=word, pos=pos, has_translation=attested)
            assert kept is should_keep, (word, pos, attested)
    finally:
        connection.close()


# ---------------------------------------------------------------- pages and persistence

def test_form_renders_every_item_with_two_answers(client_with_dict: TestClient):
    page = client_with_dict.get("/placement?seed=123")

    item_count = len(select_items(Session(get_engine()), 123))
    assert page.status_code == 200
    assert "not a certified CEFR test" in page.text
    assert 'name="seed" value="123"' in page.text
    assert "Every word is real German" in page.text
    for index in range(item_count):
        assert f'name="item{index}" value="yes"' in page.text
        assert f'name="item{index}" value="no"' in page.text


def test_submit_stores_a_run_and_redirects_to_the_result(client_with_dict: TestClient):
    seed = 123
    items = select_items(Session(get_engine()), seed)

    data = {"seed": str(seed)}
    data.update({f"item{index}": "no" for index in range(len(items))})

    response = client_with_dict.post("/placement", data=data, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/placement/")

    result = client_with_dict.get(response.headers["location"])
    assert result.status_code == 200
    assert "not a certified CEFR test" in result.text
    assert "By frequency band" in result.text
    assert "A1" in result.text

    with Session(get_engine()) as session:
        run = session.scalar(select(PlacementRun))
        assert run is not None
        assert run.seed == seed
        # Every item answered "no": no band is known, so the centre is A1.
        expected = score(items, [False] * len(items))
        assert run.label == expected.label
        assert run.known_bands == 0
        stored = session.scalars(select(PlacementResponse)).all()
        assert len(stored) == len(items)
        assert all(response.claimed_known is False for response in stored)
        assert all(response.is_nonword is False for response in stored)


def test_result_page_matches_the_persisted_summary(client_with_dict: TestClient):
    seed = 55
    items = select_items(Session(get_engine()), seed)
    data = {"seed": str(seed), **{f"item{index}": "yes" for index in range(len(items))}}

    response = client_with_dict.post("/placement", data=data, follow_redirects=False)
    page = client_with_dict.get(response.headers["location"]).text

    expected = score(items, [True] * len(items))

    with Session(get_engine()) as session:
        run = session.scalar(select(PlacementRun))
        assert run is not None
        assert run.known_bands == expected.known_bands
        assert run.thin_sample is expected.thin_sample
        assert run.label == expected.label
        assert run.label in page
        assert f"run #{run.id}" in page


def test_unknown_run_is_404(client_with_dict: TestClient):
    assert client_with_dict.get("/placement/999").status_code == 404


def test_latest_redirects_to_the_most_recent_run(client_with_dict: TestClient):
    items = select_items(Session(get_engine()), 1)
    data = {"seed": "1", **{f"item{index}": "no" for index in range(len(items))}}
    client_with_dict.post("/placement", data=data, follow_redirects=False)

    latest = client_with_dict.get("/placement/latest", follow_redirects=False)
    assert latest.status_code == 303
    assert latest.headers["location"].startswith("/placement/")


def test_placement_is_read_only_for_user_data(client_with_dict: TestClient):
    """Acceptance: user_words / texts / tokens counts are identical before and after."""
    client_with_dict.post("/texts", data={"title": "Testtext", "text": "Der Wolf lief."}, follow_redirects=False)
    client_with_dict.post(
        "/api/words", json={"lemma": "Wolf", "translation": "wolf", "text_id": 1, "position": 1}
    )

    def counts() -> tuple[int, int, int]:
        with Session(get_engine()) as session:
            return (
                session.scalar(select(func.count()).select_from(UserWord)) or 0,
                session.scalar(select(func.count()).select_from(TextRecord)) or 0,
                session.scalar(select(func.count()).select_from(Token)) or 0,
            )

    before = counts()
    items = select_items(Session(get_engine()), 321)
    data = {"seed": "321", **{f"item{index}": "yes" for index in range(len(items))}}
    client_with_dict.post("/placement", data=data, follow_redirects=False)
    after = counts()

    assert before == after
    with Session(get_engine()) as session:
        assert session.scalar(select(func.count()).select_from(PlacementRun)) == 1


def test_missing_seed_is_rejected(client_with_dict: TestClient):
    assert client_with_dict.post("/placement", data={"item0": "yes"}).status_code == 422


def test_seed_over_the_limit_is_normalised_the_same_way_in_form_and_submit(client_with_dict: TestClient):
    """Regression: the form wrapped the seed modulo 1,000,000, the POST did not.

    A large ?seed= then produced one item set on screen and a different one in
    the scored run, so answers landed on the wrong words.
    """
    page = client_with_dict.get("/placement?seed=20261029").text
    match = re.search(r'name="seed" value="(\d+)"', page)
    assert match is not None
    shown_seed = int(match.group(1))
    assert shown_seed == 20261029 % 1_000_000

    items = select_items(Session(get_engine()), shown_seed)
    data = {"seed": str(20261029), **{f"item{index}": "no" for index in range(len(items))}}
    response = client_with_dict.post("/placement", data=data, follow_redirects=False)
    assert response.status_code == 303

    with Session(get_engine()) as session:
        run = session.scalar(select(PlacementRun))
        assert run is not None
        assert run.seed == shown_seed, "the stored run must use the seed the form showed"
        stored = session.scalars(select(PlacementResponse).order_by(PlacementResponse.position)).all()
        assert [row.word for row in stored] == [item.word for item in items]


def test_no_network_capable_imports_in_app_or_scripts():
    """The check must work offline like everything else in M0/M1."""
    root: Path = PROJECT_ROOT
    pattern = ("requests", "httpx", "aiohttp", "urllib.request", "socket")
    offenders = []
    for folder in ("app", "scripts"):
        for path in (root / folder).rglob("*.py"):
            for line in path.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if not (stripped.startswith("import ") or stripped.startswith("from ")):
                    continue
                if any(name in stripped for name in pattern):
                    offenders.append(f"{path.relative_to(root)}: {stripped}")
    assert offenders == []

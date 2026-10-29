"""Tests for the placement check: item selection, scoring, and the read-only guard.

The scoring tests use fabricated profiles (no database) because the mapping is a
pure function; the selection and page tests use the seeded sample data.
"""

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import PROJECT_ROOT
from app.db import get_engine
from app.models import DictEntry, PlacementResponse, PlacementRun, TextRecord, Token, UserWord, WordFreq
from app.placement import (
    BANDS,
    BAND_KNOWN_RATIO,
    DISCLAIMER,
    ITEMS_PER_BAND,
    KNOWN_BANDS_TO_LEVEL,
    NONWORDS,
    Item,
    select_items,
    score,
)

REAL_DB = PROJECT_ROOT / "data" / "wortgut.sqlite3"


# ---------------------------------------------------------------- fake profiles

def fake_items() -> list[Item]:
    """30 real words (5 per band) and the 10 nonwords — the shape of a full run."""
    items: list[Item] = []
    for band_index in range(len(BANDS)):
        for offset in range(ITEMS_PER_BAND):
            items.append(Item(word=f"wort{band_index}x{offset}", band_index=band_index, is_nonword=False))
    items.extend(Item(word=word, band_index=None, is_nonword=True) for word in NONWORDS)
    return items


def answers_for(bands_known: int, *, nonword_yes: int = 0) -> list[bool | None]:
    """Claim known for every word in the first `bands_known` bands, no elsewhere."""
    answers: list[bool | None] = []
    for item in fake_items():
        if item.is_nonword:
            answers.append(False)
        else:
            answers.append(item.band_index < bands_known)
    for index in range(nonword_yes):
        answers[30 + index] = True
    return answers


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
    assert profile.overclaims is False


def test_label_table_covers_every_possible_count():
    assert set(KNOWN_BANDS_TO_LEVEL) == set(range(len(BANDS) + 1))


def test_full_run_two_bands_short_widens_nothing_but_reports_the_gap():
    profile = score(fake_items(), answers_for(2))
    assert profile.known_bands == 2
    assert profile.overclaims is False
    assert profile.thin_sample is False
    assert profile.label == "A2"


def test_overclaiming_widens_the_label_and_sets_the_flag():
    """Three invented words claimed known -> overclaim flag, range one band wider."""
    profile = score(fake_items(), answers_for(0, nonword_yes=3))

    assert profile.nonword_hits == 3
    assert profile.nonword_total == len(NONWORDS)
    assert profile.overclaims is True
    assert "overclaims" in profile.flags
    # centre for 0 known bands is A1; widening a level each side clips at the bottom
    assert profile.level_low == 0
    assert profile.level_high == 1
    assert profile.label == "A1–A2"


def test_two_nonword_hits_do_not_trigger_the_flag():
    profile = score(fake_items(), answers_for(6, nonword_yes=2))
    assert profile.overclaims is False
    assert profile.label == "B2"


def test_thin_band_is_reported_and_widens_the_label():
    items = fake_items()
    answers = answers_for(6)
    # leave band 3 unanswered entirely
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
    # 5 bands known -> B1 centre, widened to A2–B2
    assert profile.label == "A2–B2"


def test_band_threshold_is_three_of_five():
    """60% of 5 is 3: three of five passes a band, two does not."""
    items = fake_items()
    yes_counts = {0: 5, 1: 3, 2: 2}  # band -> how many of its five are claimed
    answers: list[bool | None] = []
    seen_per_band: dict[int, int] = {}

    for item in items:
        if item.is_nonword:
            answers.append(False)
            continue
        seen = seen_per_band.get(item.band_index, 0)
        seen_per_band[item.band_index] = seen + 1
        answers.append(seen < yes_counts.get(item.band_index, 0))

    profile = score(items, answers)

    assert profile.bands[0].known == 5 and profile.bands[0].known_band is True
    assert profile.bands[1].known == 3 and profile.bands[1].known_band is True
    assert profile.bands[2].known == 2 and profile.bands[2].known_band is False
    assert BAND_KNOWN_RATIO == 0.6


def test_unanswered_items_are_ignored_not_counted_as_unknown():
    """A skipped word shrinks the sample; it never counts as a "no"."""
    items = fake_items()
    answers = [None if index % 2 == 0 else answer for index, answer in enumerate(answers_for(1))]

    profile = score(items, answers)

    for band in profile.bands:
        answered_in_band = [
            answer
            for item, answer in zip(items, answers)
            if item.band_index == band.index and answer is not None
        ]
        assert band.attempted == len(answered_in_band)
        assert band.known == sum(1 for answer in answered_in_band if answer)

    assert profile.nonword_total == len(NONWORDS)
    assert profile.nonword_hits == 0


def test_mismatched_lengths_are_rejected():
    with pytest.raises(ValueError):
        score(fake_items(), [True])


def test_result_payload_has_every_field_the_board_asks_for():
    profile = score(fake_items(), answers_for(3))
    payload = profile.as_dict()

    for field in (
        "bands",
        "nonword_hits",
        "nonword_total",
        "known_bands",
        "level_low",
        "level_high",
        "label",
        "overclaims",
        "thin_sample",
        "flags",
        "disclaimer",
    ):
        assert field in payload

    assert len(payload["bands"]) == len(BANDS)
    assert payload["disclaimer"] == DISCLAIMER
    assert "not a certified CEFR test" in payload["disclaimer"]
    for band in payload["bands"]:
        assert {"index", "low", "high", "attempted", "known", "enough_data", "known_band"} <= set(band)


# ---------------------------------------------------------------- item selection

def test_same_seed_gives_the_same_item_set(seeded: None, session: Session):
    first = select_items(session, 4242)
    second = select_items(session, 4242)

    assert [item.word for item in first] == [item.word for item in second]
    assert [(item.word, item.band_index) for item in first] == [
        (item.word, item.band_index) for item in second
    ]


def test_every_run_contains_all_nonwords_once(seeded: None, session: Session):
    items = select_items(session, 7)
    nonwords = [item.word for item in items if item.is_nonword]

    assert sorted(nonwords) == sorted(NONWORDS)
    assert len(nonwords) == len(set(nonwords))


def test_real_items_come_from_their_own_frequency_band(seeded: None, session: Session):
    items = select_items(session, 99)

    ranks = dict(session.execute(select(WordFreq.word, WordFreq.rank)).all())
    for item in items:
        if item.is_nonword:
            continue
        low, high = BANDS[item.band_index]
        assert low <= ranks[item.word] <= high


def test_nonwords_are_absent_from_the_sample_data(seeded: None, session: Session):
    lemmas = {lemma.lower() for (lemma,) in session.execute(select(DictEntry.lemma)).all()}
    words = {word.lower() for (word,) in session.execute(select(WordFreq.word)).all()}

    for nonword in NONWORDS:
        assert nonword.lower() not in lemmas
        assert nonword.lower() not in words


@pytest.mark.skipif(not REAL_DB.exists(), reason="real dictionary database not imported on this machine")
def test_nonwords_are_absent_from_the_real_data():
    """The calibration only works if no invented word is a real one."""
    connection = sqlite3.connect(f"file:{REAL_DB}?mode=ro", uri=True)
    try:
        for nonword in NONWORDS:
            in_dict = connection.execute(
                "select count(*) from dict_entries where lemma = ? collate nocase", (nonword,)
            ).fetchone()[0]
            in_freq = connection.execute(
                "select count(*) from word_freq where word = ? collate nocase", (nonword,)
            ).fetchone()[0]
            assert in_dict == 0, f"{nonword} is a real dictionary headword"
            assert in_freq == 0, f"{nonword} is in the frequency list"
    finally:
        connection.close()


# ---------------------------------------------------------------- pages and persistence

def test_form_renders_every_item_with_two_answers(client_with_dict: TestClient):
    page = client_with_dict.get("/placement?seed=123")

    assert page.status_code == 200
    assert "not a certified CEFR test" in page.text
    assert 'name="seed" value="123"' in page.text
    for index in range(3 + len(NONWORDS)):  # sample frequency list has 3 words
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
        # Bands 1-5 have no words in the sample list, so the run is thin and the
        # label is widened from A1 to A1-A2: honest behaviour, asserted as such.
        assert run.label == "A1–A2"
        assert run.thin_sample is True
        assert run.level_low == 0 and run.level_high == 1
        assert run.nonword_hits == 0
        assert run.nonword_total == len(NONWORDS)
        stored = session.scalars(select(PlacementResponse)).all()
        assert len(stored) == len(items)
        assert all(response.claimed_known is False for response in stored)


def test_result_page_matches_the_persisted_summary(client_with_dict: TestClient):
    seed = 55
    items = select_items(Session(get_engine()), seed)
    data = {"seed": str(seed)}
    for index, item in enumerate(items):
        data[f"item{index}"] = "no" if item.is_nonword else "yes"

    response = client_with_dict.post("/placement", data=data, follow_redirects=False)
    page = client_with_dict.get(response.headers["location"]).text

    with Session(get_engine()) as session:
        run = session.scalar(select(PlacementRun))
        assert run is not None
        assert run.known_bands == 1  # only band 0 has words in the sample list
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

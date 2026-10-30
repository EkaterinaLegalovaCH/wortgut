"""Tests for the known-word model: seeding, coverage, and the status machine."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, insert, select
from sqlalchemy.orm import Session

from app.db import get_engine
from app.knowledge import (
    COVERED_STATUSES,
    PROMOTION_ENCOUNTERS,
    SEED_POLICY,
    STATUSES,
    band_counts,
    passed_bands,
    promote,
    record_lookup,
    save_word,
    seed_from_placement,
    status_map,
    text_coverage,
)
from app.models import PlacementItem, PlacementResponse, PlacementRun, TextRecord, Token, UserWord, WordFreq, WordStatus
from app.placement import Item, score

SAMPLE_TEXT = [  # (lemma, surface) — "der" appears twice, in two cases
    ("der", "Der"),
    ("Hund", "Hund"),
    ("laufen", "läuft"),
    ("durch", "durch"),
    ("der", "den"),
    ("Garten", "Garten"),
]


def make_run(session: Session, *, bands_known: int, seed: int = 1) -> PlacementRun:
    """A stored run whose responses pass the first `bands_known` bands."""
    run = PlacementRun(
        seed=seed,
        label="test",
        level_low=0,
        level_high=0,
        known_bands=bands_known,
        nonword_hits=0,
        nonword_total=0,
        overclaims=False,
        thin_sample=False,
    )
    session.add(run)
    session.flush()

    position = 0
    for band_index in range(6):
        for offset in range(7):
            session.add(
                PlacementResponse(
                    run_id=run.id,
                    position=position,
                    # same names the pool/frequency fixtures use, so a claim can
                    # refer to a word that actually gets seeded
                    word=f"pool{band_index}x{offset}",
                    band_index=band_index,
                    is_nonword=False,
                    claimed_known=band_index < bands_known,
                )
            )
            position += 1
    session.commit()
    return run


BAND_RANKS = [1, 1500, 4000, 9000, 20000, 40000]  # one representative rank per band


def seed_pool(session: Session, words_per_band: int = 2) -> None:
    """Words that are both in the placement pool and in the frequency list.

    Seeding reads `word_freq`, so a pool row alone would not be seeded; a word
    needs a rank to belong to a band.
    """
    pool_rows = []
    freq_rows = []
    for band_index in range(6):
        for offset in range(words_per_band):
            word = f"pool{band_index}x{offset}"
            rank = BAND_RANKS[band_index] + offset
            pool_rows.append({"word": word, "band_index": band_index, "rank": rank})
            freq_rows.append({"word": word, "rank": rank, "count": 1})
    session.execute(insert(PlacementItem), pool_rows)
    session.execute(insert(WordFreq), freq_rows)
    session.commit()


def make_text(session: Session, lemmas: list[tuple[str, str]]) -> int:
    text = TextRecord(title="Testtext")
    session.add(text)
    session.flush()
    for position, (lemma, surface) in enumerate(lemmas):
        session.add(
            Token(
                text_id=text.id,
                position=position,
                surface=surface,
                lemma=lemma,
                pos="NOUN",
                is_word=True,
                whitespace=" ",
            )
        )
    session.commit()
    return text.id


# ---------------------------------------------------------------- seeding

def test_passed_bands_are_recomputed_from_responses_not_the_label(session: Session):
    run = make_run(session, bands_known=3)
    assert passed_bands(session, run) == {0, 1, 2}
    assert run.label == "test"  # the label is deliberately meaningless here


@pytest.mark.parametrize(
    ("policy", "expected_passed", "expected_rest"),
    [
        ("familiar", "familiar", "new"),   # the default: believe it, let reading confirm
        ("known", "known", "new"),         # believe the profile outright
    ],
)
def test_seed_policy_decides_what_a_passed_band_means(
    session: Session, policy: str, expected_passed: str, expected_rest: str
):
    seed_pool(session)
    run = make_run(session, bands_known=2)

    counts = seed_from_placement(session, run, policy=policy)

    statuses = dict(session.execute(select(WordStatus.lemma, WordStatus.status)).all())
    assert statuses["pool0x0"] == expected_passed
    assert statuses["pool1x0"] == expected_passed
    assert statuses["pool2x0"] == expected_rest
    assert counts[expected_passed] == 4
    assert counts[expected_rest] == 8
    assert all(row_source == "placement" for (row_source,) in session.execute(select(WordStatus.source)))


def test_a_word_claimed_in_a_failed_band_is_still_familiar(session: Session):
    """Individual answers beat the band they came from.

    She claimed 3 of 7 words in band 1; the band failed the 60% rule, but those
    three words are direct evidence and must not be seeded as new.
    """
    seed_pool(session)
    run = make_run(session, bands_known=1)
    # claim one specific word in band 2, which the run did not pass
    claimed_word = "pool2x0"
    row = session.scalar(select(PlacementResponse).where(PlacementResponse.run_id == run.id, PlacementResponse.word == claimed_word))
    row.claimed_known = True
    session.commit()

    seed_from_placement(session, run)

    assert session.get(WordStatus, claimed_word).status == "familiar"
    assert session.get(WordStatus, "pool2x1").status == "new"


def test_seed_policy_none_writes_nothing(session: Session):
    seed_pool(session)
    run = make_run(session, bands_known=6)

    counts = seed_from_placement(session, run, policy="none")

    assert sum(counts.values()) == 0
    assert (session.scalar(select(func.count()).select_from(WordStatus)) or 0) == 0


def test_unknown_policy_is_rejected(session: Session):
    run = make_run(session, bands_known=1, seed=2)
    with pytest.raises(ValueError):
        seed_from_placement(session, run, policy="wishful")


def test_reseeding_keeps_words_the_learner_actually_looked_up(session: Session):
    """"Real evidence outranks a questionnaire": re-taking the check must not erase reading."""
    seed_pool(session)
    run = make_run(session, bands_known=6)
    seed_from_placement(session, run)
    record_lookup(session, "pool0x0")  # they did not know this after all

    seed_from_placement(session, make_run(session, bands_known=0, seed=99))

    row = session.get(WordStatus, "pool0x0")
    assert row is not None
    assert row.status == "learning"
    assert row.source == "lookup"
    # everything else was re-seeded from the new run, which passed nothing
    assert session.get(WordStatus, "pool1x0").status == "new"


# ---------------------------------------------------------------- status machine

def test_lookup_demotes_a_seeded_word_to_learning(session: Session):
    seed_pool(session)
    seed_from_placement(session, make_run(session, bands_known=6))

    assert session.get(WordStatus, "pool0x0").status == "familiar"
    record_lookup(session, "pool0x0")

    row = session.get(WordStatus, "pool0x0")
    assert row.status == "learning"
    assert row.source == "lookup"
    assert row.encounters == 1


def test_lookup_on_an_unknown_word_creates_a_learning_row(session: Session):
    row = record_lookup(session, "Fremdwort")
    assert row.status == "learning"
    assert row.source == "lookup"


def test_saving_a_word_sets_the_same_status_in_both_tables(session: Session):
    text_id = make_text(session, [("Hund", "Hund")])

    word = save_word(session, lemma="Hund", translation="dog", context="Der Hund.", text_id=text_id)

    assert word.status == "learning"
    assert session.get(WordStatus, "hund").status == "learning"
    assert session.get(WordStatus, "hund").source == "save"


def test_promotion_needs_repeated_meetings(session: Session):
    """A word met once is still new; it has to earn each step."""
    record_lookup(session, "Zelt")            # creates learning, encounters 1
    row = session.get(WordStatus, "zelt")
    row.status = "new"
    row.encounters = 0
    session.commit()

    needed = PROMOTION_ENCOUNTERS["new"]
    for _ in range(needed - 1):
        assert promote(session, "Zelt") == "new"
    assert promote(session, "Zelt") == "learning"

    needed = PROMOTION_ENCOUNTERS["learning"]
    for _ in range(needed - 1):
        assert promote(session, "Zelt") == "learning"
    assert promote(session, "Zelt") == "familiar"


def test_status_map_defaults_unknown_lemmas_to_unrated(session: Session):
    """"No opinion" is not "new": the two buckets must never be merged."""
    record_lookup(session, "Bekannt")
    assert status_map(session, ["Bekannt", "NieGesehen"]) == {"Bekannt": "learning", "NieGesehen": "unrated"}


def test_status_lookup_is_case_insensitive(session: Session):
    """Text lemmas are capitalised for nouns, the frequency list is lowercase."""
    record_lookup(session, "Hund")
    assert status_map(session, ["Hund", "hund"]) == {"Hund": "learning", "hund": "learning"}


def test_band_counts_always_lists_every_status(session: Session):
    counts = band_counts(session)
    assert set(counts) == set(STATUSES)
    assert sum(counts.values()) == 0


# ---------------------------------------------------------------- coverage

def test_coverage_counts_distinct_lemmas_not_tokens(session: Session):
    text_id = make_text(session, SAMPLE_TEXT)  # "der" appears twice, in different cases
    for lemma in ("hund", "laufen", "garten", "der"):
        session.add(WordStatus(lemma=lemma, status="known", source="placement", encounters=0))
    session.commit()

    coverage = text_coverage(session, text_id)

    assert coverage.total_words == 5  # der/Hund/laufen/durch/Garten: "der" counted once
    assert coverage.known_words == 4  # everything but "durch", which has no row
    assert coverage.unrated_words == 1
    assert coverage.percent == 100  # 4 of 4 rated words


def test_coverage_after_a_fresh_install_has_no_baseline(session: Session):
    text_id = make_text(session, SAMPLE_TEXT)

    coverage = text_coverage(session, text_id)

    assert coverage.has_baseline is False
    assert coverage.percent is None
    assert "no level yet" in coverage.summary


def test_coverage_reports_percentage_and_new_words(session: Session):
    text_id = make_text(session, SAMPLE_TEXT)
    session.add(WordStatus(lemma="hund", status="known", source="placement", encounters=0))
    session.add(WordStatus(lemma="laufen", status="familiar", source="placement", encounters=0))
    session.add(WordStatus(lemma="garten", status="new", source="placement", encounters=0))
    session.commit()

    coverage = text_coverage(session, text_id)

    assert coverage.has_baseline is True
    assert "familiar" in COVERED_STATUSES
    assert coverage.known_words == 2          # hund + laufen
    assert coverage.new_words == 1            # garten: ranked, band not passed
    assert coverage.unrated_words == 2        # der, durch: no rank in this fixture
    assert coverage.rated_words == 3
    assert coverage.percent == round(100 * 2 / 3)
    assert f"{coverage.percent}% known" in coverage.summary
    assert f"{coverage.new_words} new words" in coverage.summary
    assert f"{coverage.unrated_words} not rated" in coverage.summary


def test_lemma_without_a_rank_is_unrated_not_new(session: Session):
    """The bug: 35 of 39 "new words" in one text had no status row at all.

    A word the app has no frequency rank for cannot be "not learned yet" — it is
    simply unknown to the model, and must not depress the percentage.
    """
    text_id = make_text(session, [("Hund", "Hund"), ("frisst", "frisst"), ("Geißlein", "Geißlein")])
    session.add(WordStatus(lemma="hund", status="familiar", source="placement", encounters=0))
    session.add(WordStatus(lemma="frisst", status="new", source="placement", encounters=0))
    session.commit()

    coverage = text_coverage(session, text_id)

    assert coverage.total_words == 3
    assert coverage.known_words == 1
    assert coverage.new_words == 1        # frisst: ranked, in a band not passed
    assert coverage.unrated_words == 1    # Geißlein: no rank anywhere
    assert coverage.rated_words == 2
    assert coverage.percent == 50          # 1 of 2 rated words, not 1 of 3
    assert "1 not rated" in coverage.summary


def test_baseline_requires_a_seeded_run_not_a_single_lookup(session: Session):
    """A fresh install with one lookup must still say "no level yet"."""
    text_id = make_text(session, [("Hund", "Hund")])

    record_lookup(session, "Hund")
    coverage = text_coverage(session, text_id)

    assert coverage.has_baseline is False
    assert coverage.percent is None
    assert "no level yet" in coverage.summary


def test_seeding_covers_ranked_words_outside_the_placement_pool(session: Session):
    """Seeding reads word_freq, so function words and common verbs get a status."""
    session.execute(
        insert(WordFreq),
        [
            {"word": "der", "rank": 10, "count": 1},
            {"word": "und", "rank": 9, "count": 1},
            {"word": "gehen", "rank": 106, "count": 1},
            {"word": "frisst", "rank": 9000, "count": 1},
        ],
    )
    session.commit()

    seed_from_placement(session, make_run(session, bands_known=1))

    statuses = dict(session.execute(select(WordStatus.lemma, WordStatus.status)).all())
    assert statuses["der"] == "familiar"     # band 0 passed
    assert statuses["gehen"] == "familiar"
    assert statuses["frisst"] == "new"       # band 2 not passed


# ---------------------------------------------------------------- pages

def test_reader_shows_coverage(client_with_dict: TestClient):
    client_with_dict.post("/texts", data={"title": "Testtext", "text": "Der Wolf lief."}, follow_redirects=False)

    page = client_with_dict.get("/texts/1").text

    assert 'class="coverage"' in page
    assert "no level yet" in page  # nothing seeded yet, and it says so


def test_home_shows_coverage_per_text(client_with_dict: TestClient):
    client_with_dict.post("/texts", data={"title": "Testtext", "text": "Der Wolf lief."}, follow_redirects=False)

    page = client_with_dict.get("/").text

    assert "no level yet" in page


def test_submitting_a_placement_run_seeds_the_model(client_with_dict: TestClient):
    from app.placement import select_items

    seed = 11
    items = select_items(Session(get_engine()), seed)
    # answer "yes" to every word in band 0 only
    data = {"seed": str(seed)}
    for index, item in enumerate(items):
        data[f"item{index}"] = "yes" if item.band_index <= 1 else "no"
    client_with_dict.post("/placement", data=data, follow_redirects=False)

    with Session(get_engine()) as session:
        total = session.scalar(select(func.count()).select_from(WordStatus)) or 0
        assert total > 0, "a submitted run should seed word_status"
        seeded = session.scalar(
            select(func.count()).select_from(WordStatus).where(WordStatus.source == "placement")
        )
        assert seeded == total
        statuses = {status for (status,) in session.execute(select(WordStatus.status))}
        assert statuses <= {"familiar", "known", "new"}

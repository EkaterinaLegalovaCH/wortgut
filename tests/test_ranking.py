"""Tests for the choose-next-text ranking (W-9).

The ranking must be honest: a percentage built on words the app knows nothing
about would be a guess, and guessing is exactly what the board forbids. So the
tests pin three things — that unrated words never count as known, that the fast
whole-library query agrees with the slow per-text one, and that a text too short
to read is not offered as the next read.
"""

from collections import Counter

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.knowledge import text_coverage
from app.models import TextLemma, TextRecord, Token, WordStatus
from app.ranking import (
    MIN_SUGGEST_WORDS,
    coverage_by_text,
    index_text_lemmas,
    suggest_next_texts,
)


def add_text(session: Session, title: str, lemmas: list[str]) -> TextRecord:
    """Create a text whose tokens are `lemmas`, repeated as written."""
    text = TextRecord(title=title)
    text.tokens = [
        Token(
            position=index,
            surface=lemma,
            lemma=lemma,
            pos="NOUN",
            is_word=True,
            whitespace=" ",
        )
        for index, lemma in enumerate(lemmas)
    ]
    session.add(text)
    session.flush()
    index_text_lemmas(session, text.id)
    session.commit()
    return text


def set_statuses(session: Session, statuses: dict[str, str], source: str = "placement") -> None:
    """Write word-status rows; `source='placement'` makes the baseline exist."""
    for lemma, status in statuses.items():
        session.merge(WordStatus(lemma=lemma.lower(), status=status, source=source))
    session.commit()


def words(prefix: str, count: int) -> list[str]:
    return [f"{prefix}{index}" for index in range(count)]


def test_no_suggestions_without_a_placement_baseline(session: Session):
    """A percentage over no evidence is a guess — say nothing instead."""
    add_text(session, "Irgendein Text", words("wort", 600))

    assert suggest_next_texts(session) == []


def test_ranks_the_text_with_more_known_words_first(session: Session):
    easy = add_text(session, "Leicht", words("leicht", 600))
    hard = add_text(session, "Schwer", words("schwer", 600))
    set_statuses(
        session,
        {**{w: "familiar" for w in words("leicht", 300)}, **{w: "new" for w in words("schwer", 600)}},
    )

    suggestions = suggest_next_texts(session)

    assert [item.text.id for item in suggestions] == [easy.id, hard.id]
    assert suggestions[0].known_share == 50
    assert suggestions[1].known_share == 0


def test_unrated_words_never_count_as_known(session: Session):
    """Half the text has no rank: the honest score is 0%, not 50%."""
    text = add_text(session, "Unbekannt", words("dunkel", 600))
    set_statuses(session, {w: "new" for w in words("dunkel", 300)})

    suggestion = suggest_next_texts(session)[0]

    assert suggestion.text.id == text.id
    assert suggestion.known_share == 0
    assert suggestion.coverage.unrated_words == 300
    assert "300 not rated (50%)" in suggestion.why


def test_fast_coverage_agrees_with_the_slow_per_text_path(session: Session):
    """The optimisation must not change the answer."""
    first = add_text(session, "Eins", words("eins", 40) + words("gemeinsam", 10))
    second = add_text(session, "Zwei", words("zwei", 25))
    set_statuses(
        session,
        {
            **{w: "known" for w in words("eins", 20)},
            **{w: "learning" for w in words("eins", 5)},
            "gemeinsam0": "familiar",
            **{w: "new" for w in words("zwei", 10)},
        },
    )

    fast = coverage_by_text(session)

    for text in (first, second):
        slow = text_coverage(session, text.id)
        assert fast[text.id].total_words == slow.total_words
        assert fast[text.id].known_words == slow.known_words
        assert fast[text.id].new_words == slow.new_words
        assert fast[text.id].learning_words == slow.learning_words
        assert fast[text.id].unrated_words == slow.unrated_words
        assert fast[text.id].percent == slow.percent


def test_short_texts_are_not_offered_as_the_next_read(session: Session):
    """A 50-word sample at 100% must not outrank a real text at 10%."""
    sample = add_text(session, "Leseprobe", words("probe", 50))
    book = add_text(session, "Buch", words("buch", MIN_SUGGEST_WORDS))
    set_statuses(
        session,
        {**{w: "known" for w in words("probe", 50)}, **{w: "new" for w in words("buch", MIN_SUGGEST_WORDS)}},
    )

    suggestions = suggest_next_texts(session)

    assert [item.text.id for item in suggestions] == [book.id]
    assert sample.id not in [item.text.id for item in suggestions]


def test_a_small_library_still_suggests_what_it_has(session: Session):
    """With nothing long enough yet, the short texts are better than silence."""
    add_text(session, "Nur eine Probe", words("kurz", 80))
    set_statuses(session, {"kurz0": "known"})

    suggestions = suggest_next_texts(session)

    assert len(suggestions) == 1
    assert suggestions[0].known_share >= 1


def test_why_line_cites_the_numbers_it_counted(session: Session):
    # Disjoint word sets: known, learning, and the rest count as new.
    add_text(session, "Zahlen", words("k", 60) + words("l", 20) + words("u", 520))
    set_statuses(
        session,
        {
            **{w: "known" for w in words("k", 60)},
            **{w: "learning" for w in words("l", 20)},
            **{w: "new" for w in words("u", 520)},  # rated, not yet learned
        },
        source="lookup",
    )
    set_statuses(session, {"k0": "known"})  # source='placement' makes the baseline exist

    suggestion = suggest_next_texts(session)[0]

    assert "10% of its 600 distinct words" in suggestion.why
    assert "520 new" in suggestion.why
    assert "20 in progress" in suggestion.why


def test_indexing_the_same_text_twice_does_not_duplicate_rows(session: Session):
    text = add_text(session, "Doppelt", ["Haus", "haus", "Hund"])

    index_text_lemmas(session, text.id)
    index_text_lemmas(session, text.id)
    session.commit()

    stored = session.scalar(
        select(func.count()).select_from(TextLemma).where(TextLemma.text_id == text.id)
    )
    assert stored == 2  # "Haus" and "haus" are one word once lowercased
    lemmas = Counter(row for row in session.scalars(select(TextLemma.lemma)))
    assert lemmas["haus"] == 1

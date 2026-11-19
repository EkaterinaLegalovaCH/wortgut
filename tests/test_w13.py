"""W-13: lemma repairs and the surface-form dictionary fallback.

Two behaviours, both triggered by a real friction-log entry (2026-11-19):
a learner taps a plain German word, the popup says "no entry found", and the
word the app shows her as a "lemma" is not a German word at all.

Design note — where the repair applies, and where it deliberately does not:

- **On ingest** (`pipeline.tokenize`): a new text never stores a broken lemma.
- **Once, on existing data** (`scripts/fix_lemmas.py`): rewrites `tokens.lemma`
  and rebuilds the derived tables, so every reader of `tokens` agrees.
- **On lookup** (`lookup.lookup`): a safety net. If a row somehow still carries
  a broken lemma, the popup shows the real word instead of the broken key.
- **Not in `text_coverage` / `coverage_by_text`**: one is a Python walk, the
  other a pure SQL join, and a test asserts they agree bucket by bucket. Repairing
  in one read path but not the other would make them disagree about the same
  database. The data is repaired, so both read the same corrected lemmas.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.knowledge import text_coverage
from app.lemmas import LEMMA_FIXES, fix_lemma, is_repaired
from app.lookup import find_translations_for, lookup
from app.models import DictEntry, TextRecord, Token, WordStatus


def _text_with(session: Session, tokens: list[tuple[str, str, str]]) -> int:
    """Create a one-paragraph text from (surface, lemma, pos) triples."""
    record = TextRecord(title="W-13 text")
    record.tokens = [
        Token(position=i, surface=surface, lemma=lemma, pos=pos, is_word=True, whitespace=" ")
        for i, (surface, lemma, pos) in enumerate(tokens)
    ]
    session.add(record)
    session.commit()
    return record.id


# --- the repair table itself -------------------------------------------------


def test_fix_lemma_is_idempotent_and_case_insensitive() -> None:
    assert fix_lemma("eur") == "euer"
    assert fix_lemma("Eur") == "euer"
    assert fix_lemma("Thüre") == "tür"
    assert fix_lemma("thüre") == "tür"
    # a corrected lemma is never itself a key, so a second pass changes nothing
    assert fix_lemma(fix_lemma("eur")) == "euer"
    # unknown and empty input pass through untouched
    assert fix_lemma("Haus") == "Haus"
    assert fix_lemma("") == ""


def test_is_repaired_reports_the_table() -> None:
    assert is_repaired("eur") is True
    assert is_repaired("Haus") is False


def test_no_repair_target_is_also_a_repair_source() -> None:
    """The table must not chain: `a -> b` and `b -> c` would make the result
    depend on how many times the function ran."""
    sources = {bad.lower() for bad in LEMMA_FIXES}
    for bad, (good, _why) in LEMMA_FIXES.items():
        assert good.lower() not in sources, f"{good} is both a target and a source (via {bad})"


def test_every_entry_states_a_reason() -> None:
    for bad, (good, why) in LEMMA_FIXES.items():
        assert good, f"{bad} has no target"
        assert why in {"card", "spacy-suffix", "old-orthography"}, f"{bad}: unknown reason {why!r}"


# --- the surface-form fallback (the friction-log complaint) -------------------


def test_surface_fallback_finds_an_inflected_headword(session: Session) -> None:
    """The learner taps `Barken`; the dictionary only has `Barken` (plural form).

    spaCy lemmatises it to `Barke`, which is not a headword, so searching only
    the lemma says "no entry found" for a word that is right there.
    """
    session.add(DictEntry(lemma="Barken", pos="n", translation="barques", source="test"))
    session.commit()

    assert find_translations_for(session, "Barke", "Barken") == ["barques"]


def test_lemma_wins_over_surface_when_both_exist(session: Session) -> None:
    """Order matters: the lemma's entries describe the word, not one inflection."""
    session.add_all(
        [
            DictEntry(lemma="laufen", pos="v", translation="to run", source="test"),
            DictEntry(lemma="laufen", pos="v", translation="to walk", source="test"),
            DictEntry(lemma="liefen", pos="v", translation="ran (plural)", source="test"),
        ]
    )
    session.commit()

    assert find_translations_for(session, "laufen", "liefen") == ["to run", "to walk"]


def test_repaired_lemma_is_tried_before_the_surface(session: Session) -> None:
    """An old row may hold `eur`; the dictionary answers `euer`."""
    session.add(DictEntry(lemma="euer", pos="det", translation="your", source="test"))
    session.commit()

    assert find_translations_for(session, "eur", "Eure") == ["your"]


def test_surface_fallback_returns_nothing_when_nothing_matches(session: Session) -> None:
    """A genuine miss must stay a miss — no invented translation."""
    assert find_translations_for(session, "quarkkäulchen", "Quarkkäulchen") == []


# --- lookup end to end -------------------------------------------------------


def test_repaired_lemma_is_used_by_lookup(session: Session) -> None:
    """A row written before the table existed still answers with the real word.

    `eur` was stored as a lemma by an older ingest; the popup must show `euer`
    and its translation, not the broken key.
    """
    session.add(DictEntry(lemma="euer", pos="det", translation="your", source="test"))
    text_id = _text_with(session, [("Eure", "eur", "DET")])
    session.commit()

    result = lookup(session, text_id, 0)
    assert result is not None
    assert result.lemma == "euer"
    assert result.translations == ["your"]


def test_lookup_saves_under_the_repaired_lemma(session: Session) -> None:
    """Saving from the popup must use the real lemma, not the broken one.

    Otherwise the learner's own list fills up with non-words — which is exactly
    what happened in production (`verdunkelen`, `rüsteen`).
    """
    from app.lookup import save_user_word

    text_id = _text_with(session, [("Eure", "eur", "DET")])
    session.commit()
    result = lookup(session, text_id, 0)
    assert result is not None

    word = save_user_word(
        session, lemma=result.lemma, translation="your", text_id=text_id, position=0
    )
    assert word.lemma == "euer"


# --- coverage after a repair -------------------------------------------------


def test_coverage_counts_repaired_lemmas_as_one_word(session: Session) -> None:
    """After repair, three former `eur` forms are one known word, not two.

    This is the invariant the migration produces: `tokens.lemma` is rewritten,
    so `text_coverage` sees one lemma with one status.
    """
    text_id = _text_with(
        session, [("Eure", "euer", "DET"), ("Euer", "euer", "DET"), ("euren", "euer", "DET")]
    )
    session.add(WordStatus(lemma="euer", status="familiar", source="placement", encounters=0))
    session.commit()

    coverage = text_coverage(session, text_id)
    assert coverage.total_words == 1, "the three forms are one lemma"
    assert coverage.known_words == 1
    assert coverage.unrated_words == 0

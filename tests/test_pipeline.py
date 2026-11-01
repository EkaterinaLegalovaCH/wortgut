"""Tests for the spaCy pipeline: tokenization, lemmatization, word marking.

These tests run the real German model — lemmatization is exactly the part that
cannot be mocked meaningfully.
"""

import pytest

from app.pipeline import (
    MAX_CHUNK_CHARS,
    MODEL_NAME,
    _chunks,
    is_model_installed,
    load_nlp,
    tokenize,
)

pytestmark = pytest.mark.skipif(
    not is_model_installed(), reason=f"{MODEL_NAME} not installed"
)


def test_lemmatizes_irregular_verb_form():
    """"lief" is the past tense of "laufen" — the lemma, not the surface, is the key."""
    drafts = tokenize("Er lief schnell nach Hause.")
    lemmas = {draft.surface: draft.lemma for draft in drafts}
    assert lemmas["lief"] == "laufen"


def test_lemmatizes_plural_noun_to_singular():
    drafts = tokenize("Die Häuser sind alt.")
    lemmas = {draft.surface: draft.lemma for draft in drafts}
    assert lemmas["Häuser"] == "Haus"


def test_marks_words_and_punctuation():
    drafts = tokenize("Der Wolf kommt.")
    by_surface = {draft.surface: draft for draft in drafts}

    assert by_surface["Wolf"].is_word is True
    assert by_surface["Wolf"].pos == "NOUN"
    assert by_surface["."].is_word is False  # punctuation stays, but is not tappable


def test_positions_are_contiguous_from_zero():
    drafts = tokenize("Ein kleiner Test.")
    assert [draft.position for draft in drafts] == list(range(len(drafts)))


def test_tokens_keep_surface_spelling_with_umlauts():
    drafts = tokenize("Schöne Grüße aus München!")
    surfaces = [draft.surface for draft in drafts]
    assert "Schöne" in surfaces and "Grüße" in surfaces and "München" in surfaces


def test_whitespace_is_captured_from_the_original_text():
    """The reader replays this instead of guessing where spaces belong."""
    drafts = tokenize("Der Wolf kommt.")
    assert [(d.surface, d.whitespace) for d in drafts] == [
        ("Der", " "),
        ("Wolf", " "),
        ("kommt", ""),
        (".", ""),
    ]


def test_whitespace_is_normalised_to_newlines_for_paragraphs():
    drafts = tokenize("Erste Zeile.\n\nZweite Zeile.")
    assert drafts[2].surface == "."
    assert drafts[2].whitespace == "\n\n"


def test_empty_text_produces_no_tokens():
    assert tokenize("") == []


def test_model_load_is_cached():
    assert load_nlp() is load_nlp()


def test_chunks_preserve_the_text_exactly():
    """Chunking must be lossless — the reader shows what was pasted."""
    text = "Erster Absatz hier.\n\nZweiter Absatz dort.\n\nDritter Absatz."
    for limit in (20, 25, 40):
        pieces = list(_chunks(text, limit=limit))
        assert "".join(pieces) == text, f"limit={limit} lost characters"


def test_chunks_respect_the_limit():
    text = "Zeile eins.\n\nZeile zwei.\n\nZeile drei."
    pieces = list(_chunks(text, limit=20))
    assert all(len(piece) <= 20 for piece in pieces)


def test_chunks_keep_long_words_and_lines_intact():
    long_line = "Satz eins. " * 40
    pieces = list(_chunks(long_line, limit=60))
    assert "".join(pieces) == long_line
    assert all(len(piece) <= 60 for piece in pieces)


def test_tokenize_handles_text_longer_than_one_spacy_document():
    """A whole novel exceeds spaCy's 1M-character limit; chunking must save it."""
    sentence = "Der Wolf lief durch den dunklen Wald und suchte nach Beute. "
    # Comfortably past spaCy's 1,000,000-character ceiling.
    text = sentence * (1_050_000 // len(sentence) + 1)
    assert len(text) > 1_000_000

    drafts = tokenize(text)

    assert len(drafts) > 100_000
    assert "".join(d.surface + d.whitespace for d in drafts) == text
    assert [d.position for d in drafts] == list(range(len(drafts)))


def test_tokenize_keeps_paragraph_breaks_across_chunk_seams(monkeypatch):
    """Paragraph breaks survive where one chunk ends and the next begins."""
    monkeypatch.setattr("app.pipeline.MAX_CHUNK_CHARS", 40)
    text = "Erster Absatz hier.\n\nZweiter Absatz dort.\n\nDritter Absatz."

    drafts = tokenize(text)

    rebuilt = "".join(d.surface + d.whitespace for d in drafts)
    assert rebuilt == text
    assert any(d.whitespace == "\n\n" for d in drafts)

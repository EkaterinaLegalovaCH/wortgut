"""Tests for the spaCy pipeline: tokenization, lemmatization, word marking.

These tests run the real German model — lemmatization is exactly the part that
cannot be mocked meaningfully.
"""

import pytest

from app.pipeline import MODEL_NAME, is_model_installed, load_nlp, tokenize

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

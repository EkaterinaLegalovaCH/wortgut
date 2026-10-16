"""Tests for text layout: replaying stored whitespace and cutting sentences."""

import pytest

from app.layout import DisplayToken, collapse_whitespace, join, sentence_around


def tokens(*pairs: tuple[str, str]) -> list[DisplayToken]:
    return [DisplayToken(surface=surface, whitespace=whitespace) for surface, whitespace in pairs]


def test_join_replays_the_original_spacing():
    text = tokens(("Der", " "), ("Wolf", " "), ("kommt", ""), (".", ""))
    assert join(text) == "Der Wolf kommt."


def test_join_keeps_paragraph_breaks():
    text = tokens(("Ende", "\n\n"), ("Neuanfang", ""), (".", ""))
    assert join(text) == "Ende\n\nNeuanfang."


def test_join_strips_surrounding_whitespace():
    assert join(tokens(("Hallo", " "))) == "Hallo"


def test_collapse_whitespace_makes_one_line():
    assert collapse_whitespace("Ende\n\n  Neuanfang .") == "Ende Neuanfang ."


def test_sentence_around_finds_the_current_sentence():
    text = tokens(
        ("Der", " "), ("Wolf", " "), ("kommt", ""), (".", " "),
        ("Das", " "), ("Mädchen", " "), ("läuft", ""), (".", ""),
    )
    assert sentence_around(text, 1) == "Der Wolf kommt."
    assert sentence_around(text, 5) == "Das Mädchen läuft."


def test_sentence_around_stops_at_paragraph_breaks():
    text = tokens(("Erste", " "), ("Zeile", "\n\n"), ("Zweite", " "), ("Zeile", ""))
    assert sentence_around(text, 0) == "Erste Zeile"
    assert sentence_around(text, 3) == "Zweite Zeile"


def test_sentence_around_handles_last_sentence():
    text = tokens(("Der", " "), ("Wolf", ""), (".", " "), ("Er", " "), ("läuft", ""))
    assert sentence_around(text, 4) == "Er läuft"


def test_sentence_around_rejects_out_of_range_positions():
    with pytest.raises(IndexError):
        sentence_around(tokens(("Der", " "), ("Wolf", "")), 2)

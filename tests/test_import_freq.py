"""Tests for the frequency-list parser and import."""

from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import make_engine
from app.models import WordFreq
from scripts.import_freq import import_frequency, parse_freq_lines


def test_parse_freq_lines_uses_line_number_as_rank():
    lines = ["ich 5890279\n", "sie 3806767\n", "das 3122198\n"]
    assert list(parse_freq_lines(lines)) == [
        ("ich", 1, 5890279),
        ("sie", 2, 3806767),
        ("das", 3, 3122198),
    ]


def test_parse_freq_lines_skips_malformed_lines():
    lines = ["gut 100\n", "\n", "kaputt\n", "a b c\n", "zwei 2\n", "negativ -5\n"]
    assert list(parse_freq_lines(lines)) == [("gut", 1, 100), ("zwei", 5, 2)]


def test_parse_freq_lines_limit():
    lines = ["a 3\n", "b 2\n", "c 1\n"]
    assert list(parse_freq_lines(lines, limit=2)) == [("a", 1, 3), ("b", 2, 2)]


def test_parse_freq_lines_keeps_umlauts():
    assert list(parse_freq_lines(["schön 42\n"])) == [("schön", 1, 42)]


def test_import_frequency_is_idempotent_and_dedupes(tmp_path: Path):
    freq_file = tmp_path / "de.txt"
    freq_file.write_text("ich 100\nsie 90\nich 80\n", encoding="utf-8")
    db_path = tmp_path / "freq.sqlite3"

    import_frequency(db_path, freq_file)
    import_frequency(db_path, freq_file)

    with Session(make_engine(db_path)) as session:
        rows = session.scalars(select(WordFreq)).all()

    assert {row.word for row in rows} == {"ich", "sie"}
    assert len(rows) == 2  # re-running replaces instead of duplicating
    assert session_row(rows, "ich").rank == 1  # best (lowest) rank wins


def session_row(rows, word):
    return next(row for row in rows if row.word == word)

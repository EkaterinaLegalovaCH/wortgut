"""Tests for the FreeDict TEI parser and its database import."""

from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import DictEntry
from scripts.import_dict import SOURCE_NAME, import_dictionary, parse_entries, parse_entry

FIXTURES = Path(__file__).parent / "fixtures"


def test_parse_entries_reads_lemma_pos_and_translations():
    entries = list(parse_entries(FIXTURES / "mini.tei"))
    by_lemma = {entry.lemma: entry for entry in entries}

    assert by_lemma["Haus"].pos == "n"
    assert by_lemma["Haus"].translation == "house; home"
    assert by_lemma["Mädchen"].translation == "girl"  # umlauts survive the parser
    assert by_lemma["laufen"].pos == "v"
    assert by_lemma["laufen"].translation == "to run"


def test_parse_entries_skips_entries_without_translation():
    entries = list(parse_entries(FIXTURES / "mini.tei"))
    assert "nurform" not in {entry.lemma for entry in entries}


def test_parse_entries_ignores_example_citations():
    """Only type="trans" citations are translations; examples must not leak in."""
    laufen = next(entry for entry in parse_entries(FIXTURES / "mini.tei") if entry.lemma == "laufen")
    assert "Ich laufe schnell." not in laufen.translation


def test_parse_entry_handles_missing_form():
    from xml.etree import ElementTree

    empty = ElementTree.fromstring('<entry xmlns="http://www.tei-c.org/ns/1.0"></entry>')
    assert parse_entry(empty) is None


def test_import_dictionary_is_idempotent(session: Session, tmp_path: Path):
    db_path = tmp_path / "dict.sqlite3"

    first = import_dictionary(db_path, FIXTURES / "mini.tei")
    second = import_dictionary(db_path, FIXTURES / "mini.tei")

    assert first == second == 3  # three usable entries in the fixture


def test_import_dictionary_stores_rows(tmp_path: Path):
    db_path = tmp_path / "dict.sqlite3"
    import_dictionary(db_path, FIXTURES / "mini.tei")

    from app.db import make_engine

    with Session(make_engine(db_path)) as session:
        rows = session.scalars(
            select(DictEntry).where(DictEntry.lemma == "Haus", DictEntry.source == SOURCE_NAME)
        ).all()

    assert len(rows) == 1
    assert rows[0].translation == "house; home"
    assert rows[0].pos == "n"


def test_import_dictionary_limit(tmp_path: Path):
    db_path = tmp_path / "dict.sqlite3"
    assert import_dictionary(db_path, FIXTURES / "mini.tei", limit=1) == 1

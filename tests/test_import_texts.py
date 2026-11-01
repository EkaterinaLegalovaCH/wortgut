"""Tests for the public-domain text import (W-8).

No test touches the network: the downloader is injected or monkeypatched, and
every test runs on the throwaway database from conftest.
"""

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import create_schema, make_engine
from app.models import TextRecord, Token, UserWord, WordStatus
from scripts.import_texts import (
    ImportResult,
    import_text,
    load_manifest,
    run_import,
    strip_gutenberg_boilerplate,
)

SAMPLE_BODY = "Der Wolf lief durch Häuser und Mädchen. Er war hungrig und müde."

SAMPLE_ENTRY = {
    "gutenberg_id": 99999,
    "title": "Testtext",
    "author": "Test Autor",
    "source": "Project Gutenberg",
    "source_url": "https://www.gutenberg.org/ebooks/99999",
    "text_url": "https://www.gutenberg.org/cache/epub/99999/pg99999.txt",
    "license": "Public domain in the USA (Project Gutenberg); author died 1900",
}


def _wrapped(body: str) -> str:
    return (
        "The Project Gutenberg eBook of Test\r\n\r\n"
        "This eBook is for the use of anyone anywhere at no cost.\r\n\r\n"
        "*** START OF THE PROJECT GUTENBERG EBOOK TEST ***\r\n\r\n"
        f"{body}\r\n\r\n"
        "*** END OF THE PROJECT GUTENBERG EBOOK TEST ***\r\n\r\n"
        "Updated editions will replace the previous one.\r\n"
    )


def test_strip_boilerplate_returns_only_the_body():
    assert strip_gutenberg_boilerplate(_wrapped(SAMPLE_BODY)) == SAMPLE_BODY


def test_strip_boilerplate_keeps_text_when_markers_are_missing():
    raw = "Ein Text ohne Gutenberg-Rahmen."
    assert strip_gutenberg_boilerplate(raw) == raw


def test_import_text_records_source_licence_and_tokens(session: Session):
    result = import_text(session, SAMPLE_ENTRY, _wrapped(SAMPLE_BODY))

    assert result.skipped is False
    assert result.tokens > 0

    text = session.get(TextRecord, result.text_id)
    assert text.title == "Testtext"
    assert text.source_url == SAMPLE_ENTRY["source_url"]
    assert text.license == SAMPLE_ENTRY["license"]
    assert "Test Autor" in text.source_note

    token_count = session.scalar(
        select(func.count()).select_from(Token).where(Token.text_id == text.id)
    )
    assert token_count == result.tokens


def test_import_text_is_idempotent(session: Session):
    first = import_text(session, SAMPLE_ENTRY, _wrapped(SAMPLE_BODY))
    second = import_text(session, SAMPLE_ENTRY, _wrapped(SAMPLE_BODY))

    assert second.skipped is True
    assert second.text_id == first.text_id
    assert session.scalar(select(func.count()).select_from(TextRecord)) == 1


def test_import_text_never_touches_the_status_model(session: Session):
    """W-8 acceptance: import is read-only for `word_status` and `user_words`."""
    session.add(WordStatus(lemma="wolf", status="familiar", source="placement", band_index=1))
    session.add(
        UserWord(lemma="wolf", translation="wolf", status="learning", first_seen_text_id=None)
    )
    session.commit()

    before = (
        session.scalar(select(func.count()).select_from(WordStatus)),
        session.scalar(select(func.count()).select_from(UserWord)),
    )
    import_text(session, SAMPLE_ENTRY, _wrapped(SAMPLE_BODY))
    after = (
        session.scalar(select(func.count()).select_from(WordStatus)),
        session.scalar(select(func.count()).select_from(UserWord)),
    )

    assert before == after == (1, 1)
    assert session.get(WordStatus, "wolf").status == "familiar"


def test_run_import_uses_the_injected_downloader_and_reports_counts(engine, monkeypatch):
    calls: list[dict] = []

    def fake_download(entry: dict, cache_dir: Path) -> str:
        calls.append(entry)
        return _wrapped(SAMPLE_BODY)

    monkeypatch.setattr("scripts.import_texts.download_text", fake_download)

    entries = [SAMPLE_ENTRY, {**SAMPLE_ENTRY, "gutenberg_id": 99998, "title": "Zweiter Text",
                              "source_url": "https://www.gutenberg.org/ebooks/99998"}]
    results, before, after = run_import(
        engine, entries, cache_dir=Path("/tmp/does-not-matter"), progress=lambda *_: None
    )

    assert len(calls) == 2
    assert [r.skipped for r in results] == [False, False]
    assert all(r.tokens > 0 for r in results)
    assert before == after  # read-only for the status model


def test_run_import_skips_already_imported_texts(engine, monkeypatch):
    monkeypatch.setattr("scripts.import_texts.download_text", lambda e, c: _wrapped(SAMPLE_BODY))
    first, _, _ = run_import(engine, [SAMPLE_ENTRY], cache_dir=Path("/tmp/x"), progress=lambda *_: None)

    monkeypatch.setattr(
        "scripts.import_texts.download_text",
        lambda e, c: pytest.fail("a skipped text must not be downloaded again"),
    )
    second, _, _ = run_import(engine, [SAMPLE_ENTRY], cache_dir=Path("/tmp/x"), progress=lambda *_: None)

    assert first[0].skipped is False
    assert second[0].skipped is True


def test_create_schema_adds_late_columns_to_an_old_texts_table(tmp_path: Path):
    """An existing database from before W-8 must gain the new columns on startup."""
    db_path = tmp_path / "old.sqlite3"
    connection = sqlite3.connect(db_path)
    connection.execute(
        "CREATE TABLE texts (id INTEGER PRIMARY KEY, title VARCHAR(300), "
        "source_note TEXT, created_at DATETIME)"
    )
    connection.commit()
    connection.close()

    create_schema(make_engine(db_path))

    connection = sqlite3.connect(db_path)
    columns = {row[1] for row in connection.execute("PRAGMA table_info(texts)")}
    connection.close()
    assert {"source_url", "license"} <= columns


def test_manifest_lists_public_domain_german_texts():
    entries = load_manifest()

    assert 30 <= len(entries) <= 50
    for entry in entries:
        assert entry["language"] == "de"
        assert entry["source"] == "Project Gutenberg"
        assert entry["text_url"].startswith("https://www.gutenberg.org/cache/epub/")
        assert "public domain" in entry["license"].lower()
        assert entry["death_year"] <= 1955  # 70 years before 2026, with margin

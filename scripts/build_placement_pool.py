"""Build the curated word pool the placement check samples from.

    uv run python scripts/build_placement_pool.py

Why this exists: `word_freq` comes from film subtitles, so it contains names
(`frodo`), interjections (`ahh`), function words (`der`, `muss`) and stray
tokens (`chffffff`). Showing those measures nothing. This script keeps a word
only if

  1. it is 3+ letters of the alphabet (drops tokens like `chffffff`),
  2. the dictionary attests it as a headword, so it is real vocabulary with a
     translation (drops most names and slang), and
  3. spaCy tags it NOUN, VERB or ADJ (drops interjections, proper nouns, numbers
     and function words).

The result goes into `placement_items` (word, band_index, rank). Re-running is
safe: the table is rebuilt in one transaction. The whole run takes about a
minute on the full 50,000-word list.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings  # noqa: E402
from app.db import create_schema, make_engine  # noqa: E402
from app.placement import (  # noqa: E402
    ALLOWED_POS,
    BANDS,
    dict_lookup_params,
    ALLOWED_POS,
    choose_word_class,
    is_blocked,
)

CHUNK = 2_000


def band_for_rank(rank: int) -> int | None:
    """Which frequency band a rank falls into, or None when it is out of range."""
    for index, (low, high) in enumerate(BANDS):
        if low <= rank <= high:
            return index
    return None


def dict_positions(connection: sqlite3.Connection, word: str) -> set[str]:
    """FreeDict part-of-speech tags for this headword (may be empty)."""
    rows = connection.execute(
        "select distinct pos from dict_entries where (lemma = ? or lemma = ?) and pos is not null",
        dict_lookup_params(word),
    ).fetchall()
    return {pos for (pos,) in rows if pos}


def has_translation(connection: sqlite3.Connection, word: str) -> bool:
    """Is there a dictionary headword for this word?

    Frequency-list entries are lowercase; FreeDict capitalises nouns. Two
    indexed lookups (lowercase and capitalised) cover both spellings without a
    case-insensitive scan over 517k rows.
    """
    rows = connection.execute(
        "select 1 from dict_entries where lemma = ? or lemma = ? limit 1",
        dict_lookup_params(word),
    ).fetchone()
    return rows is not None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=0, help="only read the first N frequency words (debugging)")
    args = parser.parse_args()

    import spacy

    settings = get_settings()
    if not settings.database_path.exists():
        print(f"error: no database at {settings.database_path} — run the imports first", file=sys.stderr)
        return 1

    # The app creates tables on startup; the script may run before the app has
    # ever started, so make sure `placement_items` exists.
    create_schema(make_engine(settings.database_path))

    connection = sqlite3.connect(settings.database_path)
    try:
        freq_rows = connection.execute(
            "select word, rank from word_freq order by rank"
        ).fetchall()
        if not freq_rows:
            print("error: word_freq is empty — run scripts/import_freq.py first", file=sys.stderr)
            return 1

        if args.limit:
            freq_rows = freq_rows[: args.limit]

        # Cheap filters first: no point spending spaCy time on `chffffff`.
        candidates = [
            (word, rank)
            for word, rank in freq_rows
            if band_for_rank(rank) is not None and len(word) >= 3 and word.isalpha()
        ]

        with_translation = [
            (word, rank) for word, rank in candidates if has_translation(connection, word)
        ]

        nlp = spacy.load("de_core_news_sm")
        # Lemma -> (band, rank). Keyed by the dictionary form so the same word
        # cannot appear in two bands, and so the check asks about "gehen" rather
        # than "geht": the word list stores lemmas too.
        by_lemma: dict[str, tuple[int, int]] = {}
        rejected_by_pos: dict[str, int] = {}
        rejected_by_blocklist = 0
        rejected_no_lemma_entry = 0

        for start in range(0, len(with_translation), CHUNK):
            chunk = with_translation[start : start + CHUNK]
            for (word, rank), doc in zip(chunk, nlp.pipe([w for w, _ in chunk], batch_size=256)):
                token = doc[0] if len(doc) else None
                pos = token.pos_ if token is not None else "X"
                lemma = word
                if token is not None and token.lemma_.isalpha() and len(token.lemma_) >= 3:
                    lemma = token.lemma_

                if is_blocked(lemma):
                    rejected_by_blocklist += 1
                    continue
                if not has_translation(connection, lemma):
                    rejected_no_lemma_entry += 1
                    continue

                word_class = choose_word_class(
                    dict_positions=dict_positions(connection, lemma), spacy_pos=pos, word=lemma
                )
                if word_class is None:
                    rejected_by_pos[pos] = rejected_by_pos.get(pos, 0) + 1
                    continue

                # German nouns are capitalised; the frequency list is lowercase.
                display = lemma[:1].upper() + lemma[1:] if word_class == "noun" else lemma
                band_index = band_for_rank(rank)
                assert band_index is not None
                previous = by_lemma.get(display)
                if previous is None or rank < previous[1]:
                    by_lemma[display] = (band_index, rank)

            print(f"  tagged {min(start + CHUNK, len(with_translation)):,}/{len(with_translation):,}")

        kept = sorted(
            ((word, band, rank) for word, (band, rank) in by_lemma.items()), key=lambda row: row[2]
        )

        with connection:
            connection.execute("delete from placement_items")
            connection.executemany(
                "insert into placement_items (word, band_index, rank) values (?, ?, ?)", kept
            )

        print(f"\nfrequency words read: {len(freq_rows):,}")
        print(f"  dropped: not alphabetic/too short/rank out of range: {len(freq_rows) - len(candidates):,}")
        print(f"  dropped: no dictionary headword: {len(candidates) - len(with_translation):,}")
        print(f"  dropped: part of speech not in {sorted(ALLOWED_POS)}: {sum(rejected_by_pos.values()):,} {rejected_by_pos}")
        print(f"  dropped: editorially excluded (POOL_BLOCKLIST): {rejected_by_blocklist:,}")
        print(f"  dropped: lemma not in the dictionary: {rejected_no_lemma_entry:,}")
        print(f"kept (unique lemmas): {len(kept):,}")

        print("\nper band:")
        for index, (low, high) in enumerate(BANDS):
            words = [word for word, band, _ in kept if band == index]
            print(f"  band {index} (ranks {low:,}-{high:,}): {len(words):,} words")
            print(f"    first 10: {', '.join(words[:10])}")

        return 0
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())

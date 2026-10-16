"""Import the FrequencyWords German list into `word_freq`.

Usage:
    uv run python scripts/import_freq.py              # uses data/de_50k.txt
    uv run python scripts/import_freq.py --limit 1000

Source file format (from hermitdave/FrequencyWords, `content/2018/de/de_50k.txt`):
one `word count` pair per line, sorted most-frequent first. The line number is
the rank, so the rank comes from file order and is never invented.

Re-running is safe: the table is replaced wholesale inside one transaction.
"""

import argparse
import sys
import time
from collections.abc import Iterable, Iterator
from pathlib import Path

# Allow both `python scripts/import_freq.py` and `python -m scripts.import_freq`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import delete, insert, select, func  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db import create_schema, make_engine  # noqa: E402
from app.models import WordFreq  # noqa: E402

BATCH_SIZE = 5000


def parse_freq_lines(lines: Iterable[str], limit: int | None = None) -> Iterator[tuple[str, int, int]]:
    """Yield (word, rank, count) from raw frequency-list lines.

    Malformed lines are skipped rather than crashing the import — a data file
    we do not control should never break setup.
    """
    emitted = 0
    for position, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped:
            continue
        parts = stripped.split()
        if len(parts) != 2:
            continue
        word, raw_count = parts
        try:
            count = int(raw_count)
        except ValueError:
            continue
        if count <= 0:
            continue
        yield word, position, count
        emitted += 1
        if limit is not None and emitted >= limit:
            return


def import_frequency(db_path: Path, freq_path: Path, limit: int | None = None) -> int:
    """Replace word_freq with a fresh parse of the frequency list."""
    engine = make_engine(db_path)
    create_schema(engine)

    started = time.monotonic()
    with open(freq_path, encoding="utf-8") as fh:
        parsed = list(parse_freq_lines(fh, limit=limit))

    # A word can appear once only (the file is a ranking); de-duplicate by keeping
    # the best rank, so the primary key can never be violated.
    best: dict[str, tuple[int, int]] = {}
    for word, rank, count in parsed:
        if word not in best or rank < best[word][0]:
            best[word] = (rank, count)

    rows = [{"word": word, "rank": rank, "count": count} for word, (rank, count) in best.items()]

    with Session(engine) as session:
        session.execute(delete(WordFreq))
        for start in range(0, len(rows), BATCH_SIZE):
            session.execute(insert(WordFreq), rows[start : start + BATCH_SIZE])
        session.commit()
        stored = session.scalar(select(func.count()).select_from(WordFreq))

    print(
        f"import_freq: {len(parsed):,} lines parsed, {stored:,} distinct words stored, "
        f"{time.monotonic() - started:.1f}s"
    )
    return len(rows)


def main() -> int:
    settings = get_settings()
    parser = argparse.ArgumentParser(description="Import the de_50k frequency list into word_freq.")
    parser.add_argument("--freq", type=Path, default=settings.freq_path)
    parser.add_argument("--db", type=Path, default=settings.database_path)
    parser.add_argument("--limit", type=int, default=None, help="stop after N lines (smoke runs)")
    args = parser.parse_args()

    if not args.freq.exists():
        print(
            f"error: frequency file not found: {args.freq}\n"
            "Download it first — see README → Data setup.",
            file=sys.stderr,
        )
        return 1

    import_frequency(args.db, args.freq, limit=args.limit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

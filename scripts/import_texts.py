"""Import public-domain German texts from Project Gutenberg into the library.

Usage:
    uv run python scripts/import_texts.py                  # every manifest entry
    uv run python scripts/import_texts.py --limit 3        # first 3 (smoke run)
    uv run python scripts/import_texts.py --id 74008       # one text by id
    uv run python scripts/import_texts.py --force          # re-import existing

The manifest (`library/texts.json`, committed) lists the titles, authors, source
URLs and licences. The text bodies are NOT committed: each one is downloaded
once into `data/texts/` (gitignored) and cached there, so a re-run is offline.

Re-running is safe: a text whose `source_url` is already in the database is
skipped, unless `--force` is given. Import only writes to `texts` and `tokens`:
`word_status` and `user_words` are never touched, and the before/after row
counts prove it.

Licence: every author in the manifest died more than 70 years ago. The works are
public domain in Germany and marked public domain in the USA by Project
Gutenberg; the manifest records that per title.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

# Allow both `python scripts/import_texts.py` and `python -m scripts.import_texts`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import delete, func, select, update  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.config import PROJECT_ROOT, get_settings  # noqa: E402
from app.db import create_schema, make_engine  # noqa: E402
from app.knowledge import text_coverage  # noqa: E402
from app.models import TextRecord, Token, UserWord, WordStatus  # noqa: E402
from app.pipeline import ModelNotInstalled, tokenize  # noqa: E402

MANIFEST_PATH = PROJECT_ROOT / "library" / "texts.json"
TOKEN_BATCH = 20_000
FETCH_RETRIES = 3
USER_AGENT = "wortgut-import/1.0 (local reading app)"

START_MARKER = "*** START OF THE PROJECT GUTENBERG EBOOK"
END_MARKER = "*** END OF THE PROJECT GUTENBERG EBOOK"


@dataclass(frozen=True)
class ImportResult:
    """What one manifest entry produced, for the report and the tests."""

    gutenberg_id: int
    title: str
    text_id: int | None
    tokens: int
    skipped: bool


def load_manifest(path: Path = MANIFEST_PATH) -> list[dict]:
    """Read the committed manifest. Raises with a clear message when absent."""
    if not path.exists():
        raise FileNotFoundError(
            f"manifest not found: {path}. It is committed as library/texts.json."
        )
    payload = json.loads(path.read_text(encoding="utf-8"))
    texts = payload.get("texts")
    if not isinstance(texts, list) or not texts:
        raise ValueError(f"{path} has no 'texts' list")
    return texts


def strip_gutenberg_boilerplate(raw: str) -> str:
    """Return only the book body, between Gutenberg's two marker lines.

    Project Gutenberg wraps every text in an English header and footer. The
    markers are stable; when a text is missing them the whole input is returned
    rather than guessing where the body starts.
    """
    start = raw.find(START_MARKER)
    end = raw.find(END_MARKER, start + 1 if start != -1 else 0)
    if start == -1 or end == -1 or end <= start:
        return raw.strip()

    # Cut after the marker line itself, not mid-line.
    body_start = raw.find("\n", start)
    body_start = 0 if body_start == -1 else body_start + 1
    return raw[body_start:end].strip()


def download_text(entry: dict, cache_dir: Path) -> str:
    """Fetch one text with the system `curl`, caching it on disk.

    The Python code stays free of HTTP libraries, the same way `fetch_data.sh`
    keeps the app offline-capable: network access lives in the shell tool. A
    re-run reads the cache and never touches the network.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    target = cache_dir / f"pg{entry['gutenberg_id']}.txt"
    if target.exists() and target.stat().st_size > 0:
        return target.read_text(encoding="utf-8", errors="replace")

    url = entry["text_url"]
    temporary = target.with_suffix(".tmp")
    last_error = ""
    for attempt in range(FETCH_RETRIES):
        completed = subprocess.run(
            [
                "curl", "-fSL", "--retry", "2", "--max-time", "120",
                "-A", USER_AGENT, "-o", str(temporary), url,
            ],
            capture_output=True,
            text=True,
        )
        if completed.returncode == 0 and temporary.exists() and temporary.stat().st_size > 0:
            # Rename only after a complete download, so a failed run leaves no half file.
            temporary.replace(target)
            return target.read_text(encoding="utf-8", errors="replace")
        last_error = completed.stderr.strip() or f"curl exited {completed.returncode}"
        temporary.unlink(missing_ok=True)
        time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"could not download {url}: {last_error}")


def import_text(session: Session, entry: dict, raw: str) -> ImportResult:
    """Store one text and its tokens. Idempotent on `source_url`.

    Writes only `texts` and `tokens`; the learner's status model is untouched.
    """
    source_url = entry["source_url"]
    existing = session.scalar(select(TextRecord).where(TextRecord.source_url == source_url))
    if existing is not None:
        return ImportResult(
            gutenberg_id=entry["gutenberg_id"],
            title=existing.title or entry["title"],
            text_id=existing.id,
            tokens=0,
            skipped=True,
        )

    body = strip_gutenberg_boilerplate(raw)
    drafts = tokenize(body)
    if not drafts:
        raise ValueError(f"{entry['title']} produced no tokens")

    text_record = TextRecord(
        title=entry["title"],
        source_note=f"{entry['author']} · Project Gutenberg #{entry['gutenberg_id']}",
        source_url=source_url,
        license=entry["license"],
    )
    session.add(text_record)
    session.flush()  # need the id before the token rows can point at it

    rows = [
        {
            "text_id": text_record.id,
            "position": draft.position,
            "surface": draft.surface,
            "lemma": draft.lemma,
            "pos": draft.pos,
            "is_word": draft.is_word,
            "whitespace": draft.whitespace,
        }
        for draft in drafts
    ]
    for start in range(0, len(rows), TOKEN_BATCH):
        session.bulk_insert_mappings(Token, rows[start : start + TOKEN_BATCH])
    session.commit()

    return ImportResult(
        gutenberg_id=entry["gutenberg_id"],
        title=text_record.title,
        text_id=text_record.id,
        tokens=len(rows),
        skipped=False,
    )


def _table_counts(session: Session) -> tuple[int, int]:
    """Row counts for the two tables an import must never change."""
    words = session.scalar(select(func.count()).select_from(WordStatus)) or 0
    saved = session.scalar(select(func.count()).select_from(UserWord)) or 0
    return words, saved


def _force_delete(session: Session, text_id: int) -> None:
    """Remove one text for `--force`, preserving the learner's saved words.

    `user_words.first_seen_text_id` only records where a word was first met; it
    is nulled rather than deleting the word itself.
    """
    session.execute(update(UserWord).where(UserWord.first_seen_text_id == text_id).values(first_seen_text_id=None))
    session.execute(delete(Token).where(Token.text_id == text_id))
    session.execute(delete(TextRecord).where(TextRecord.id == text_id))
    session.commit()


def run_import(
    engine,
    entries: list[dict],
    *,
    cache_dir: Path,
    force: bool = False,
    progress=print,
) -> tuple[list[ImportResult], tuple[int, int], tuple[int, int]]:
    """Import every entry and return results plus before/after counts."""
    with Session(engine) as session:
        before = _table_counts(session)

        results: list[ImportResult] = []
        for index, entry in enumerate(entries, start=1):
            progress(f"[{index}/{len(entries)}] {entry['title']} (#{entry['gutenberg_id']})")
            existing = session.scalar(
                select(TextRecord).where(TextRecord.source_url == entry["source_url"])
            )
            if existing is not None and not force:
                # Checked before the download: a skipped text must not hit the network.
                results.append(
                    ImportResult(
                        gutenberg_id=entry["gutenberg_id"],
                        title=existing.title or entry["title"],
                        text_id=existing.id,
                        tokens=0,
                        skipped=True,
                    )
                )
                progress(f"    skipped (already imported, id {existing.id})")
                continue
            if existing is not None:
                _force_delete(session, existing.id)

            raw = download_text(entry, cache_dir)
            result = import_text(session, entry, raw)
            results.append(result)
            progress(
                f"    skipped (already imported, id {result.text_id})"
                if result.skipped
                else f"    imported {result.tokens:,} tokens as text {result.text_id}"
            )

        after = _table_counts(session)
        return results, before, after


def _print_report(engine, results: list[ImportResult], before, after) -> None:
    imported = [r for r in results if not r.skipped]
    skipped = [r for r in results if r.skipped]
    print()
    print(f"imported {len(imported)} texts, skipped {len(skipped)} already present")
    print(f"tokens written: {sum(r.tokens for r in imported):,}")
    print(f"word_status rows  before {before[0]:,}  after {after[0]:,}")
    print(f"user_words rows   before {before[1]:,}  after {after[1]:,}")
    if before != after:
        raise SystemExit("error: import changed word_status or user_words — that must not happen")

    if not imported:
        return

    print()
    print(f"{'text':<48} {'words':>6} {'known%':>7} {'new':>6} {'unrated':>8} {'unrated%':>9}")
    with Session(engine) as session:
        for result in imported:
            if result.text_id is None:  # import_text always sets it; keeps the type honest
                continue
            coverage = text_coverage(session, result.text_id)
            share = 100 * coverage.unrated_words / coverage.total_words if coverage.total_words else 0
            percent = "—" if coverage.percent is None else f"{coverage.percent}%"
            title = (result.title or "")[:46]
            print(
                f"{title:<48} {coverage.total_words:>6,} {percent:>7} "
                f"{coverage.new_words:>6,} {coverage.unrated_words:>8,} {share:>8.1f}%"
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Import public-domain German texts from Project Gutenberg.")
    parser.add_argument("--manifest", type=Path, default=MANIFEST_PATH, help="path to the manifest JSON")
    parser.add_argument("--limit", type=int, default=None, help="import at most N entries")
    parser.add_argument("--id", type=int, action="append", default=None, help="import only this Gutenberg id (repeatable)")
    parser.add_argument("--force", action="store_true", help="re-import texts that are already present")
    parser.add_argument("--cache-dir", type=Path, default=None, help="where downloaded texts are cached")
    args = parser.parse_args(argv)

    settings = get_settings()
    cache_dir = args.cache_dir or (settings.data_dir / "texts")
    engine = make_engine(settings.database_path)
    create_schema(engine)

    entries = load_manifest(args.manifest)
    if args.id:
        wanted = set(args.id)
        entries = [e for e in entries if e["gutenberg_id"] in wanted]
        if not entries:
            print(f"error: no manifest entry matches {sorted(wanted)}", file=sys.stderr)
            return 2
    if args.limit is not None:
        entries = entries[: args.limit]

    try:
        results, before, after = run_import(engine, entries, cache_dir=cache_dir, force=args.force)
    except ModelNotInstalled as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    _print_report(engine, results, before, after)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Import the FreeDict German–English dictionary into `dict_entries`.

Usage:
    uv run python scripts/import_dict.py            # uses data/deu-eng.tei
    uv run python scripts/import_dict.py --limit 500  # quick smoke run

The TEI file is ~450 MB and holds 517k entries, so it is streamed with
`iterparse` and each element is cleared after use — memory stays flat no matter
how large the file gets.

Re-running is safe: rows from this source are deleted and re-inserted inside
one transaction, so a failed run never leaves a half-imported dictionary.
"""

import argparse
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree

# Allow both `python scripts/import_dict.py` and `python -m scripts.import_dict`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import delete, insert, select, func  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db import create_schema, make_engine  # noqa: E402
from app.models import DictEntry  # noqa: E402

TEI_NS = "{http://www.tei-c.org/ns/1.0}"
SOURCE_NAME = "freedict-deu-eng"
MAX_TRANSLATIONS = 5  # enough to be useful in a popup, short enough to read
BATCH_SIZE = 5000


@dataclass(frozen=True)
class Entry:
    """One dictionary entry, reduced to what the app actually needs."""

    lemma: str
    pos: str | None
    translation: str


def _text_of(element: ElementTree.Element | None) -> str:
    """All text inside an element (quotes may contain nested markup)."""
    if element is None:
        return ""
    return "".join(element.itertext()).strip()


def parse_entry(element: ElementTree.Element) -> Entry | None:
    """Convert one <entry> element into an Entry, or None if it has no usable sense.

    The schema nests <gramGrp> both at entry level and inside each translation
    citation; each is addressed explicitly so the entry-level part-of-speech
    never picks up a nested one.
    """
    orth = element.find(f"{TEI_NS}form/{TEI_NS}orth")
    lemma = _text_of(orth)
    if not lemma:
        return None

    gram_grp = element.find(f"{TEI_NS}gramGrp")
    pos = _text_of(gram_grp.find(f"{TEI_NS}pos")) if gram_grp is not None else ""

    translations: list[str] = []
    for citation in element.iterfind(f".//{TEI_NS}cit[@type='trans']"):
        quote = _text_of(citation.find(f"{TEI_NS}quote"))
        if quote and quote not in translations:
            translations.append(quote)
        if len(translations) >= MAX_TRANSLATIONS:
            break

    if not translations:
        return None

    return Entry(lemma=lemma, pos=pos or None, translation="; ".join(translations))


def parse_entries(path: Path, limit: int | None = None) -> Iterator[Entry]:
    """Stream entries out of the TEI file, one element at a time."""
    seen = 0
    for event, element in ElementTree.iterparse(path, events=("end",)):
        if element.tag != f"{TEI_NS}entry":
            continue
        entry = parse_entry(element)
        if entry is not None:
            yield entry
            seen += 1
        element.clear()
        if limit is not None and seen >= limit:
            break


def import_dictionary(db_path: Path, tei_path: Path, limit: int | None = None) -> int:
    """Replace the dictionary rows with a fresh parse. Returns the row count."""
    admin_engine = make_engine(db_path)
    create_schema(admin_engine)

    started = time.monotonic()
    with Session(admin_engine) as session:
        session.execute(delete(DictEntry).where(DictEntry.source == SOURCE_NAME))
        session.commit()

        batch: list[dict[str, str | None]] = []
        total = 0
        for entry in parse_entries(tei_path, limit=limit):
            batch.append(
                {
                    "lemma": entry.lemma,
                    "pos": entry.pos,
                    "translation": entry.translation,
                    "source": SOURCE_NAME,
                }
            )
            if len(batch) >= BATCH_SIZE:
                session.execute(insert(DictEntry), batch)
                total += len(batch)
                batch = []
                if total % 50000 == 0:
                    print(f"  {total:,} entries inserted…", flush=True)
        if batch:
            session.execute(insert(DictEntry), batch)
            total += len(batch)
        session.commit()

        stored = session.scalar(
            select(func.count()).select_from(DictEntry).where(DictEntry.source == SOURCE_NAME)
        )

    print(
        f"import_dict: {total:,} entries parsed, {stored:,} in database, "
        f"{time.monotonic() - started:.1f}s"
    )
    return total


def main() -> int:
    settings = get_settings()
    parser = argparse.ArgumentParser(description="Import FreeDict deu-eng into dict_entries.")
    parser.add_argument("--tei", type=Path, default=settings.dict_tei_path)
    parser.add_argument("--db", type=Path, default=settings.database_path)
    parser.add_argument("--limit", type=int, default=None, help="stop after N entries (smoke runs)")
    args = parser.parse_args()

    if not args.tei.exists():
        print(
            f"error: TEI file not found: {args.tei}\n"
            "Download it first — see README → Data setup.",
            file=sys.stderr,
        )
        return 1

    import_dictionary(args.db, args.tei, limit=args.limit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

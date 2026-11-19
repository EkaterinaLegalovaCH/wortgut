"""Repair stored lemmas in one existing database, using app/lemmas.py.

The app repairs lemmas on ingest, so any text imported after this change is
already clean. This script fixes what is already in the file: token rows whose
lemma is a known spaCy mistake are rewritten to the real word, and the derived
tables (`text_lemmas`, `word_status`, `user_words`) are moved to the new key so
that coverage, ranking and saved words all agree afterwards.

It is idempotent: a corrected lemma is not itself a key in the table, so running
it twice changes nothing the second time.

Usage (always on a copy first — see the README):
    uv run python scripts/fix_lemmas.py --db data/wortgut.sqlite3 --dry-run
    uv run python scripts/fix_lemmas.py --db data/wortgut.sqlite3 --apply
    uv run python scripts/fix_lemmas.py --db data/wortgut.sqlite3 --undo out.json

`--dry-run` is read-only and prints every mapping plus how many rows and tokens
it would touch. `--apply` writes, takes a verified backup through SQLite's own
backup API (a file copy would miss the WAL), and saves a JSON undo record;
`--undo` reverses one exactly, by token id.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypedDict

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.knowledge import STATUS_RANK, key  # noqa: E402
from app.lemmas import LEMMA_FIXES  # noqa: E402


class PlanRow(TypedDict):
    """One repair as it applies to this database."""

    bad: str
    good: str
    why: str
    tokens: int
    text_lemmas: int
    status: dict[str, Any] | None
    user_words: list[dict[str, Any]]


def _connect(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise SystemExit(f"error: database not found: {path}")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def plan(conn: sqlite3.Connection) -> list[PlanRow]:
    """One row per mapping that actually exists in this database."""
    cur = conn.cursor()
    out: list[PlanRow] = []
    for bad, (good, why) in sorted(LEMMA_FIXES.items(), key=lambda kv: kv[0].lower()):
        # case-insensitive: spaCy casing varies between texts. Proper nouns are
        # excluded on purpose — `FRISST` in a scanned text is a name, not the
        # verb, and a name is not vocabulary the learner needs a lemma for.
        token_rows = cur.execute(
            "select count(*) from tokens where lower(trim(lemma)) = ? and pos != 'PROPN'",
            (bad.lower(),),
        ).fetchone()[0]
        text_lemma_rows = cur.execute(
            "select count(*) from text_lemmas where lower(trim(lemma)) = ?", (bad.lower(),)
        ).fetchone()[0]
        status_row = cur.execute(
            "select lemma, status, source, encounters from word_status where lower(trim(lemma)) = ?",
            (bad.lower(),),
        ).fetchone()
        user_rows = cur.execute(
            "select id, lemma from user_words where lower(trim(lemma)) = ?", (bad.lower(),)
        ).fetchall()
        if not (token_rows or text_lemma_rows or status_row or user_rows):
            continue
        # `text_lemmas` is derived and is rebuilt from `tokens`, so a mapping
        # whose only remaining hits are proper-noun rows (which are skipped by
        # design) has nothing to do. Without this, a second run reports work
        # that it will not perform, and idempotency looks untrue.
        if not token_rows and not status_row and not user_rows:
            continue
        out.append(
            PlanRow(
                bad=bad,
                good=good,
                why=why,
                tokens=token_rows,
                text_lemmas=text_lemma_rows,
                status=dict(status_row) if status_row else None,
                user_words=[dict(r) for r in user_rows],
            )
        )
    return out


def print_plan(rows: list[PlanRow]) -> None:
    if not rows:
        print("nothing to do — no stored lemma matches the repair table")
        return
    print(f"{'bad lemma':16} {'-> real word':14} {'tokens':>7} {'tlemmas':>8}  status / saved")
    print("-" * 84)
    for row in rows:
        status = row["status"]
        detail = ""
        if status:
            detail = f"{status['lemma']}={status['status']}/{status['source']} enc={status['encounters']}"
        saves = row["user_words"] or []
        if saves:
            detail += f"  |  saved: {', '.join(w['lemma'] for w in saves)}"
        print(
            f"{str(row['bad']):16} {str(row['good']):14} {int(row['tokens']):7} "
            f"{int(row['text_lemmas']):8}  {detail}"
        )
    print("-" * 84)
    print(f"mappings that apply here: {len(rows)}")
    print(f"token rows rewritten:     {sum(int(r['tokens']) for r in rows):,}")


def _rebuild_text_lemmas(cur: sqlite3.Cursor, text_id: int) -> None:
    """Rewrite one text's `text_lemmas` rows from its (already repaired) tokens.

    Uses the app's `key()` so the stored lemmas match what `index_text_lemmas`
    and `coverage_by_text` produce — SQLite's `lower()` is ASCII-only and would
    leave `Äußerung` capitalised, which silently unmatches those words.
    """
    lemmas = {
        key(lemma)
        for (lemma,) in cur.execute(
            "select lemma from tokens where text_id = ? and is_word = 1", (text_id,)
        ).fetchall()
    }
    existing = {
        row[0]
        for row in cur.execute(
            "select lemma from text_lemmas where text_id = ?", (text_id,)
        ).fetchall()
    }
    missing = lemmas - existing
    if missing:
        cur.executemany(
            "insert into text_lemmas (text_id, lemma) values (?, ?)",
            [(text_id, lemma) for lemma in missing],
        )


def apply_plan(conn: sqlite3.Connection, rows: list[PlanRow]) -> dict[str, Any]:
    """Rewrite the rows and return an undo record."""
    cur = conn.cursor()
    undo: dict[str, Any] = {"created_at": datetime.now().isoformat(timespec="seconds"), "changes": []}

    for row in rows:
        bad, good = str(row["bad"]), str(row["good"])
        entry: dict[str, Any] = {"bad": bad, "good": good, "collisions": []}

        # 1) tokens: the materialised text. Record the exact row id AND its
        #    original lemma before rewriting. The undo must restore only these
        #    rows, and must restore their original casing (`Gethan` and `gethan`
        #    both match the table case-insensitively but are different rows).
        #    A naive `update tokens set lemma = bad where lemma = good` would
        #    also mangle every occurrence of the corrected word that was always
        #    right (`thun`->`tun` must not turn 2,039 real `tun` rows into `thun`).
        changed = cur.execute(
            "select id, lemma from tokens where lower(trim(lemma)) = ? and pos != 'PROPN'",
            (bad.lower(),),
        ).fetchall()
        entry["token_lemmas"] = [[row[0], row[1]] for row in changed]
        cur.execute(
            "update tokens set lemma = ? where lower(trim(lemma)) = ? and pos != 'PROPN'",
            (good, bad.lower()),
        )

        # 2) text_lemmas: derived, one row per (text_id, lemma). Deleting the old
        #    lemma is not enough — the corrected word may not have been in this
        #    text before, so it must be inserted too, or the ranking silently
        #    loses a word. Rebuild the affected texts in Python rather than SQL:
        #    SQLite's lower() is ASCII-only, so it leaves `Äußerung` capitalised
        #    and the rows would stop matching the tokens.
        affected = {
            r[0]
            for r in cur.execute(
                "select distinct text_id from text_lemmas where lower(trim(lemma)) = ?",
                (bad.lower(),),
            ).fetchall()
        }
        affected |= {
            r[0]
            for r in cur.execute(
                "select distinct text_id from tokens where lemma = ? and pos != 'PROPN'",
                (good,),
            ).fetchall()
        }
        entry["texts_touched"] = sorted(affected)
        cur.execute("delete from text_lemmas where lower(trim(lemma)) = ?", (bad.lower(),))
        for text_id in sorted(affected):
            _rebuild_text_lemmas(cur, text_id)

        # 3) word_status: the model's opinion. If the corrected word already has
        #    a row, keep the one with real evidence (non-placement source) and
        #    delete the broken key — otherwise the learner's own evidence would
        #    be thrown away by a mechanical repair.
        broken_status = cur.execute(
            "select lemma, status, source, encounters, band_index, last_credited_day"
            " from word_status where lower(trim(lemma)) = ?",
            (bad.lower(),),
        ).fetchone()
        if broken_status:
            existing = cur.execute(
                "select lemma, status, source, encounters, band_index, last_credited_day"
                " from word_status where lemma = ?",
                (good,),
            ).fetchone()
            entry["collisions"].append(
                {"broken": dict(broken_status), "existing": dict(existing) if existing else None}
            )
            if existing is None:
                cur.execute("update word_status set lemma = ? where lemma = ?", (good, broken_status["lemma"]))
            else:
                # Both keys have a row: one word, two opinions. Keep the stronger
                # one — the two rows describe the same word, so a repair must not
                # demote the learner. `aller`(learning) -> `all`(familiar) has to
                # stay familiar; the reverse would lose a word she demonstrably
                # knows. Ties go to the non-placement row (real evidence beats a
                # placement seed).
                broken_rank = STATUS_RANK.get(str(broken_status["status"]), -1)
                existing_rank = STATUS_RANK.get(str(existing["status"]), -1)
                if broken_rank > existing_rank or (
                    broken_rank == existing_rank
                    and broken_status["source"] != "placement"
                    and existing["source"] == "placement"
                ):
                    survivors = {
                        "status": broken_status["status"],
                        "source": broken_status["source"],
                        "encounters": broken_status["encounters"],
                        "band_index": broken_status["band_index"],
                        "last_credited_day": broken_status["last_credited_day"],
                        "lemma": broken_status["lemma"],
                    }
                    cur.execute("delete from word_status where lemma = ?", (good,))
                    cur.execute(
                        "update word_status set lemma = ?, status = ?, source = ?, encounters = ?,"
                        " band_index = ?, last_credited_day = ? where lemma = ?",
                        (
                            good, survivors["status"], survivors["source"], survivors["encounters"],
                            survivors["band_index"], survivors["last_credited_day"], survivors["lemma"],
                        ),
                    )
                else:
                    cur.execute("delete from word_status where lemma = ?", (broken_status["lemma"],))

        # 4) user_words: her own saved list. A saved row must never be lost, and
        #    the undo has to be able to put it back under its old lemma.
        renames = [{"id": w["id"], "from": w["lemma"]} for w in (row["user_words"] or [])]
        entry["user_word_renames"] = renames
        for saved in row["user_words"] or []:
            clash = cur.execute(
                "select id, encounters from user_words where lemma = ?", (good,)
            ).fetchone()
            if clash is None:
                cur.execute("update user_words set lemma = ? where id = ?", (good, saved["id"]))
            else:
                # Both spellings saved: fold the encounters into the surviving
                # row so the count still reflects how often she met the word.
                cur.execute(
                    "update user_words set encounters = encounters + ? where id = ?",
                    (saved.get("encounters", 1), clash["id"]),
                )
                cur.execute("delete from user_words where id = ?", (saved["id"],))

        undo["changes"].append(entry)

    conn.commit()
    return undo


def undo_apply(conn: sqlite3.Connection, record: dict[str, Any]) -> None:
    """Reverse a previous --apply using its undo record.

    Rewrites the stored lemmas back and restores any `word_status` row the
    repair removed. The whole reversal runs in one transaction: a half-undone
    migration is worse than either end state, because tokens and the derived
    tables would disagree.

    `text_lemmas` is derived. The rows added for the corrected lemma are removed
    here so the table matches the restored tokens; the app re-indexes anything
    missing on the next start (`ensure_indexed`).
    """
    cur = conn.cursor()
    try:
        for change in record.get("changes", []):
            bad, good = change["bad"], change["good"]

            # tokens: restore exactly the rows the forward pass rewrote, by id,
            # with their original casing.
            pairs = change.get("token_lemmas") or []
            for start in range(0, len(pairs), 500):
                chunk = pairs[start : start + 500]
                cur.executemany(
                    "update tokens set lemma = ? where id = ?",
                    [(original, token_id) for token_id, original in chunk],
                )

            # user_words: put each saved row back under the lemma it had.
            for rename in change.get("user_word_renames") or []:
                cur.execute(
                    "update user_words set lemma = ? where id = ?",
                    (rename["from"], rename["id"]),
                )

            # text_lemmas: derived. After the token lemmas are restored, rebuild
            # each touched text from scratch for the two lemmas involved, so a
            # row that was always `good` is not dropped.
            for text_id in change.get("texts_touched", []):
                cur.execute(
                    "delete from text_lemmas where text_id = ? and lower(trim(lemma)) in (lower(trim(?)), lower(trim(?)))",
                    (text_id, good, bad),
                )
                _rebuild_text_lemmas(cur, text_id)

            # word_status: restore every row the repair deleted, including the
            # NOT NULL columns the forward pass never touched.
            for collision in change.get("collisions", []):
                broken = collision.get("broken")
                if broken:
                    cur.execute(
                        "insert or replace into word_status"
                        " (lemma, status, source, encounters, band_index,"
                        "  last_credited_day, updated_at)"
                        " values (?,?,?,?,?,?,?)",
                        (
                            broken["lemma"], broken["status"], broken["source"],
                            broken["encounters"], broken["band_index"],
                            broken["last_credited_day"],
                            datetime.now(timezone.utc).isoformat(sep=" "),
                        ),
                    )
                existing = collision.get("existing")
                if existing:
                    # The repair may have deleted the corrected word's row to
                    # keep the broken one; put it back exactly as recorded.
                    cur.execute(
                        "insert or replace into word_status"
                        " (lemma, status, source, encounters, band_index,"
                        "  last_credited_day, updated_at)"
                        " values (?,?,?,?,?,?,?)",
                        (
                            existing["lemma"], existing["status"], existing["source"],
                            existing["encounters"], existing.get("band_index"),
                            existing.get("last_credited_day"),
                            datetime.now(timezone.utc).isoformat(sep=" "),
                        ),
                    )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    print("undone: token lemmas restored, word_status and text_lemmas rebuilt")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", type=Path, default=Path("data/wortgut.sqlite3"))
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true", help="read-only: print what would change")
    group.add_argument("--apply", action="store_true", help="write the repairs")
    group.add_argument("--undo", type=Path, metavar="RECORD", help="reverse a previous --apply")
    parser.add_argument("--no-backup", action="store_true", help="skip the automatic .bak copy (not recommended)")
    args = parser.parse_args()

    conn = _connect(args.db)

    if args.undo:
        record = json.loads(args.undo.read_text(encoding="utf-8"))
        undo_apply(conn, record)
        return

    rows = plan(conn)
    print(f"database: {args.db}")
    print_plan(rows)

    if args.dry_run or not rows:
        if args.dry_run:
            print("\n(dry run — nothing written)")
        return

    if not args.no_backup:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = args.db.with_name(f"{args.db.name}.bak-{stamp}")
        print(f"\nbacking up to {backup} …")
        # Use SQLite's own backup API, never a file copy: the database runs in
        # WAL mode, so the main file alone is missing every commit still in the
        # `-wal` sidecar. A shutil.copy2 here silently produced a backup that
        # was hours stale (17 saved words instead of 25, 0 credits instead of
        # 562) — a backup that lies is worse than none.
        target = sqlite3.connect(backup)
        try:
            with target:
                conn.backup(target)
        finally:
            target.close()
        # Prove the backup is usable before touching anything.
        check = sqlite3.connect(f"file:{backup}?mode=ro", uri=True)
        try:
            ok = check.execute("pragma integrity_check").fetchone()[0]
            backed_up_words = check.execute("select count(*) from user_words").fetchone()[0]
        finally:
            check.close()
        print(f"  backup integrity: {ok}; user_words in backup: {backed_up_words}")
        if ok != "ok":
            raise SystemExit("error: backup failed its integrity check — aborting, nothing written")

    undo = apply_plan(conn, rows)
    out = Path(f"lemma-repair-undo-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json")
    out.write_text(json.dumps(undo, indent=2, default=str), encoding="utf-8")
    print(f"applied. undo record: {out}")
    print("restart the app so the derived tables are rebuilt from the repaired tokens.")


if __name__ == "__main__":
    main()

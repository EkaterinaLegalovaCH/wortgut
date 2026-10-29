"""Seed the known-word model from a stored placement run.

    uv run python scripts/seed_knowledge.py                 # latest run
    uv run python scripts/seed_knowledge.py --run-id 2      # a specific run
    uv run python scripts/seed_knowledge.py --policy known  # trust the profile fully

Normally the app seeds automatically when a placement check is submitted. This
script exists for two cases: an existing database whose run predates the feature,
and a deliberate re-seed after changing the policy.

What it writes: `word_status` rows for the curated pool words, `familiar` for
bands the run passed and `new` for the rest (policy `familiar`), or `known`
instead of `familiar` (policy `known`). Words the learner has already looked up
or saved keep their status — real evidence outranks a questionnaire.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy.orm import Session  # noqa: E402

from app.db import create_schema, get_engine  # noqa: E402
from app.knowledge import SEED_POLICIES, passed_bands, seed_from_placement  # noqa: E402
from app.models import PlacementRun  # noqa: E402
from app.placement import latest_run  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", type=int, default=0, help="placement run to seed from (default: latest)")
    parser.add_argument("--policy", default="familiar", choices=SEED_POLICIES)
    args = parser.parse_args()

    create_schema(get_engine())

    with Session(get_engine()) as session:
        run = session.get(PlacementRun, args.run_id) if args.run_id else latest_run(session)
        if run is None:
            print("error: no placement run found — take the placement check first", file=sys.stderr)
            return 1

        bands = passed_bands(session, run)
        print(f"run #{run.id}: seed {run.seed}, label {run.label}, thin_sample={run.thin_sample}")
        print(f"bands the run passes: {sorted(bands) or 'none'}")

        counts = seed_from_placement(session, run, policy=args.policy)
        total = sum(counts.values())
        print(f"policy {args.policy!r}: seeded {total:,} words")
        for status in ("known", "familiar", "learning", "new"):
            print(f"  {status:9} {counts.get(status, 0):,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Known-word model: per-lemma statuses, seeding from the placement check, and coverage.

The idea: every lemma the app has an opinion about gets a status, and a text's
coverage is just "how many of its words are already known". Two inputs feed it:

- the placement check, which says which frequency bands the learner recognises;
- real use: looking a word up means you did not know it, saving it means you
  want it.

Design notes:
- `word_status` is the model for all lemmas. `user_words` is the learner's list
  of saved words and also carries a status for display; `save_word` writes both
  through one function so they cannot drift apart.
- Seeding is a policy, not a hard-coded truth. `SEED_POLICY` says what a passed
  band means: `known` (believe the profile fully), `familiar` (believe it, but
  let real reading confirm it — the default), or nothing at all.
- Coverage is computed from statuses, and says when it has no basis: a fresh
  install with no run and no reading reports "no baseline", never "0% known".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from .models import PlacementResponse, PlacementRun, PlacementItem, TextRecord, Token, UserWord, WordStatus
from .placement import BAND_KNOWN_RATIO, BANDS, MIN_BAND_RESPONSES, Item, score

# new → learning → familiar → known
STATUSES: tuple[str, ...] = ("new", "learning", "familiar", "known")
STATUS_RANK: dict[str, int] = {status: rank for rank, status in enumerate(STATUSES)}

# Statuses that count as "known" when measuring coverage. `familiar` counts: the
# learner said they recognise the word in the placement check, or has met it
# repeatedly without needing it explained.
COVERED_STATUSES: frozenset[str] = frozenset({"familiar", "known"})

# What a band the learner passed means for the words in it.
#   "familiar" — seeded as familiar, promoted to known by real encounters (default)
#   "known"    — seeded as known, trusting the placement result outright
#   "none"     — seed nothing; the model learns only from reading
SEED_POLICIES: tuple[str, ...] = ("familiar", "known", "none")
SEED_POLICY = "familiar"

# Encounters needed to promote a word one step, when it is met and not looked up.
PROMOTION_ENCOUNTERS: dict[str, int] = {
    "new": 2,        # met twice without needing it → learning
    "learning": 3,   # met three more times → familiar
    "familiar": 3,   # met three more times → known
}


@dataclass(frozen=True)
class Coverage:
    """How much of one text the learner already knows."""

    total_words: int
    known_words: int
    new_words: int
    learning_words: int
    has_baseline: bool

    @property
    def percent(self) -> int | None:
        """Whole-percent coverage, or None when there is nothing to compare against."""
        if not self.has_baseline or self.total_words == 0:
            return None
        return round(100 * self.known_words / self.total_words)

    @property
    def summary(self) -> str:
        """The one-line form the reader shows: "% known · N new words"."""
        if not self.has_baseline:
            return f"{self.total_words} words · no level yet"
        return f"{self.percent}% known · {self.new_words} new words"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _upsert(session: Session, lemma: str, *, status: str, source: str, band_index: int | None = None) -> WordStatus:
    row = session.get(WordStatus, lemma)
    if row is None:
        row = WordStatus(
            lemma=lemma,
            status=status,
            source=source,
            band_index=band_index,
            encounters=0,
            updated_at=_now(),
        )
        session.add(row)
    else:
        row.status = status
        row.source = source
        if band_index is not None:
            row.band_index = band_index
        row.updated_at = _now()
    return row


def passed_bands(session: Session, run: PlacementRun) -> set[int]:
    """Bands this run says the learner knows, recomputed from the stored responses.

    The label is never used: it is a derived view, and seeding from it would put
    a rounded number in charge of thousands of words.
    """
    responses = session.scalars(
        select(PlacementResponse)
        .where(PlacementResponse.run_id == run.id)
        .order_by(PlacementResponse.position)
    ).all()
    if not responses:
        return set()

    items = [Item(word=row.word, band_index=row.band_index or 0) for row in responses]
    answers = [row.claimed_known for row in responses]
    profile = score(items, answers)
    return {band.index for band in profile.bands if band.known_band}


def seed_from_placement(
    session: Session,
    run: PlacementRun,
    *,
    policy: str = SEED_POLICY,
) -> dict[str, int]:
    """Seed word statuses from a placement run. Returns counts per status written.

    Only the curated pool is seeded: words outside it are rare or not in the
    frequency list, and inventing a status for them would be a guess.

    Real evidence outranks a questionnaire. Words the learner has looked up or
    saved keep their status — taking the check again re-calibrates the bands, it
    does not erase what actual reading taught the model.
    """
    if policy not in SEED_POLICIES:
        raise ValueError(f"unknown seed policy {policy!r}; expected one of {SEED_POLICIES}")

    counts = {status: 0 for status in STATUSES}
    if policy == "none":
        return counts

    known = passed_bands(session, run)
    passed_status = "known" if policy == "known" else "familiar"

    evidence = set(
        session.scalars(
            select(WordStatus.lemma).where(WordStatus.source.in_(("lookup", "save")))
        ).all()
    )

    pool = session.execute(select(PlacementItem.word, PlacementItem.band_index)).all()

    # Replace the previous seed wholesale (one delete + one bulk insert) rather
    # than updating row by row; the pool is ~14k words and a test takes minutes,
    # so this should not also take minutes.
    session.execute(delete(WordStatus).where(WordStatus.source == "placement"))

    now = _now()
    rows = []
    for word, band_index in pool:
        if word in evidence:
            continue
        status = passed_status if band_index in known else "new"
        counts[status] += 1
        rows.append(
            {
                "lemma": word,
                "status": status,
                "band_index": band_index,
                "source": "placement",
                "encounters": 0,
                "updated_at": now,
            }
        )

    if rows:
        session.bulk_insert_mappings(WordStatus, rows)
    session.commit()
    return counts


def record_lookup(session: Session, lemma: str) -> WordStatus:
    """The learner tapped this word for a translation: they did not know it.

    A lookup overrides an optimistic seed. It never promotes: asking what a word
    means is evidence against knowing it.
    """
    row = _upsert(session, lemma, status="learning", source="lookup")
    row.encounters = (row.encounters or 0) + 1
    session.commit()
    return row


def save_word(session: Session, *, lemma: str, translation: str, context: str | None, text_id: int) -> UserWord:
    """Add a word to the learner's list and mark it learning in the model.

    The single write path for both tables, so `user_words.status` and
    `word_status.status` cannot disagree.
    """
    now = _now()
    word = session.scalar(select(UserWord).where(UserWord.lemma == lemma))
    if word is None:
        word = UserWord(
            lemma=lemma,
            translation=translation.strip(),
            status="learning",
            added_at=now,
            encounters=1,
            last_seen_at=now,
            context_sentence=context,
            first_seen_text_id=text_id,
        )
        session.add(word)
    else:
        word.encounters += 1
        word.last_seen_at = now
        word.status = "learning"
        if translation.strip():
            word.translation = translation.strip()

    row = _upsert(session, lemma, status="learning", source="save")
    row.encounters = (row.encounters or 0) + 1

    session.commit()
    return word


def promote(session: Session, lemma: str) -> str:
    """One more encounter with a word that was not looked up: maybe promote it.

    Deliberately table-driven and slow: a word has to be met several times
    without needing help before the model stops calling it new.
    """
    row = session.get(WordStatus, lemma)
    if row is None:
        return "new"

    row.encounters = (row.encounters or 0) + 1
    needed = PROMOTION_ENCOUNTERS.get(row.status, 99)
    if row.encounters >= needed and row.status in STATUS_RANK:
        next_rank = min(STATUS_RANK[row.status] + 1, len(STATUSES) - 1)
        row.status = STATUSES[next_rank]
        row.source = "encounter"
        row.encounters = 0
    row.updated_at = _now()
    session.commit()
    return row.status


def status_map(session: Session, lemmas: list[str]) -> dict[str, str]:
    """Status per lemma, defaulting to "new" when the model has no row for it."""
    if not lemmas:
        return {}
    rows = session.execute(
        select(WordStatus.lemma, WordStatus.status).where(WordStatus.lemma.in_(set(lemmas)))
    ).all()
    found = {lemma: status for lemma, status in rows}
    return {lemma: found.get(lemma, "new") for lemma in lemmas}


def text_coverage(session: Session, text_id: int) -> Coverage:
    """Known-word coverage for one text, counting each distinct lemma once.

    Distinct lemmas, not tokens: "der" appearing nine times is one word to know.
    """
    lemmas = list(
        dict.fromkeys(
            session.scalars(
                select(Token.lemma).where(Token.text_id == text_id, Token.is_word.is_(True)).order_by(Token.position)
            ).all()
        )
    )
    statuses = status_map(session, lemmas)

    known = sum(1 for status in statuses.values() if status in COVERED_STATUSES)
    learning = sum(1 for status in statuses.values() if status == "learning")
    new = len(lemmas) - known - learning

    # A baseline exists once anything at all is known: a placement seed, a saved
    # word, or a lookup. Without one, "0% known" would be a lie dressed as data.
    baseline = (session.scalar(select(func.count()).select_from(WordStatus)) or 0) > 0

    return Coverage(
        total_words=len(lemmas),
        known_words=known,
        new_words=new,
        learning_words=learning,
        has_baseline=baseline,
    )


def band_counts(session: Session) -> dict[str, int]:
    """How many words sit in each status — the honest overview W-7 needs."""
    rows = session.execute(select(WordStatus.status, func.count()).group_by(WordStatus.status)).all()
    counts = {status: 0 for status in STATUSES}
    counts.update({status: count for status, count in rows})
    return counts

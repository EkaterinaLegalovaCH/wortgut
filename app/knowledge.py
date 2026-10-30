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
- Seeding reads `word_freq`, not the placement pool. The pool deliberately
  excludes function words, so seeding from it left `ich`, `nicht`, `und`, `der`
  without a status — and every status-less lemma was being counted as "new",
  which understated coverage badly (35 of 39 "new words" in one text). Any word
  with a frequency rank now gets a status from its band.
- A lemma with no rank anywhere is **not rated**, never "new". "New" means "in a
  band you have not shown you know"; "not rated" means "the app has no opinion".
  Folding the second into the first is what made the first percentage lie.
- Keys are lowercased: text lemmas arrive capitalised for nouns ("Hund") while
  the frequency list is lowercase ("hund"), and one word must not get two rows.
- Coverage is computed from statuses, and says when it has no basis: the
  baseline is a *seeded placement run*, not "any row exists", so a fresh install
  with one lookup still says "no level yet".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from .models import PlacementResponse, PlacementRun, TextRecord, Token, UserWord, WordFreq, WordStatus
from .placement import Item, band_for_rank, score

# new → learning → familiar → known. "unrated" is not a step on that ladder: it
# means the app has no opinion, and it is reported separately rather than folded
# into "new".
STATUSES: tuple[str, ...] = ("new", "learning", "familiar", "known")
UNRATED = "unrated"
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
    """How much of one text the learner already knows.

    Four buckets, because three of them mean different things:
    known/familiar = counted as understood, learning = being worked on,
    new = ranked, but in a band not yet shown, unrated = the app has no rank for
    it at all and therefore no opinion.
    """

    total_words: int
    known_words: int
    new_words: int
    learning_words: int
    unrated_words: int
    has_baseline: bool

    @property
    def rated_words(self) -> int:
        """Words the app has an opinion about — the honest denominator."""
        return self.total_words - self.unrated_words

    @property
    def percent(self) -> int | None:
        """Whole-percent coverage over rated words, or None with no baseline."""
        if not self.has_baseline or self.rated_words == 0:
            return None
        return round(100 * self.known_words / self.rated_words)

    @property
    def summary(self) -> str:
        """The one-line form the reader shows: "% known · N new words"."""
        if not self.has_baseline:
            return f"{self.total_words} words · no level yet"
        line = f"{self.percent}% known · {self.new_words} new words"
        if self.unrated_words:
            line += f" · {self.unrated_words} not rated"
        return line


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _upsert(session: Session, lemma: str, *, status: str, source: str, band_index: int | None = None) -> WordStatus:
    lemma = key(lemma)
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

    Every word with a frequency rank is seeded (about 50k): the band decides the
    status. Words with no rank at all get no row and stay "unrated" downstream.

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

    # Seed from the frequency list, not the placement pool: the pool exists to
    # pick test items and deliberately excludes function words, so seeding from
    # it left "der", "und", "ich" with no status at all.
    ranked = session.execute(select(WordFreq.word, WordFreq.rank)).all()

    # Replace the previous seed wholesale (one delete + one bulk insert) rather
    # than updating row by row.
    session.execute(delete(WordStatus).where(WordStatus.source == "placement"))

    now = _now()
    rows = []
    for word, rank in ranked:
        lemma = key(word)
        if lemma in evidence:
            continue
        band_index = band_for_rank(rank)
        status = passed_status if band_index in known else "new"
        counts[status] += 1
        rows.append(
            {
                "lemma": lemma,
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
    row = session.get(WordStatus, key(lemma))
    if row is None:
        return UNRATED

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


def key(lemma: str) -> str:
    """The `word_status` key for a lemma: lowercased.

    Text lemmas are capitalised for nouns ("Hund"); the frequency list is
    lowercase ("hund"). One word, one row.
    """
    return lemma.strip().lower()


def status_map(session: Session, lemmas: list[str]) -> dict[str, str]:
    """Status per lemma, "unrated" when the model has no row for it."""
    if not lemmas:
        return {}
    keys = {key(lemma) for lemma in lemmas}
    rows = session.execute(
        select(WordStatus.lemma, WordStatus.status).where(WordStatus.lemma.in_(keys))
    ).all()
    found = {lemma: status for lemma, status in rows}
    return {lemma: found.get(key(lemma), UNRATED) for lemma in lemmas}


def text_coverage(session: Session, text_id: int) -> Coverage:
    """Known-word coverage for one text, counting each distinct lemma once.

    Distinct lemmas, not tokens: "der" appearing nine times is one word to know.
    A lemma the app has no rank for is "unrated", never "new" — the difference
    between "not learned yet" and "no opinion" is the whole honesty of the number.
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
    unrated = sum(1 for status in statuses.values() if status == UNRATED)
    new = len(lemmas) - known - learning - unrated

    # The baseline is a *seeded placement run*, not "any row exists": otherwise
    # one lookup on a fresh install would turn "no level yet" into "0% known".
    baseline = (
        session.scalar(
            select(func.count()).select_from(WordStatus).where(WordStatus.source == "placement")
        )
        or 0
    ) > 0

    return Coverage(
        total_words=len(lemmas),
        known_words=known,
        new_words=new,
        learning_words=learning,
        unrated_words=unrated,
        has_baseline=baseline,
    )


def band_counts(session: Session) -> dict[str, int]:
    """How many words sit in each status — the honest overview W-7 needs."""
    rows = session.execute(select(WordStatus.status, func.count()).group_by(WordStatus.status)).all()
    counts = {status: 0 for status in STATUSES}
    counts.update({status: count for status, count in rows})
    return counts

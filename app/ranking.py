"""Which text to read next — ranked, and honest about what the numbers mean.

The ranking is deliberately conservative: a text's score is the share of its
*distinct words* the learner has already marked as familiar or known. Words the
app has no rank for are counted as unknown here, never as known, so the score
is a lower bound on how readable a text is. The "why" line says so.

Why a separate query instead of `text_coverage()` per text: that function walks
every lemma of one text, and doing it for the whole library took 6.4 s. Ranking
over the derived `text_lemmas` table takes ~0.5 s for 44 texts. A test asserts
the two agree, so speed does not buy a different answer.
"""

from dataclasses import dataclass

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from .knowledge import COVERED_STATUSES, Coverage, key
from .models import TextLemma, TextRecord, Token, WordStatus


def index_text_lemmas(session: Session, text_id: int) -> int:
    """Store the text's distinct lowercase lemmas (idempotent).

    Called once when a text is tokenized; the words of a text never change
    afterwards, so this is written once and only read by the ranking query.
    """
    lemmas = {
        key(lemma)
        for lemma in session.scalars(
            select(Token.lemma).where(Token.text_id == text_id, Token.is_word.is_(True))
        )
    }
    existing = {
        row.lemma for row in session.scalars(select(TextLemma).where(TextLemma.text_id == text_id))
    }
    missing = lemmas - existing
    if missing:
        session.add_all(TextLemma(text_id=text_id, lemma=lemma) for lemma in missing)
        session.flush()
    return len(lemmas)


def ensure_indexed(session: Session) -> int:
    """Index any text that has no lemma rows yet (one-time per text).

    Makes the table self-healing after an upgrade: the first home-page load
    pays the indexing cost once, later loads read the index. The check reads
    the small derived table, not `tokens` — a DISTINCT over 2.6M rows cost
    0.4 s on every request.
    """
    indexed = set(session.scalars(select(TextLemma.text_id).distinct()))
    missing = [
        text_id for text_id in session.scalars(select(TextRecord.id)) if text_id not in indexed
    ]
    for text_id in missing:
        index_text_lemmas(session, text_id)
    if missing:
        session.commit()
    return len(missing)


def has_baseline(session: Session) -> bool:
    """True once a placement run has seeded statuses — the honest precondition."""
    return (
        session.scalar(
            select(func.count()).select_from(WordStatus).where(WordStatus.source == "placement")
        )
        or 0
    ) > 0


def coverage_by_text(session: Session) -> dict[int, Coverage]:
    """Coverage for every text in one grouped query.

    Same arithmetic and the same buckets as `text_coverage()`; only the
    traversal differs (distinct lemmas instead of raw tokens).
    """
    baseline = has_baseline(session)
    ensure_indexed(session)
    known_case = case((WordStatus.status.in_(COVERED_STATUSES), 1), else_=0)
    learning_case = case((WordStatus.status == "learning", 1), else_=0)
    unrated_case = case((WordStatus.lemma.is_(None), 1), else_=0)

    rows = session.execute(
        select(
            TextRecord.id,
            func.count(TextLemma.id).label("total"),
            func.coalesce(func.sum(known_case), 0).label("known"),
            func.coalesce(func.sum(learning_case), 0).label("learning"),
            func.coalesce(func.sum(unrated_case), 0).label("unrated"),
        )
        .select_from(TextRecord)
        .outerjoin(TextLemma, TextLemma.text_id == TextRecord.id)
        .outerjoin(WordStatus, WordStatus.lemma == TextLemma.lemma)
        .group_by(TextRecord.id)
    ).all()

    result: dict[int, Coverage] = {}
    for text_id, total, known, learning, unrated in rows:
        total, known, learning, unrated = int(total), int(known), int(learning), int(unrated)
        result[text_id] = Coverage(
            total_words=total,
            known_words=known,
            new_words=max(0, total - known - learning - unrated),
            learning_words=learning,
            unrated_words=unrated,
            has_baseline=baseline,
        )
    return result


@dataclass(frozen=True)
class Suggestion:
    """One ranked text plus the sentence explaining its position."""

    text: TextRecord
    coverage: Coverage
    known_share: int
    why: str

    @property
    def unrated_share(self) -> int:
        if not self.coverage.total_words:
            return 0
        return round(100 * self.coverage.unrated_words / self.coverage.total_words)


def _why(coverage: Coverage, known_share: int) -> str:
    """One line, built only from numbers we actually counted."""
    if not coverage.total_words:
        return "empty text — nothing to read yet"
    parts = [f"{known_share}% of its {coverage.total_words:,} distinct words are already yours"]
    if coverage.new_words:
        parts.append(f"{coverage.new_words:,} new")
    if coverage.learning_words:
        parts.append(f"{coverage.learning_words:,} in progress")
    if coverage.unrated_words:
        share = round(100 * coverage.unrated_words / coverage.total_words)
        parts.append(f"{coverage.unrated_words:,} not rated ({share}%)")
    return " · ".join(parts)


# A "read next" suggestion should be something you can actually read for a
# while. Below this many distinct words a text is a sample, not a read; those
# still appear under "Recent texts", they are just not ranked as next reads.
MIN_SUGGEST_WORDS = 500


def suggest_next_texts(
    session: Session, limit: int = 5, coverage: dict[int, Coverage] | None = None
) -> list[Suggestion]:
    """Rank texts by how much of them is already familiar.

    Ties break towards the shorter text: same coverage, less to plough through.
    Returns nothing without a placement baseline — a percentage over no evidence
    would be a guess, and the template says so instead. Pass `coverage` when the
    caller already computed it: the grouped query is the expensive part.
    """
    if not has_baseline(session):
        return []

    coverage = coverage if coverage is not None else coverage_by_text(session)
    texts = session.scalars(select(TextRecord)).all()

    scored: list[Suggestion] = []
    for text in texts:
        cover = coverage.get(text.id)
        if cover is None or not cover.total_words:
            continue
        # Distinct words already familiar or known / all distinct words.
        # Unrated words count as unknown here on purpose: a lower bound.
        known_share = round(100 * cover.known_words / cover.total_words)
        scored.append(
            Suggestion(
                text=text,
                coverage=cover,
                known_share=known_share,
                why=_why(cover, known_share),
            )
        )

    scored.sort(key=lambda item: (-item.known_share, item.coverage.total_words, item.text.id))
    long_enough = [item for item in scored if item.coverage.total_words >= MIN_SUGGEST_WORDS]
    # A young library may hold nothing long enough yet; suggest what exists.
    return (long_enough or scored)[:limit]


def comfort_note(suggestions: list[Suggestion]) -> str:
    """Say plainly when the whole library is still hard for the learner."""
    if not suggestions:
        return ""
    best = suggestions[0].known_share
    if best >= 80:
        return "The top pick should read comfortably."
    if best >= 50:
        return "Even the easiest text still has plenty of new words."
    return (
        f"Nothing in the library is comfortable yet — the easiest text is only "
        f"{best}% familiar. Short texts and repeated reading will move this fastest."
    )

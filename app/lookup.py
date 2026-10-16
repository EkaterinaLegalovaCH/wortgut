"""Lookup and save services — all dictionary/frequency/user-word logic lives here.

Routes stay thin: they parse input, call one function, and render a result.
"""

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from .layout import DisplayToken, sentence_around
from .models import DictEntry, TextRecord, Token, UserWord, WordFreq

DICT_SOURCE = "freedict-deu-eng"


@dataclass(frozen=True)
class LookupResult:
    surface: str
    lemma: str
    pos: str
    translations: list[str]
    frequency_rank: int | None
    frequency_count: int | None
    already_saved: bool
    saved_translation: str | None


def _lemma_variants(lemma: str) -> list[str]:
    """Case variants to try against the dictionary.

    spaCy keeps the surface case (nouns capitalised), and FreeDict headwords are
    capitalised for nouns and lowercased otherwise — so a small, explicit list
    of variants beats guessing which one matches first.
    """
    variants = [lemma]
    lowered = lemma.lower()
    if lowered not in variants:
        variants.append(lowered)
    capitalised = lemma[:1].upper() + lemma[1:]
    if capitalised not in variants:
        variants.append(capitalised)
    return variants


def find_translations(session: Session, lemma: str) -> list[str]:
    """All translations for a lemma, ordered by dictionary row id (source order)."""
    rows = session.scalars(
        select(DictEntry.translation)
        .where(DictEntry.lemma.in_(_lemma_variants(lemma)))
        .order_by(DictEntry.id)
        .limit(8)
    ).all()
    return list(rows)


def find_frequency(session: Session, surface: str, lemma: str) -> tuple[int, int] | None:
    """(rank, count) for the word form if listed, else for its lemma."""
    for candidate in (surface.lower(), lemma.lower()):
        row = session.get(WordFreq, candidate)
        if row is not None:
            return row.rank, row.count
    return None


def lookup(session: Session, text_id: int, position: int) -> LookupResult | None:
    """Everything the popup needs for one token, or None if the token doesn't exist."""
    token = session.scalar(
        select(Token).where(Token.text_id == text_id, Token.position == position)
    )
    if token is None:
        return None

    translations = find_translations(session, token.lemma)
    frequency = find_frequency(session, token.surface, token.lemma)
    existing = session.scalar(select(UserWord).where(UserWord.lemma == token.lemma))

    return LookupResult(
        surface=token.surface,
        lemma=token.lemma,
        pos=token.pos,
        translations=translations,
        frequency_rank=frequency[0] if frequency else None,
        frequency_count=frequency[1] if frequency else None,
        already_saved=existing is not None,
        saved_translation=existing.translation if existing else None,
    )


def _display_tokens(session: Session, text_id: int) -> list[DisplayToken]:
    """The text as a list of (surface, whitespace) pairs, in reading order."""
    rows = session.execute(
        select(Token.surface, Token.whitespace)
        .where(Token.text_id == text_id)
        .order_by(Token.position)
    ).all()
    return [DisplayToken(surface=surface, whitespace=whitespace) for surface, whitespace in rows]


def save_user_word(
    session: Session,
    *,
    lemma: str,
    translation: str,
    text_id: int,
    position: int,
) -> UserWord:
    """Save (or re-encounter) a word, always with the sentence it appeared in.

    Saving the same lemma twice is not an error: it counts as another
    encounter, which is exactly the signal the recycling step will need later.
    """
    context = sentence_around(_display_tokens(session, text_id), position)
    now = datetime.now(timezone.utc)

    existing = session.scalar(select(UserWord).where(UserWord.lemma == lemma))
    if existing is not None:
        existing.encounters += 1
        existing.last_seen_at = now
        if translation.strip():
            existing.translation = translation.strip()
        session.commit()
        return existing

    word = UserWord(
        lemma=lemma,
        translation=translation.strip(),
        status="new",
        added_at=now,
        encounters=1,
        last_seen_at=now,
        context_sentence=context,
        first_seen_text_id=text_id,
    )
    session.add(word)
    session.commit()
    return word


def delete_user_word(session: Session, word_id: int) -> bool:
    """Delete by id. Returns False when the id does not exist."""
    result = session.execute(delete(UserWord).where(UserWord.id == word_id))
    session.commit()
    return result.rowcount > 0


def saved_lemma_counts(session: Session) -> dict[str, int]:
    """lemma → number of saved entries (always 1 today, but the view stays honest)."""
    rows = session.execute(select(UserWord.lemma, func.count(UserWord.id)).group_by(UserWord.lemma)).all()
    return {lemma: count for lemma, count in rows}


def list_texts(session: Session, limit: int = 20) -> list[tuple[TextRecord, int]]:
    """Recent texts with their word counts, newest first."""
    word_counts = (
        select(Token.text_id, func.count(Token.id).label("n"))
        .where(Token.is_word.is_(True))
        .group_by(Token.text_id)
        .subquery()
    )
    rows = session.execute(
        select(TextRecord, func.coalesce(word_counts.c.n, 0))
        .outerjoin(word_counts, word_counts.c.text_id == TextRecord.id)
        .order_by(TextRecord.created_at.desc(), TextRecord.id.desc())
        .limit(limit)
    ).all()
    return [(text, int(count)) for text, count in rows]

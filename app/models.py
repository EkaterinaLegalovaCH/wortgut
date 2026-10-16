"""Database models — the whole schema in one readable file.

Design notes:
- `tokens` is a full materialisation of the text after spaCy processing, so the
  reader view never re-runs the pipeline: it just reads rows.
- One dictionary row per (lemma, part-of-speech, translation-string) keeps the
  importer dumb and the lookup a plain indexed select.
- `user_words` has a unique lemma: this is a single-user app, one entry per
  lemma, re-saving the same word bumps `encounters` instead of duplicating.
"""

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class TextRecord(Base):
    __tablename__ = "texts"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[Optional[str]] = mapped_column(String(300))
    source_note: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    tokens: Mapped[list["Token"]] = relationship(
        back_populates="text_record",
        cascade="all, delete-orphan",
        order_by="Token.position",
    )


class Token(Base):
    __tablename__ = "tokens"

    id: Mapped[int] = mapped_column(primary_key=True)
    text_id: Mapped[int] = mapped_column(ForeignKey("texts.id"), index=True)
    position: Mapped[int] = mapped_column(Integer)  # index within the text, 0-based
    surface: Mapped[str] = mapped_column(String(200))  # exactly as written
    lemma: Mapped[str] = mapped_column(String(200))  # what the dictionary is keyed on
    pos: Mapped[str] = mapped_column(String(20))  # spaCy coarse tag: NOUN, VERB, ...
    is_word: Mapped[bool] = mapped_column(Boolean, default=True)
    # Whitespace that followed this token in the pasted text, reduced to one of
    # "", " ", "\n", "\n\n". Storing it lets the reader reproduce the text
    # exactly — no punctuation heuristics, paragraph breaks survive.
    whitespace: Mapped[str] = mapped_column(String(8), default=" ")

    text_record: Mapped[TextRecord] = relationship(back_populates="tokens")

    __table_args__ = (UniqueConstraint("text_id", "position", name="uq_tokens_text_position"),)


class DictEntry(Base):
    __tablename__ = "dict_entries"

    id: Mapped[int] = mapped_column(primary_key=True)
    lemma: Mapped[str] = mapped_column(String(200), index=True)
    pos: Mapped[Optional[str]] = mapped_column(String(20))
    translation: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(50))  # e.g. "freedict-deu-eng"


class WordFreq(Base):
    __tablename__ = "word_freq"

    word: Mapped[str] = mapped_column(String(200), primary_key=True)
    rank: Mapped[int] = mapped_column(Integer)  # 1 = most frequent; source order
    count: Mapped[int] = mapped_column(Integer)


class UserWord(Base):
    __tablename__ = "user_words"

    id: Mapped[int] = mapped_column(primary_key=True)
    lemma: Mapped[str] = mapped_column(String(200), unique=True)
    translation: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="new")  # new → learning → familiar → known
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    encounters: Mapped[int] = mapped_column(Integer, default=1)
    last_seen_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    context_sentence: Mapped[Optional[str]] = mapped_column(Text)
    first_seen_text_id: Mapped[Optional[int]] = mapped_column(ForeignKey("texts.id"))


# The reader joins user_words against token lemmas on every page load.
Index("ix_tokens_text_lemma", Token.text_id, Token.lemma)

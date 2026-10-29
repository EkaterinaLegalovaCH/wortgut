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


class PlacementItem(Base):
    """Curated pool of words the placement check may show.

    Derived data, rebuilt by `scripts/build_placement_pool.py` from `word_freq`
    (rank and band), `dict_entries` (is this real vocabulary with a translation?)
    and spaCy (is it a noun/verb/adjective rather than a name, interjection or
    function word?). Kept as a table instead of computed per request so the
    selection is fast and the pool can be inspected and reviewed.
    """

    __tablename__ = "placement_items"

    word: Mapped[str] = mapped_column(String(200), primary_key=True)
    band_index: Mapped[int] = mapped_column(Integer, index=True)
    rank: Mapped[int] = mapped_column(Integer)


class PlacementRun(Base):
    """One completed placement check: the summary W-7 will read.

    Kept out of `user_words` on purpose — the check is read-only with respect to
    the learner's vocabulary; only this table and `placement_responses` grow.
    """

    __tablename__ = "placement_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    seed: Mapped[int] = mapped_column(Integer)  # reproduces the exact item set
    label: Mapped[str] = mapped_column(String(20))  # e.g. "A2" or "A1–B1"
    level_low: Mapped[int] = mapped_column(Integer)  # index into LEVEL_NAMES
    level_high: Mapped[int] = mapped_column(Integer)
    known_bands: Mapped[int] = mapped_column(Integer)  # 0-6 bands passed
    nonword_hits: Mapped[int] = mapped_column(Integer)
    nonword_total: Mapped[int] = mapped_column(Integer)
    overclaims: Mapped[bool] = mapped_column(Boolean, default=False)
    thin_sample: Mapped[bool] = mapped_column(Boolean, default=False)

    responses: Mapped[list["PlacementResponse"]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
        order_by="PlacementResponse.position",
    )


class PlacementResponse(Base):
    """One word shown in one run, with what the learner said about it."""

    __tablename__ = "placement_responses"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("placement_runs.id"), index=True)
    position: Mapped[int] = mapped_column(Integer)  # display order in the run
    word: Mapped[str] = mapped_column(String(200))
    band_index: Mapped[Optional[int]] = mapped_column(Integer)  # None for nonwords
    is_nonword: Mapped[bool] = mapped_column(Boolean, default=False)
    # None means the item was left unanswered; that is data, not an error.
    claimed_known: Mapped[Optional[bool]] = mapped_column(Boolean)

    run: Mapped[PlacementRun] = relationship(back_populates="responses")

    __table_args__ = (
        UniqueConstraint("run_id", "position", name="uq_placement_responses_run_position"),
    )


# The reader joins user_words against token lemmas on every page load.
Index("ix_tokens_text_lemma", Token.text_id, Token.lemma)

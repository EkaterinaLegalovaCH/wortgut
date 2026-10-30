"""Placement check — a quick vocabulary estimate, not a certified test.

What it does: shows 40 real German words over six frequency bands, asks "do you
know this?" for each, and turns the answers into a per-band profile plus a
*reading-level estimate*.

Where the words come from: not `word_freq` directly. Subtitle-derived frequency
lists are full of names (`frodo`), interjections (`ahh`) and function words
(`der`, `muss`), none of which say anything about vocabulary.
`scripts/build_placement_pool.py` curates the pool once into `placement_items`:
the word must be attested in the dictionary (so it is real vocabulary with a
translation) and must not be tagged as a function word. This module only samples
the curated table.

Design notes:
- The stored artifact is the profile (attempted / known per band, seed, date).
  The level label is a deterministic table lookup over that profile, so it can
  always be recomputed and never hides its input.
- Scoring is a pure function (`score`). Item selection is the only part that
  touches the database, which keeps the mapping testable with fake profiles.
- The check writes nothing to `user_words`, `texts` or `tokens`: results live
  in their own two tables. W-7 owns known-word statuses and decides how a
  profile seeds them.
- Every item is a real word. An earlier version mixed in ten invented words to
  catch guessing (claiming words that cannot exist). Ekaterina rejected them on
  2026-10-29: for a self-check, pretending to test honesty is noise, not signal.
  The columns that held those counts stay in the schema and are written as zero,
  so an existing database needs no migration; the overclaim rule is gone.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import PlacementItem, PlacementResponse, PlacementRun

# Six frequency bands over word_freq ranks, and how many words each one
# contributes. 7+7+7+7+6+6 = 40: the denser bands carry one extra item each,
# because they have far more words to sample from.
BANDS: tuple[tuple[int, int], ...] = (
    (1, 1_000),
    (1_001, 3_000),
    (3_001, 8_000),
    (8_001, 15_000),
    (15_001, 30_000),
    (30_001, 50_000),
)
ITEMS_PER_BAND: tuple[int, ...] = (7, 7, 7, 7, 6, 6)
TOTAL_ITEMS = sum(ITEMS_PER_BAND)

# A band counts as "known" when at least this share of its answered items is
# claimed known. A band with fewer than MIN_BAND_RESPONSES answers is thin and
# reported as "not enough data" rather than guessed at.
BAND_KNOWN_RATIO = 0.6
MIN_BAND_RESPONSES = 3

LEVEL_NAMES: tuple[str, ...] = ("A1", "A2", "B1", "B2")

# How many bands the learner knows -> the level that fact points at. One row
# per possible count (0-6), which is why this is a table and not arithmetic:
# the steps are a judgement call and should be readable and editable.
KNOWN_BANDS_TO_LEVEL: dict[int, int] = {
    0: 0,
    1: 0,
    2: 1,
    3: 1,
    4: 2,
    5: 2,
    6: 3,
}

DISCLAIMER = (
    f"This is an estimate of how much German vocabulary you recognise, based on "
    f"{TOTAL_ITEMS} words across six frequency bands. It is not a certified CEFR "
    f"test and no level label from it should be quoted as an exam result."
)

# Part-of-speech tags that count as real vocabulary for this purpose. Names,
# interjections ("ahh", "hmm"), numbers and function words are excluded: they
# are not learnable vocabulary and a learner answers them without thinking.
ALLOWED_POS: frozenset[str] = frozenset({"NOUN", "VERB", "ADJ"})


# Editorial exclusions. Frequency lists built from subtitles carry English
# borrowings, slurs and loaded political vocabulary; a language-learning check
# is not the place for them. Deliberately a short, readable list rather than a
# hidden heuristic — extend it when a use-test turns up something similar.
POOL_BLOCKLIST: frozenset[str] = frozenset(
    {
        # English and brand words the subtitle corpus mixes in
        "talk",
        "dyke",
        "monsieur",
        "chevy",
        "ecstasy",
        "ciao",
        # loaded political vocabulary
        "führer",
        "nazi",
        "hitler",
        "jude",
        "russen",
        # profanity and slurs — a vocabulary test is not the place for them
        "arsch",
        "arschloch",
        "arschgesicht",
        "scheiße",
        "scheisse",
        "scheiß",
        "fick",
        "ficken",
        "gefickt",
        "hure",
        "hurensohn",
        "schwuchtel",
        "fotze",
        "wichser",
        "titten",
        "pimmel",
        "pussy",
        "penner",
        "mistkerl",
        "vollidiot",
    }
)


def is_blocked(word: str) -> bool:
    """Editorially excluded from the pool (see POOL_BLOCKLIST)."""
    return word.lower() in POOL_BLOCKLIST


class PoolNotBuilt(RuntimeError):
    """The curated item pool is missing or empty — the build script has not run."""


@dataclass(frozen=True)
class Item:
    """One word shown in the check."""

    word: str
    band_index: int


@dataclass(frozen=True)
class BandScore:
    index: int
    low: int
    high: int
    attempted: int
    known: int
    enough_data: bool
    known_band: bool

    @property
    def label(self) -> str:
        return f"ranks {self.low:,}–{self.high:,}"


@dataclass(frozen=True)
class Profile:
    """The scored result of one run."""

    bands: list[BandScore]
    known_bands: int
    level_low: int
    level_high: int
    label: str
    thin_sample: bool
    flags: list[str] = field(default_factory=list)

    @property
    def disclaimer(self) -> str:
        return DISCLAIMER

    def as_dict(self) -> dict:
        return {
            "bands": [
                {
                    "index": band.index,
                    "low": band.low,
                    "high": band.high,
                    "attempted": band.attempted,
                    "known": band.known,
                    "enough_data": band.enough_data,
                    "known_band": band.known_band,
                }
                for band in self.bands
            ],
            "known_bands": self.known_bands,
            "level_low": self.level_low,
            "level_high": self.level_high,
            "label": self.label,
            "thin_sample": self.thin_sample,
            "flags": list(self.flags),
            "disclaimer": self.disclaimer,
        }


def band_for_rank(rank: int) -> int | None:
    """Which frequency band a rank falls into, or None when it is out of range."""
    for index, (low, high) in enumerate(BANDS):
        if low <= rank <= high:
            return index
    return None


def select_items(session: Session, seed: int) -> list[Item]:
    """The 40 items for one run: 7/7/7/7/6/6 real words across the six bands.

    Deterministic for a given seed and pool: candidates are sorted by rank
    before sampling, so the same seed produces the same set on the same data.

    Raises PoolNotBuilt when the curated pool is empty, instead of silently
    falling back to the raw frequency list — the noise there is the whole
    reason the pool exists.
    """
    pool = _pool_by_band(session)
    if not any(pool.values()):
        raise PoolNotBuilt(
            "The placement item pool is empty. Run: uv run python scripts/build_placement_pool.py"
        )

    rng = random.Random(seed)
    items: list[Item] = []

    for index in range(len(BANDS)):
        candidates = pool.get(index, [])
        take = min(ITEMS_PER_BAND[index], len(candidates))
        for word in rng.sample(candidates, take):
            items.append(Item(word=word, band_index=index))

    # A second, seed-derived stream shuffles the presentation order, so the
    # bands are not in ascending order on screen.
    random.Random(seed * 7_919 + 13).shuffle(items)
    return items


def _pool_by_band(session: Session) -> dict[int, list[str]]:
    """Curated words per band, sorted by frequency rank (1 = most frequent)."""
    rows = session.execute(
        select(PlacementItem.band_index, PlacementItem.word).order_by(
            PlacementItem.rank, PlacementItem.word
        )
    ).all()

    pool: dict[int, list[str]] = {}
    for band_index, word in rows:
        pool.setdefault(band_index, []).append(word)
    return pool


def dict_lookup_params(word: str) -> tuple[str, str]:
    """The two spellings to try against `dict_entries` for a frequency-list word.

    The list is lowercase and FreeDict capitalises nouns, so both spellings are
    checked. Shared by the pool builder and its test so they cannot disagree
    about what "attested" means.
    """
    return word, word[:1].upper() + word[1:]


# FreeDict's own part-of-speech tags, mapped onto the three classes that make
# sense as vocabulary items.
DICT_POS_TO_CLASS: dict[str, str] = {"n": "noun", "v": "verb", "adj": "adj"}

# Tags that mark a headword as grammar rather than vocabulary. A word carrying
# any of these is excluded even when it also has a noun tag (FreeDict lists
# "ein" as art *and* n).
FUNCTION_POS: frozenset[str] = frozenset({"art", "pron", "prep", "conj", "int", "ptcl", "num"})


def choose_word_class(*, dict_positions: set[str], spacy_pos: str, word: str) -> str | None:
    """Decide what kind of word this is, or None to reject it.

    The dictionary is the authority. Two rules do the work:

    - a word that is *ever* tagged as a function word is rejected, even if it
      also has a noun tag. FreeDict lists `ein` as both `art` and `n`, and a
      bare "ein" in a vocabulary test measures nothing.
    - a word with no noun/verb/adjective tag is rejected. Advertising adverb-only
      entries ("noch", "mal") is the same problem one step further in.

    spaCy tags a word in isolation, which is exactly where names and function
    words slip through (`frodo` comes back as a noun), so it is used only as a
    sanity veto below — never as a ticket into the pool.
    """
    if not dict_positions:
        return None
    if dict_positions & FUNCTION_POS:
        return None
    if spacy_pos in {"PROPN", "INTJ", "NUM", "X", "SYM", "PUNCT", "SPACE"}:
        return None

    classes = {DICT_POS_TO_CLASS[pos] for pos in dict_positions if pos in DICT_POS_TO_CLASS}
    if not classes:
        return None

    # Verbs must be in citation form: asking whether a learner knows
    # "vorbeigekommen" measures parsing, not vocabulary.
    if classes == {"verb"} and not word.endswith(("en", "ern", "eln")):
        return None

    for preferred in ("noun", "verb", "adj"):
        if preferred in classes:
            return preferred
    return None


def is_pool_candidate(*, word: str, pos: str, has_translation: bool, min_length: int = 3) -> bool:
    """Should this frequency-list word be allowed into the placement pool?

    Pure on purpose: the build script and the tests agree on one definition of
    "real vocabulary worth showing a learner".
    """
    if len(word) < min_length or not word.isalpha():
        return False
    if is_blocked(word):
        return False
    if not has_translation:
        return False
    return pos in ALLOWED_POS


def score(items: list[Item], answers: list[bool | None]) -> Profile:
    """Turn answers into a per-band profile and a level estimate.

    `answers[i]` is True (claimed known), False (claimed unknown) or None (not
    answered). Pure: same inputs, same profile, no database.
    """
    if len(items) != len(answers):
        raise ValueError("items and answers must be the same length")

    bands: list[BandScore] = []

    for index, (low, high) in enumerate(BANDS):
        attempted = 0
        known = 0
        for item, answer in zip(items, answers):
            if item.band_index != index or answer is None:
                continue
            attempted += 1
            known += 1 if answer else 0

        enough_data = attempted >= MIN_BAND_RESPONSES
        known_band = enough_data and (known / attempted) >= BAND_KNOWN_RATIO
        bands.append(
            BandScore(
                index=index,
                low=low,
                high=high,
                attempted=attempted,
                known=known,
                enough_data=enough_data,
                known_band=known_band,
            )
        )

    known_bands = sum(1 for band in bands if band.known_band)
    thin_sample = any(not band.enough_data for band in bands)

    center = KNOWN_BANDS_TO_LEVEL[known_bands]
    level_low, level_high = _widen(center) if thin_sample else (center, center)

    return Profile(
        bands=bands,
        known_bands=known_bands,
        level_low=level_low,
        level_high=level_high,
        label=_render_label(level_low, level_high),
        thin_sample=thin_sample,
        flags=["thin_sample"] if thin_sample else [],
    )


def _widen(center: int) -> tuple[int, int]:
    """Widen an estimate by one level on each side, clipped to the scale.

    Uncertainty is shown by making the range wider, never by claiming a
    narrower level at the edges.
    """
    return max(0, center - 1), min(len(LEVEL_NAMES) - 1, center + 1)


def _render_label(low: int, high: int) -> str:
    if low == high:
        return LEVEL_NAMES[low]
    return f"{LEVEL_NAMES[low]}–{LEVEL_NAMES[high]}"


def save_run(session: Session, *, seed: int, items: list[Item], answers: list[bool | None], profile: Profile) -> PlacementRun:
    """Persist the run and every response. Only touches the placement tables."""
    run = PlacementRun(
        seed=seed,
        label=profile.label,
        level_low=profile.level_low,
        level_high=profile.level_high,
        known_bands=profile.known_bands,
        # Retired by the 2026-10-29 decision to drop invented words. Written as
        # zero so the existing columns (and old databases) stay valid.
        nonword_hits=0,
        nonword_total=0,
        overclaims=False,
        thin_sample=profile.thin_sample,
    )
    session.add(run)
    session.flush()  # assign run.id before the responses reference it

    for position, (item, answer) in enumerate(zip(items, answers)):
        session.add(
            PlacementResponse(
                run_id=run.id,
                position=position,
                word=item.word,
                band_index=item.band_index,
                is_nonword=False,
                claimed_known=answer,
            )
        )

    session.commit()
    return run


def latest_run(session: Session) -> PlacementRun | None:
    """Most recent run — what W-7 will read to seed the known-word model."""
    return session.scalar(select(PlacementRun).order_by(PlacementRun.id.desc()).limit(1))


def responses_for(session: Session, run_id: int) -> list[PlacementResponse]:
    return list(
        session.scalars(
            select(PlacementResponse)
            .where(PlacementResponse.run_id == run_id)
            .order_by(PlacementResponse.position)
        ).all()
    )

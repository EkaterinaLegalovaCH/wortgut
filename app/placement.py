"""Placement check — a quick vocabulary estimate, not a certified test.

What it does: shows 40 words, 5 from each of six frequency bands plus 10
nonwords, asks "do you know this?" for each, and turns the answers into a
per-band profile plus a *reading-level estimate*.

Design notes:
- The stored artifact is the profile (attempted / known per band, nonword
  hits, seed, date). The level label is a deterministic table lookup over that
  profile, so it can always be recomputed and never hides its input.
- Scoring is a pure function (`score`). Item selection is the only part that
  touches the database, which keeps the mapping testable with fake profiles.
- The check writes nothing to `user_words`, `texts` or `tokens`: results live
  in their own two tables. W-7 owns known-word statuses and decides how a
  profile seeds them.
- Nonwords are fixed, hand-picked strings with German-looking morphology. A
  test asserts they are absent from both the dictionary and the frequency list,
  because a nonword that exists in the data would silently break calibration.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import PlacementResponse, PlacementRun, WordFreq

# Six frequency bands over word_freq ranks. Five items from each.
BANDS: tuple[tuple[int, int], ...] = (
    (1, 1_000),
    (1_001, 3_000),
    (3_001, 8_000),
    (8_001, 15_000),
    (15_001, 30_000),
    (30_001, 50_000),
)
ITEMS_PER_BAND = 5

# A band counts as "known" when at least this share of its answered items is
# claimed known. A band with fewer than MIN_BAND_RESPONSES answers is thin and
# reported as "not enough data" rather than guessed at.
BAND_KNOWN_RATIO = 0.6
MIN_BAND_RESPONSES = 3

# Claiming three or more of the ten nonwords means the answers overclaim.
OVERCLAIM_HITS = 3

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
    "This is an estimate of how much German vocabulary you recognise, based on "
    "40 words across six frequency bands. It is not a certified CEFR test and "
    "no level label from it should be quoted as an exam result."
)

# Fixed, checked against both data tables by tests/test_placement.py.
NONWORDS: tuple[str, ...] = (
    "Brelmicht",
    "Grumseln",
    "Taldrisch",
    "Hornkessel",
    "Wandelmut",
    "Zerplisch",
    "Morgentucht",
    "Silberkranz",
    "Klinkerhaft",
    "Bimslicht",
)


@dataclass(frozen=True)
class Item:
    """One word shown in the check."""

    word: str
    band_index: int | None  # None for nonwords
    is_nonword: bool


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
    nonword_hits: int
    nonword_total: int
    known_bands: int
    level_low: int
    level_high: int
    label: str
    overclaims: bool
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
            "nonword_hits": self.nonword_hits,
            "nonword_total": self.nonword_total,
            "known_bands": self.known_bands,
            "level_low": self.level_low,
            "level_high": self.level_high,
            "label": self.label,
            "overclaims": self.overclaims,
            "thin_sample": self.thin_sample,
            "flags": list(self.flags),
            "disclaimer": self.disclaimer,
        }


def select_items(session: Session, seed: int) -> list[Item]:
    """The 40 items for one run: 5 real words per band plus every nonword.

    Deterministic for a given seed and database content: candidates are sorted
    before sampling, so the same seed produces the same set on the same data.
    """
    rng = random.Random(seed)
    items: list[Item] = []

    for index, (low, high) in enumerate(BANDS):
        candidates = _band_candidates(session, low, high)
        take = min(ITEMS_PER_BAND, len(candidates))
        for word in rng.sample(candidates, take):
            items.append(Item(word=word, band_index=index, is_nonword=False))

    items.extend(Item(word=word, band_index=None, is_nonword=True) for word in NONWORDS)

    # A second, seed-derived stream shuffles the presentation order, so
    # nonwords are not clustered at the end and bands are not in obvious order.
    random.Random(seed * 7_919 + 13).shuffle(items)
    return items


def _band_candidates(session: Session, low: int, high: int) -> list[str]:
    """Real words whose frequency rank falls in the band, in rank order."""
    rows = session.execute(
        select(WordFreq.word)
        .where(WordFreq.rank >= low, WordFreq.rank <= high)
        .order_by(WordFreq.rank, WordFreq.word)
    ).all()

    seen: set[str] = set()
    words: list[str] = []
    for (word,) in rows:
        if len(word) < 3 or not word.isalpha():
            continue
        if word in seen:
            continue
        seen.add(word)
        words.append(word)
    return words


def score(items: list[Item], answers: list[bool | None]) -> Profile:
    """Turn answers into a per-band profile and a level estimate.

    `answers[i]` is True (claimed known), False (claimed unknown) or None (not
    answered). Pure: same inputs, same profile, no database.
    """
    if len(items) != len(answers):
        raise ValueError("items and answers must be the same length")

    bands: list[BandScore] = []
    nonword_hits = 0
    nonword_total = 0

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

    for item, answer in zip(items, answers):
        if item.is_nonword:
            nonword_total += 1
            nonword_hits += 1 if answer else 0

    known_bands = sum(1 for band in bands if band.known_band)
    overclaims = nonword_hits >= OVERCLAIM_HITS
    thin_sample = any(not band.enough_data for band in bands)

    center = KNOWN_BANDS_TO_LEVEL[known_bands]
    level_low, level_high = _widen(center) if (overclaims or thin_sample) else (center, center)

    flags: list[str] = []
    if overclaims:
        flags.append("overclaims")
    if thin_sample:
        flags.append("thin_sample")

    return Profile(
        bands=bands,
        nonword_hits=nonword_hits,
        nonword_total=nonword_total,
        known_bands=known_bands,
        level_low=level_low,
        level_high=level_high,
        label=_render_label(level_low, level_high),
        overclaims=overclaims,
        thin_sample=thin_sample,
        flags=flags,
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
        nonword_hits=profile.nonword_hits,
        nonword_total=profile.nonword_total,
        overclaims=profile.overclaims,
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
                is_nonword=item.is_nonword,
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

"""Lemma repairs: a small, explicit table of lemmas spaCy gets wrong.

Why a table and not a better lemmatizer: the model is pinned, it runs offline,
and it makes the same mistakes every time. A curated mapping is auditable — you
can read it, argue with one line, and see exactly what changes. Swapping the
model would change thousands of lemmas at once with no review.

Where the entries come from (all verified against the real dictionary and
frequency list, `scripts/fix_lemmas.py --dry-run` prints the evidence):

- `card`: the four the strategist named for W-13 — `frisst`, `aller`, `leiben`,
  `eur`. Three are not German words at all; `frisst`/`aller` are real forms whose
  lemma should be the base word.
- `spacy-suffix`: the German lemmatizer inserts an `e` before `-ln`/`-rn`
  (`lächelen`, `murmelen`, `zitteren`) and truncates some forms (`kannstn`,
  `hinunt`, `rüsteen`).
- `old-orthography`: these texts are pre-1901 scans (`thun`, `Thüre`, `Thal`,
  `giebt`). The modern spelling is the word the learner will meet again, so the
  modern lemma is the useful key.

Rules for adding a line:
1. The target must exist in `dict_entries` or `word_freq`. A repair that points
   at another non-word is worse than the bug.
2. Never map a proper noun (PROPN). `Effi` and `Halde` are names, not vocabulary;
   they stay unrated on purpose — the app has no opinion about a character.
3. One bad lemma maps to exactly one good lemma. If a form is genuinely
   ambiguous, leave it alone.
"""

from __future__ import annotations

# bad lemma -> (good lemma, why). Keys are compared case-insensitively; the
# corrected lemma keeps the table's casing so `find_translations` can try its
# usual case variants.
LEMMA_FIXES: dict[str, tuple[str, str]] = {
    # --- named by the W-13 card ---
    "frisst": ("fressen", "card"),
    "aller": ("all", "card"),
    "leiben": ("lieb", "card"),
    "eur": ("euer", "card"),
    # --- spaCy inserted 'e' before -ln / -rn ---
    "lächelen": ("lächeln", "spacy-suffix"),
    "murmelen": ("murmeln", "spacy-suffix"),
    "zitteren": ("zittern", "spacy-suffix"),
    "verdunkelen": ("verdunkeln", "spacy-suffix"),
    "wunderen": ("wundern", "spacy-suffix"),
    "sammelen": ("sammeln", "spacy-suffix"),
    # --- spaCy truncation ---
    "kannstn": ("können", "spacy-suffix"),
    "rüsteen": ("rüsten", "spacy-suffix"),
    "hinunt": ("hinunter", "spacy-suffix"),
    "unsr": ("unser", "spacy-suffix"),
    "edl": ("edel", "spacy-suffix"),
    "eign": ("eignen", "spacy-suffix"),
    "dunkl": ("dunkel", "spacy-suffix"),
    # --- pre-1901 orthography (th, ie, ü) ---
    "thun": ("tun", "old-orthography"),
    "thut": ("tun", "old-orthography"),
    "thue": ("tun", "old-orthography"),
    "thunn": ("tun", "old-orthography"),
    "Thue": ("tun", "old-orthography"),
    "gethan": ("tun", "old-orthography"),
    "giebt": ("geben", "old-orthography"),
    "gieben": ("geben", "old-orthography"),
    "Giebt": ("geben", "old-orthography"),
    "Thüre": ("tür", "old-orthography"),
    "Thür": ("tür", "old-orthography"),
    "Thal": ("tal", "old-orthography"),
    "Thale": ("tal", "old-orthography"),
    "Thales": ("tal", "old-orthography"),
    "Theil": ("teil", "old-orthography"),
    "theil": ("teil", "old-orthography"),
}

# Case-insensitive lookup, built once: spaCy's casing is inconsistent between
# runs and texts ("Thue" and "thue" in the same library), and a repair must not
# depend on which one the tokenizer happened to produce.
_BY_LOWER: dict[str, str] = {}
for _bad, (_good, _why) in LEMMA_FIXES.items():
    _BY_LOWER.setdefault(_bad.lower(), _good)


def fix_lemma(lemma: str) -> str:
    """The corrected lemma for `lemma`, or `lemma` itself when it is fine.

    Idempotent: a corrected lemma is never itself a key, so applying this twice
    is the same as applying it once.
    """
    if not lemma:
        return lemma
    stripped = lemma.strip()
    if not stripped:
        return lemma
    return _BY_LOWER.get(stripped.lower(), stripped)


def is_repaired(lemma: str) -> bool:
    """True when the table has an entry for this lemma (i.e. it was wrong)."""
    return bool(lemma) and lemma.strip().lower() in _BY_LOWER

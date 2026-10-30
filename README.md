# Wortgut

**Read German, keep what you meet.** Paste a German text, read it in a comfortable
reading view, tap any unknown word for an offline translation (lemma +
translation + frequency rank), and save it — with the sentence it appeared in —
to your own word list.

Everything runs locally: after a one-time data download the app makes **no
network calls at all**, so it works on a train, on a plane, or with the Wi-Fi
off. That is a hard requirement, not a nicety.

This is milestone **M0 — "walking skeleton"**: the smallest version that is
genuinely usable end to end.

---

## What works today

| Flow | Endpoint | Status |
|---|---|---|
| Paste a text (title/source optional) | `POST /texts` | ✅ runs spaCy, stores text + tokens, redirects to the reader |
| List recent texts | `GET /` | ✅ with word counts and dates |
| Read: every word tappable, saved words highlighted | `GET /texts/{id}` | ✅ paragraphs preserved, phone-width friendly |
| Tap a word → popup (surface, lemma, translations, rank) | `GET /api/lookup` | ✅ works fully offline |
| Save a word, with its sentence captured automatically | `POST /api/words` | ✅ re-saving counts an encounter |
| Word list: lemma, translation, context, date, delete | `GET /words` | ✅ |
| Liveness / readiness | `GET /health`, `GET /ready` | ✅ `/ready` names exactly what is missing |
| Manual translation when the dictionary misses | popup input | ✅ compounds like *Geißlein* have no entry — you type your own |
| Placement check: 40 words, per-band result | `GET /placement` | ✅ M1 — an estimate, not a certificate (see below) |
| Score a run, store the profile | `POST /placement` | ✅ read-only for your vocabulary |
| Result: label range, band table, flags | `GET /placement/{id}` | ✅ latest run at `/placement/latest` |
| Coverage per text: "% known · N new words" | reader + home | ✅ M1 — counted per distinct word |
| Known-word statuses: new → learning → familiar → known | `word_status` table | ✅ seeded by the placement check, corrected by real use |

Not in M0: accounts, deployment, placement test, coverage scoring, vocabulary
recycling, stats, TTS, EPUB import. See *Scope* below.

---

## Quick start

Requirements: [`uv`](https://docs.astral.sh/uv/getting-started/installation/) and
`curl` + `tar`. Nothing else — no Node, no database server, no build step.

```bash
git clone https://github.com/EkaterinaLegalovaCH/wortgut.git
cd wortgut

# 1. Python 3.13 environment. This also installs the German spaCy model
#    (de_core_news_sm, ~14 MB) because it is a locked dependency.
uv sync

# 2. Source data (~470 MB, one time, cached in data/ which is gitignored)
./scripts/fetch_data.sh

# 3. Import the data — run these one after the other, never at the same time:
#    SQLite accepts a single writer, so concurrent imports fail with
#    "database is locked" (they are also both re-runnable, so just retry).
uv run python scripts/import_freq.py     # ~0.5 s  → 50,000 words
uv run python scripts/import_dict.py     # ~26 s   → 517,000 dictionary entries

# 3b. Curate the placement-check word pool (~1 min, re-runnable)
uv run python scripts/build_placement_pool.py

# 4. Run it
./run.sh                                 # http://127.0.0.1:8000
```

Then paste a German text into the form and start reading. `./run.sh --reload`
is the development variant.

Verify the setup with:

```bash
curl -s localhost:8000/ready
# {"ready":true,"checks":{"database_reachable":true,"dictionary_loaded":true,"spacy_model_loaded":true},"missing":[]}
```

Run the tests with `uv run pytest`.

---

## How it works

```
browser ──POST /texts──►  spaCy (tokenize + lemmatize)  ──►  texts + tokens (SQLite)
                                                                  │
browser ──GET /texts/{id}──►  render every token as a <button>  ◄──┘
   │
   └──click──► GET /api/lookup?text_id=&position=  ──►  dict_entries + word_freq
                     │
                     └── save ──► POST /api/words ──► user_words (+ context sentence)
```

Two steps are deliberately separated:

- **The pipeline is offline and one-time.** `scripts/fetch_data.sh` downloads,
  `scripts/import_*.py` parse into SQLite. The web app only ever *reads* those
  tables. Re-running an import is safe and replaces rows in a single
  transaction, so a crashed import can never leave a half-loaded dictionary.
- **The web app never runs spaCy at request time** except when a new text is
  pasted. Reading a stored text is pure SQL — that is why tapping words feels
  instant and why it works offline.

### Modules

| File | Responsibility |
|---|---|
| `app/config.py` | All runtime configuration, from environment variables |
| `app/models.py` | The whole schema: `texts`, `tokens`, `dict_entries`, `word_freq`, `user_words` |
| `app/db.py` | Engine, session-per-request, SQLite pragmas (WAL, foreign keys) |
| `app/pipeline.py` | spaCy: text → tokens (surface, lemma, POS, is-word, whitespace) |
| `app/lookup.py` | Dictionary/frequency/user-word queries; saving a word with its sentence |
| `app/layout.py` | Rebuilding readable text from tokens; finding the surrounding sentence |
| `app/routes/pages.py` | HTML pages (home, reader, word list) |
| `app/routes/api.py` | JSON endpoints + `/health` + `/ready` |
| `app/placement.py` | Item selection, scoring, label mapping — the placement check's logic |
| `app/knowledge.py` | Statuses, seeding from a placement run, coverage per text |
| `app/routes/placement.py` | Placement pages: form, submit, result |
| `scripts/import_dict.py` | Streams the 450 MB TEI dictionary into `dict_entries` |
| `scripts/import_freq.py` | Frequency list into `word_freq` |
| `scripts/build_placement_pool.py` | Curates `placement_items`: real vocabulary only |
| `scripts/seed_knowledge.py` | Re-seeds `word_status` from a stored placement run |

### Data model

- `texts(id, title, source_note, created_at)`
- `tokens(id, text_id, position, surface, lemma, pos, is_word, whitespace)` — the
  text after processing; the reader renders these rows and nothing else
- `dict_entries(id, lemma, pos, translation, source)` — indexed on `lemma`
- `word_freq(word, rank, count)` — rank 1 = most frequent
- `user_words(id, lemma, translation, status, added_at, encounters,
  last_seen_at, context_sentence, first_seen_text_id)` — one row per lemma
- `placement_runs(id, created_at, seed, label, level_low, level_high,
  known_bands, nonword_hits, nonword_total, overclaims, thin_sample)` — one row
  per completed check. The `nonword_*` and `overclaims` columns are retired
  (written as zero) since invented words were dropped on 2026-10-29; they stay
  so existing databases need no migration.
- `placement_responses(id, run_id, position, word, band_index, is_nonword,
  claimed_known)` — one row per word shown; `NULL` means unanswered
- `placement_items(word, band_index, rank)` — the curated pool the placement
  check samples from; derived data, rebuilt by
  `scripts/build_placement_pool.py`
- `word_status(lemma, status, band_index, source, encounters, updated_at)` —
  the known-word model: one row per lemma the app has an opinion about,
  `source` says where the value came from (`placement`, `lookup`, `save`)

---

## Design decisions and tradeoffs

**SQLite + sync SQLAlchemy, sync endpoints.** There is no runtime network I/O —
no requests, no API calls, nothing to await. Async here would add machinery
without buying anything. SQLite is a single file, backs up by copying, and is
instant at this scale (50 MB database, sub-millisecond indexed lookups).

**Whitespace is stored per token, not guessed.** spaCy tokens carry no spacing,
and the obvious fix — a table of punctuation rules — is subtly wrong (German
`„Hallo“`, English `"Hello"`, brackets) and loses paragraph breaks. So the
tokenizer records the whitespace that actually followed each token, reduced to
one of `""`, `" "`, `"\n"`, `"\n\n"`. The reader replays it inside
`white-space: pre-wrap`, reproducing the pasted text exactly, paragraphs
included. This is one extra column instead of a pile of heuristics. *(Note:
this is a small addition to the schema sketched in the M0 handoff, made for this
reason.)*

**`create_all` at startup, not migrations.** Single user, local file, schema
still moving. Migrations are a prerequisite for deployment, not for M0 — and
inventing them now would be premature.

**Frozen dataclasses for settings and pipeline output.** `Settings` is frozen so
configuration cannot be mutated at runtime; `TokenDraft`/`DisplayToken` are
frozen so a token cannot be half-updated in place.

**Routes stay thin, services hold logic.** A route parses input, calls one
function in `lookup.py`, and renders. All the interesting decisions are in
plain functions that the tests call directly.

**Dictionary lookup tries case variants explicitly.** spaCy keeps the surface
case (`Häuser` → lemma `Haus`), and FreeDict capitalises nouns but not verbs, so
`find_translations` tries the lemma, its lowercase form, and its capitalised
form — a three-element list that is easy to read and to test, instead of an
implicit `COLLATE NOCASE` that would silently change matching for everything.

**Re-saving a word is an encounter, not a duplicate.** `user_words.lemma` is
unique; saving again bumps `encounters` and `last_seen_at`. That is exactly the
signal the later recycling step needs, so the data is already accumulating.

---

## Placement check (M1)

Open `/placement` from the home page or the navigation. You get 40 words, one
question each — *do you know this word?* — and an estimate of your reading level
at the end. It takes about two minutes and works offline like everything else.

**What the 40 items are.** Real German words only: seven from each of the four
commonest frequency bands and six from each of the last two
(`1–1,000`, `1,001–3,000`, `3,001–8,000`, `8,001–15,000`, `15,001–30,000`,
`30,001–50,000`). No invented words: an earlier version mixed in ten nonwords to
catch guessing, and the use-test rejected them — for a self-check, being asked
whether you are lying is noise rather than signal. Every item therefore comes
from `placement_items`, and a test asserts exactly that.

**The pool is curated, not raw frequency.** `word_freq` comes from film
subtitles, so it is full of names (`frodo`), interjections (`ahh`), function
words (`der`, `ein`) and stray tokens. `scripts/build_placement_pool.py` keeps a
word only when it is alphabetic and 3+ letters, attested in the dictionary as
real vocabulary, and *not* tagged by the dictionary as a function word — a word
that carries any `art`/`pron`/`prep`/`conj`/`int`/`ptcl`/`num` tag is rejected
even when it also has a noun tag (`ein` is both). Verbs must be in citation form
(`laufen`, not `abgestempelt`); nouns are shown capitalised. A short editorial
list (`POOL_BLOCKLIST`) removes slurs, loaded political vocabulary and English
borrowings that subtitle corpora mix in. The result is ~14,400 words across the
six bands, stored in `placement_items` so it can be inspected:

```bash
sqlite3 data/wortgut.sqlite3 "select band_index, count(*) from placement_items group by 1"
```

**How a run is scored.** A band counts as known when at least 60% of its answered
words are claimed *and* at least three of its words were answered. The number of
known bands (0–6) is looked up in a table to give a level (`A1`, `A2`, `B1`,
`B2`). Two things widen the result instead of sharpening it:

- **a thin sample** — any band with fewer than three answers is shown as "not
  enough data" and widens the range a level in each direction.

The label is always a range or a single level with the disclaimer attached, and
the result page always shows the per-band table underneath it. There is no code
path that prints a bare level.

**What is stored.** The profile, not just the label: `placement_runs` holds the
seed, the label and range, bands known, nonword hits and both flags;
`placement_responses` holds every word with the answer (`NULL` when skipped). The
seed reproduces the exact item set, so a result can be re-derived. Nothing is
written to `user_words`, `texts` or `tokens` — a test runs a full check and
asserts those counts are unchanged. Seeding the known-word model from a profile
is W-7's job, not this card's.

**Deliberate limits.** Yes/no self-report measures recognition, not production;
it can overstate active knowledge. Forty words give a coarse estimate: it
distinguishes broad ranges honestly and cannot resolve half-levels. Compound
knowers are slightly under-credited, because a compound that is missing from the
dictionary still counts as a real word here (the word comes from the frequency
list, so the reader would recognise it even if the dictionary would not gloss it).
No adaptive difficulty, no timing, no multiple choice — none of that earns its
complexity at this scale.

---

## Known words and coverage (M1)

Every lemma the app has an opinion about gets a status:

| Status | Meaning |
|---|---|
| `new` | not met yet, or met once and not recognised |
| `learning` | looked up, or saved — actively being learned |
| `familiar` | recognised: either seeded from a passed frequency band, or met repeatedly without help |
| `known` | seedable by policy; otherwise reached by repeated meetings |

**Where the first values come from.** Submitting a placement check seeds
`word_status` from the **frequency list** (about 50,000 ranked words), not from
the placement pool: words in bands the run passed become `familiar` (the default
policy) and everything else stays `new`. Seeding from the pool would have been
wrong — the pool deliberately excludes function words, so `der`, `und`, `ich`
would have had no status and been counted as "new". Two deliberate choices sit
behind the rest:

- **The bands, not the label.** Seeding reads `placement_responses` and
  recomputes the per-band profile. A single derived label like "A1" must never
  decide the status of thousands of words.
- **`familiar`, not `known`.** The check is a self-report over 40 items, so it
  earns a "probably recognised", not a firm "known". Real meetings promote a word
  (`familiar` → `known` after three encounters); tapping it for a translation
  demotes it immediately.

`SEED_POLICY` in `app/knowledge.py` switches this: `known` trusts the profile
outright, `none` learns only from reading. `scripts/seed_knowledge.py --policy
<name>` re-seeds an existing database.

**Real evidence outranks the questionnaire.** A word looked up or saved keeps its
status when the check is taken again, so re-calibrating the band estimate never
erases what reading taught the model.

**Coverage** is per text and per **distinct lemma**, not per token: `der`
appearing nine times is one word to know. Four buckets, because three of them
mean different things:

| Bucket | Meaning | In the percentage? |
|---|---|---|
| known / familiar | counted as understood | yes, as known |
| learning | looked up or saved, being worked on | yes, as not known |
| new | ranked, but in a band you have not shown you know | yes, as not known |
| not rated | the app has no frequency rank for it at all | **no** |

A word with no rank is *not rated*, never "new": "not learned yet" and "no
opinion" are different claims, and folding the second into the first is what
made the first percentage lie. The reader and the text list both show
`"% known · N new words"` (plus `· N not rated` when there are any), and the
percentage is computed over rated words only.

With no placement run seeded, coverage reports *"no level yet"* instead of a 0%
that would look like data — one lookup on a fresh install does not count as a
baseline.

Note on tables: `user_words` is the learner's own list (saved words, with a
status for display), `word_status` is the model behind coverage. Saving writes
both through one function so they cannot drift.

---

## Data sources and licences

Neither file is committed to this repository; `scripts/fetch_data.sh` downloads
them into `data/` and the `/data/` path is gitignored. Both are free and
attributed here as their licences require.

**Dictionary — FreeDict deu–eng 1.9-fd1** (517,534 headwords), from the
[Ding](https://dict.tu-chemnitz.de/) dictionary via
[freedict.org](https://freedict.org/).
Copyright © Frank Richter, Einhard Leichtfuß, Sebastian Humenda.
Licensed under **GPLv3 and AGPLv3** (different parts of the combined work). The
licence text ships inside the download as `data/freedict-COPYING`.
The SHA-512 of the pinned release is verified by `fetch_data.sh`.

**Frequency list — `de_50k.txt`** from
[hermitdave/FrequencyWords](https://github.com/hermitdave/FrequencyWords)
(2018 German list, subtitle-derived, therefore modern and conversational),
licensed **CC BY-SA 4.0**. Rank comes from file order; the count is the
published occurrence count.

**spaCy + `de_core_news_sm`** — MIT-licensed tooling and model, downloaded as a
Python package.

---

## Configuration

All via environment variables (see `.env.example`); `run.sh` loads `.env` if
present. No secrets are used anywhere in M0.

| Variable | Default | Meaning |
|---|---|---|
| `DATABASE_PATH` | `data/wortgut.sqlite3` | SQLite file |
| `DATA_DIR` | `data` | Where the downloaded source data lives |
| `TARGET_LANG` | `en` | Translation target language |
| `HOST` / `PORT` | `127.0.0.1` / `8000` | Bind address — localhost only in M0 |

---

## Tests

`uv run pytest` — 127 tests, no network, no fixtures needed beyond the repo.

- **Import parsers** against a tiny hand-written TEI fixture: lemmas, POS,
  multiple translations, umlauts, entries with no translation, idempotent
  re-import, `--limit`.
- **Frequency parser**: rank from line order, malformed lines skipped, duplicate
  words de-duplicated, idempotent re-import.
- **Pipeline** (real German model): `lief` → `laufen`, `Häuser` → `Haus`, POS
  tagging, punctuation not tappable, whitespace and paragraph captures.
- **Layout**: whitespace replay, sentence extraction, paragraph boundaries,
  out-of-range positions.
- **Lookup, save flow, pages**: lookups by lemma, frequency fallback
  (`Kinder` → `kind` → rank), dictionary miss, manual translation, encounter
  counting, saved-word marking, delete, `/health`, `/ready`, static assets —
  all through the real HTTP stack against a temp database.
- **Placement check**: band-count-to-label table (every possible count), the
  60% band rule at its boundary, thin bands (fewer than three answers →
  "not enough data" + widened range), unanswered items not counted as "no",
  seeded item selection reproducing the same set, every item drawn from the
  curated pool and from its own band, the dictionary-first word-class rule,
  the seed-normalisation regression, and a guard that
  `user_words`/`texts`/`tokens` counts are identical before and after a run.
- **Known-word model**: seeding policies (`familiar`/`known`/`none`), seeding
  from ranks so function words get a status, lookup/save evidence surviving a
  re-seed, promotion only after repeated meetings, case-insensitive lemma keys,
  coverage counting distinct lemmas, a rank-less lemma reporting as "not rated"
  rather than "new", the rated-words denominator, and a baseline that requires a
  seeded run rather than any row.

---

## Known limitations (M0)

Documented rather than fixed, per the milestone plan:

- **Compounds and separable verbs often miss.** `Geißlein` has no entry; the
  manual-translation path exists for exactly this. Single-token selection only —
  no multi-word phrases yet.
- **A lemma can have several unrelated dictionary entries**, and the popup shows
  them in source order as separate lines. FreeDict/ Ding mixes senses (the
  animal *Wolf*, the medical *Wolf* = chafing), so the correct gloss is always
  visible but not always first. Ranking senses by likely relevance is future work;
  nothing is silently dropped.
- **Translations include phrase-like glosses** ("not to see the wood for the
  trees") because the source stores them alongside the plain gloss. The popup
  shows the entry as the dictionary wrote it rather than pretending to filter it.
- **Sentence snippets cut on punctuation**, so an abbreviation can end a context
  sentence early.
- **No auth** and localhost-only binding — expected until the deploy milestone.
- **No migrations yet** (`create_all` on startup) — fine for a local file, must
  change before deploy.
- **Rare words have no frequency rank** (`Geiß`): the frequency list is
  subtitle-derived, so a fairytale word may simply not be in the top 50k.
- **The placement check is a coarse self-report.** Forty yes/no items over six
  bands give a wide estimate, not a CEFR measurement. It cannot detect
  overconfidence, because there is nothing in it that the learner cannot
  legitimately know.

---

## Scope

M0 is the walking skeleton; M1 adds the level-aware parts. The placement check
(above) is the first M1 card. The full project plan (known-word model, coverage
scoring, level-matched library, vocabulary recycling, stats, deploy) lives in the
project folder as `wortgut-brief.md` and `wortgut-kanban.md`, milestone by
milestone. Next: seeding the known-word model from a stored profile and showing
coverage per text.

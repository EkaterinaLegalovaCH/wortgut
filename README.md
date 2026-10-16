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

Not in M0: accounts, deployment, placement test, coverage scoring, vocabulary
recycling, stats, TTS, EPUB import. See *Scope* below.

---

## Quick start

Requirements: [`uv`](https://docs.astral.sh/uv/getting-started/installation/) and
`curl` + `tar`. Nothing else — no Node, no database server, no build step.

```bash
git clone https://github.com/EkaterinaLegalovaCH/wortgut.git
cd wortgut

# 1. Python 3.13 environment (uv reads .python-version and pyproject.toml)
uv sync

# 2. German spaCy model (~14 MB, one time)
uv run python -m spacy download de_core_news_sm

# 3. Source data (~470 MB, one time, cached in data/ which is gitignored)
./scripts/fetch_data.sh

# 4. Import the data — run these one after the other, never at the same time:
#    SQLite accepts a single writer, so concurrent imports fail with
#    "database is locked" (they are also both re-runnable, so just retry).
uv run python scripts/import_freq.py     # ~0.5 s  → 50,000 words
uv run python scripts/import_dict.py     # ~26 s   → 517,000 dictionary entries

# 5. Run it
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
| `scripts/import_dict.py` | Streams the 450 MB TEI dictionary into `dict_entries` |
| `scripts/import_freq.py` | Frequency list into `word_freq` |

### Data model

- `texts(id, title, source_note, created_at)`
- `tokens(id, text_id, position, surface, lemma, pos, is_word, whitespace)` — the
  text after processing; the reader renders these rows and nothing else
- `dict_entries(id, lemma, pos, translation, source)` — indexed on `lemma`
- `word_freq(word, rank, count)` — rank 1 = most frequent
- `user_words(id, lemma, translation, status, added_at, encounters,
  last_seen_at, context_sentence, first_seen_text_id)` — one row per lemma

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

`uv run pytest` — 51 tests, no network, no fixtures needed beyond the repo.

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

---

## Scope

M0 is the walking skeleton. The full project plan (placement check, known-word
model, coverage scoring, level-matched library, vocabulary recycling, stats,
deploy) lives in the project folder as `wortgut-brief.md` and
`wortgut-kanban.md`, milestone by milestone. Next up is M1: a placement check
that seeds the known-word model, coverage scoring per text, and a seeded library
of public-domain German texts.

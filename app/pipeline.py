"""spaCy pipeline: raw pasted text in, token rows out.

The model is loaded lazily and cached — importing this module must stay cheap
so that scripts and tests can import app modules without paying for the model.

Each token keeps the whitespace that followed it in the original text, so the
reader can rebuild the text exactly as pasted instead of guessing where spaces
belong.
"""

import re
from dataclasses import dataclass
from collections.abc import Iterator

import spacy
from spacy.language import Language
from spacy.tokens import Doc, Token

MODEL_NAME = "de_core_news_sm"

# spaCy refuses documents over 1,000,000 characters and its parser needs about
# 1GB of temporary memory per 100k characters. Whole novels blow past both
# limits (Der Nachsommer is ~1.55M characters), so long text is tokenized in
# chunks that are cut only at line breaks — never inside a word — and the
# per-token whitespace carries the paragraph breaks across the seams.
MAX_CHUNK_CHARS = 400_000
_SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s+")

_nlp: Language | None = None


class ModelNotInstalled(RuntimeError):
    """Raised when the German spaCy model is missing from the environment."""


def load_nlp() -> Language:
    """Return the shared German pipeline, loading it on first use."""
    global _nlp
    if _nlp is None:
        try:
            _nlp = spacy.load(MODEL_NAME)
        except OSError as exc:  # spaCy raises OSError for a missing model
            raise ModelNotInstalled(
                f"spaCy model '{MODEL_NAME}' is not installed. "
                f"Run: uv sync   (the model is a locked dependency; do not install it by hand)"
            ) from exc
    return _nlp


def is_model_installed() -> bool:
    """Cheap check for the /ready endpoint — no pipeline run involved."""
    return spacy.util.is_package(MODEL_NAME)


def normalize_whitespace(raw: str) -> str:
    """Reduce a whitespace run to one of: '', ' ', '\\n', '\\n\\n'.

    Keeping only these four shapes means the reader shows gentle paragraph
    breaks without echoing however many spaces a pasted page happened to use.
    """
    if not raw:
        return ""
    if "\n" in raw:
        return "\n\n" if raw.count("\n") >= 2 else "\n"
    return " "


@dataclass(frozen=True)
class TokenDraft:
    """One token on its way into the database (no id yet)."""

    position: int
    surface: str
    lemma: str
    pos: str
    is_word: bool
    whitespace: str


def _split_long_line(line: str, limit: int) -> Iterator[str]:
    """Break one over-long line into slices under `limit`, text unchanged.

    Cuts at the last space inside the window so words stay whole; only a
    single word longer than `limit` is cut mid-word. Slices concatenate back
    to the original line exactly — no spaces added or dropped.
    """
    start = 0
    while len(line) - start > limit:
        window_end = start + limit
        cut = line.rfind(" ", start, window_end)
        if cut <= start:
            # No usable space: hard-cut at the window edge.
            yield line[start:window_end]
            start = window_end
        else:
            # Keep the space at the end of this slice so nothing is lost.
            yield line[start : cut + 1]
            start = cut + 1
    yield line[start:]


def _chunks(text: str, limit: int = MAX_CHUNK_CHARS) -> Iterator[str]:
    """Yield text pieces under `limit`, cutting only at line breaks.

    Each piece keeps the newlines that followed it, so spaCy's per-token
    whitespace still describes the paragraph breaks at the seams. A single
    over-long line is split at sentence ends, never mid-word.
    """
    buffer = ""
    for line in text.splitlines(keepends=True):
        if len(line) > limit:
            if buffer:
                yield buffer
                buffer = ""
            yield from _split_long_line(line, limit)
            continue
        if len(buffer) + len(line) > limit:
            yield buffer
            buffer = ""
        buffer += line
    if buffer:
        yield buffer


def tokenize(text: str) -> list[TokenDraft]:
    """Split text into tokens, lemmatize, and mark which tokens are real words.

    is_word = alphabetic token. Punctuation, digits and symbols stay in the
    list so the reader can show the text exactly as written, but only words
    become clickable lookup targets.

    spaCy hands paragraph breaks back as whitespace-only tokens of their own
    (e.g. "\\n\\n"); those are folded into the gap after the previous token so
    the stored token list contains only things a reader can see as text.

    Long text is processed chunk by chunk (see `_chunks`), so a whole novel
    stays under spaCy's length limit and its memory ceiling. Token positions
    keep counting across seams, so the stored text reads exactly as written.
    """
    nlp = load_nlp()
    drafts: list[TokenDraft] = []
    position = 0
    for chunk in _chunks(text):
        drafts.extend(_tokenize_chunk(nlp(chunk), position))
        position = drafts[-1].position + 1 if drafts else 0
    return drafts


def _tokenize_chunk(doc: Doc, start_position: int) -> list[TokenDraft]:
    """Turn one spaCy document into drafts, numbering from `start_position`."""
    # (token, whitespace_after) with whitespace-only tokens merged backwards
    merged: list[tuple[Token, str]] = []
    for token in doc:
        if token.is_space:
            if merged:
                previous_token, previous_whitespace = merged[-1]
                merged[-1] = (previous_token, previous_whitespace + token.text)
            continue
        merged.append((token, token.whitespace_))

    return [
        TokenDraft(
            position=start_position + offset,
            surface=token.text,
            lemma=token.lemma_ or token.text,
            pos=token.pos_,
            is_word=token.is_alpha,
            whitespace=normalize_whitespace(whitespace),
        )
        for offset, (token, whitespace) in enumerate(merged)
    ]

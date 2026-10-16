"""spaCy pipeline: raw pasted text in, token rows out.

The model is loaded lazily and cached — importing this module must stay cheap
so that scripts and tests can import app modules without paying for the model.

Each token keeps the whitespace that followed it in the original text, so the
reader can rebuild the text exactly as pasted instead of guessing where spaces
belong.
"""

from dataclasses import dataclass

import spacy
from spacy.language import Language

MODEL_NAME = "de_core_news_sm"

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
                f"Run: uv run python -m spacy download {MODEL_NAME}"
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


def tokenize(text: str) -> list[TokenDraft]:
    """Split text into tokens, lemmatize, and mark which tokens are real words.

    is_word = alphabetic token. Punctuation, digits and symbols stay in the
    list so the reader can show the text exactly as written, but only words
    become clickable lookup targets.

    spaCy hands paragraph breaks back as whitespace-only tokens of their own
    (e.g. "\\n\\n"); those are folded into the gap after the previous token so
    the stored token list contains only things a reader can see as text.
    """
    doc = load_nlp()(text)

    # (token, whitespace_after) with whitespace-only tokens merged backwards
    merged: list[tuple[object, str]] = []
    for token in doc:
        if token.is_space:
            if merged:
                previous_token, previous_whitespace = merged[-1]
                merged[-1] = (previous_token, previous_whitespace + token.text)
            continue
        merged.append((token, token.whitespace_))

    return [
        TokenDraft(
            position=i,
            surface=token.text,
            lemma=token.lemma_ or token.text,
            pos=token.pos_,
            is_word=token.is_alpha,
            whitespace=normalize_whitespace(whitespace),
        )
        for i, (token, whitespace) in enumerate(merged)
    ]

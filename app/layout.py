"""Turn token rows back into readable text.

Whitespace is not guessed: the tokenizer records what actually followed each
token in the pasted text (spaces, newlines, nothing), and the reader replays
that. So the reading view reproduces the original text exactly — including
paragraph breaks — and no punctuation heuristics are needed.
"""

from collections.abc import Sequence
from dataclasses import dataclass

SENTENCE_END = set(".!?…»")


@dataclass(frozen=True)
class DisplayToken:
    """The minimum a view needs to lay a token out again."""

    surface: str
    whitespace: str


def join(tokens: Sequence[DisplayToken]) -> str:
    """Reassemble tokens into plain text, keeping their original whitespace."""
    return "".join(token.surface + token.whitespace for token in tokens).strip()


def collapse_whitespace(text: str) -> str:
    """Single-line form of a text (context snippets have no room for breaks)."""
    return " ".join(text.split())


def sentence_around(tokens: Sequence[DisplayToken], position: int) -> str:
    """Return the sentence containing `position` as a one-line string.

    Boundaries are the previous/next sentence-final punctuation or a paragraph
    break (a newline in a token's whitespace). Abbreviations can still cut a
    sentence early; acceptable for a context snippet.
    """
    if not 0 <= position < len(tokens):
        raise IndexError(f"position {position} outside text of {len(tokens)} tokens")

    start = position
    while start > 0 and not _ends_sentence(tokens[start - 1]):
        start -= 1

    end = position
    while end < len(tokens) - 1 and not _ends_sentence(tokens[end]):
        end += 1

    return collapse_whitespace(join(tokens[start : end + 1]))


def _ends_sentence(token: DisplayToken) -> bool:
    return token.surface[-1:] in SENTENCE_END or "\n" in token.whitespace

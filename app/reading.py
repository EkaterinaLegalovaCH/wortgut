"""Page geometry for the reader, shared by the HTML page and the read event.

Both routes must agree on what "page 2" means: if the read-event endpoint
counted a different slice than the one rendered, encounters would be recorded
for words that were never on screen. One constant, one clamp, one window.
"""

from math import ceil

TOKENS_PER_PAGE = 2000


def page_count(total_tokens: int) -> int:
    """How many reader pages a text has — always at least one."""
    return max(1, ceil(total_tokens / TOKENS_PER_PAGE))


def clamp_page(page: int, total_tokens: int) -> int:
    """Keep a requested page inside the text instead of erroring on it."""
    return min(max(page, 1), page_count(total_tokens))


def page_window(page: int, total_tokens: int) -> tuple[int, int]:
    """(offset, limit) of one page, for `OFFSET`/`LIMIT` on token positions."""
    page = clamp_page(page, total_tokens)
    return (page - 1) * TOKENS_PER_PAGE, TOKENS_PER_PAGE

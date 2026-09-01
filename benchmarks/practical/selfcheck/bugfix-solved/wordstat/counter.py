"""Token counting for wordstat.

Contract (see task.json): a *word* is a maximal run of ASCII
alphanumerics ``[A-Za-z0-9]``. Every other character -- punctuation,
whitespace, apostrophes, symbols -- acts as a separator.
"""

import re
import string

_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")


def count_words(text: str) -> int:
    """Count words in ``text`` according to the contract above."""
    return len(_TOKEN_RE.findall(text))


def token_edges(token: str) -> str:
    """Strip leading/trailing punctuation from a whitespace token.

    Kept for backward compatibility; word_freq now tokenizes via the
    regex above instead.
    """
    return token.strip(string.punctuation)

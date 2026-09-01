"""Token counting for wordstat.

Contract (see task.json): a *word* is a maximal run of ASCII
alphanumerics ``[A-Za-z0-9]``. Every other character -- punctuation,
whitespace, apostrophes, symbols -- acts as a separator.
"""

import string


def count_words(text: str) -> int:
    """Count words in ``text`` according to the contract above."""
    return len(text.split())


def token_edges(token: str) -> str:
    """Strip leading/trailing punctuation from a whitespace token."""
    return token.strip(string.punctuation)

"""Word frequency statistics built on top of counter tokens."""

from .counter import _TOKEN_RE


def word_freq(text: str) -> dict[str, int]:
    """Frequency map of words in ``text`` (keys lowercased)."""
    freq: dict[str, int] = {}
    for tok in _TOKEN_RE.findall(text.lower()):
        freq[tok] = freq.get(tok, 0) + 1
    return freq

"""Frequency statistics over tokens (shared logic lives in metrics.util)."""

from .util import normalize_token, tokenize

__all__ = ["count_frequency", "normalize_token", "tokenize"]


def count_frequency(text: str) -> dict[str, int]:
    """Frequency map over normalized tokens of ``text``."""
    freq: dict[str, int] = {}
    for tok in tokenize(text):
        freq[tok] = freq.get(tok, 0) + 1
    return freq

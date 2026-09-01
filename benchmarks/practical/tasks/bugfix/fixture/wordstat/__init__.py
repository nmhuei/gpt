"""wordstat: tiny text statistics utilities."""

from .counter import count_words
from .stats import word_freq

__all__ = ["count_words", "word_freq"]

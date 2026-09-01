"""Existing behavior tests for metrics.text. These must KEEP PASSING
unchanged through the refactor.
"""

from metrics.text import normalize_token, tokenize


def test_normalize_strips_and_folds():
    assert normalize_token("  Hi!? ") == "hi"


def test_normalize_keeps_only_alphanumeric():
    assert normalize_token("a-b") == "ab"
    assert normalize_token("") == ""


def test_tokenize_basic():
    assert tokenize("Hello, world! Hello") == ["hello", "world", "hello"]


def test_tokenize_noise_and_empty():
    assert tokenize("... !!!") == []
    assert tokenize("") == []


def test_tokenize_whitespace_variants():
    assert tokenize("one\ttwo\nthree") == ["one", "two", "three"]

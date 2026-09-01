"""Existing behavior tests for metrics.stats. These must KEEP PASSING
unchanged through the refactor.
"""

from metrics import stats as stats_mod
from metrics.text import tokenize as text_tokenize


def test_count_frequency_basic():
    assert stats_mod.count_frequency("Hi! hi HEY") == {"hi": 2, "hey": 1}


def test_count_frequency_punctuation_glues_tokens_together():
    # "Hi!" and "hi" normalize to the same key -- regression guard for
    # any future normalization change.
    assert stats_mod.count_frequency("Hi! hi") == {"hi": 2}


def test_count_frequency_edge_cases():
    assert stats_mod.count_frequency("") == {}
    assert stats_mod.count_frequency("a a a") == {"a": 3}


def test_modules_agree_on_tokenize():
    sample = "Rain!! rain go, away"
    assert stats_mod.tokenize(sample) == text_tokenize(sample)

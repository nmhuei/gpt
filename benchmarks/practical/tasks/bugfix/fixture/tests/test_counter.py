"""Visible tests for wordstat. These FAIL against the shipped buggy code;
they must pass after the bug is fixed. Do not modify this file to make it
pass -- fix the implementation instead.
"""

from wordstat import count_words, word_freq


def test_simple():
    assert count_words("hello world") == 2


def test_punctuation_only_tokens_are_not_words():
    assert count_words("... wait -- what?!") == 2


def test_hyphenated_compound_is_four_words():
    assert count_words("state-of-the-art") == 4


def test_empty_and_noise():
    assert count_words("") == 0
    assert count_words("!!! --- ???") == 0


def test_word_freq_strips_punctuation_and_folds_case():
    assert word_freq("Hey! hey HEY.") == {"hey": 3}

"""Hidden conformance suite for the BUGFIX task (run by grade.py only).

Encodes the mandatory assertions of the wordstat contract:
words = maximal runs of [A-Za-z0-9]; everything else separates;
word_freq keys are lowercased clean tokens.
"""

import pytest
from wordstat import count_words, word_freq


@pytest.mark.parametrize(
    "text,expected",
    [
        ("", 0),
        ("hello", 1),
        ("Hello, World!", 2),
        ("... wait -- what?!", 2),
        ("state-of-the-art", 4),
        ("it's fine", 3),
        ("abc123 456", 2),
        ("!!! --- ???", 0),
        ("one\ttwo\nthree", 3),
        ("don't stop believin'", 4),
        ("echo echo echo", 3),
        ("x9-y8_z7", 3),
    ],
)
def test_count_words_contract(text, expected):
    assert count_words(text) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("a b a", {"a": 2, "b": 1}),
        ("Hey! hey HEY.", {"hey": 3}),
        ("", {}),
        ("Mix3d c4s3!", {"mix3d": 1, "c4s3": 1}),
        ("don't stop", {"don": 1, "t": 1, "stop": 1}),
    ],
)
def test_word_freq_contract(text, expected):
    assert word_freq(text) == expected


def test_freq_and_count_agree():
    sample = "Go go GO! now"
    assert sum(word_freq(sample).values()) == count_words(sample)

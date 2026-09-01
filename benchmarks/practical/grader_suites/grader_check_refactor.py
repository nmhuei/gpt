"""Hidden conformance suite for the REFACTOR task (run by grade.py only).

Asserts three-way parity (metrics.text / metrics.stats / metrics.util),
exact behavior anchors, and that the public API survived the refactor.
On the pristine fixture this suite ERRORS because metrics.util does not
exist yet -- which is exactly the baseline-fail the grader expects.
"""

import pytest
from metrics import stats as stats_mod
from metrics import text as text_mod
from metrics import util as util_mod

PARITY_CASES = [
    "Hello, World!",
    "... !!!",
    "",
    "a-b-c",
    "Mixed CASE words here!",
    "tab\tsep",
    "x  y  z",
    "!@#$%",
    "naïve? ASCII-only-plz",
    "Rain!! rain go, away",
]


@pytest.mark.parametrize("case", PARITY_CASES)
def test_three_way_parity(case):
    assert text_mod.tokenize(case) == util_mod.tokenize(case)
    assert stats_mod.tokenize(case) == util_mod.tokenize(case)


def test_util_normalize_exact():
    assert util_mod.normalize_token("  Hi!? ") == "hi"
    assert util_mod.normalize_token("a-b") == "ab"


def test_util_tokenize_exact():
    assert util_mod.tokenize("One two THREE") == ["one", "two", "three"]
    assert util_mod.tokenize("Hi! hi") == ["hi", "hi"]


def test_count_frequency_exact():
    assert stats_mod.count_frequency("D d d!") == {"d": 3}
    assert stats_mod.count_frequency("") == {}


def test_public_api_preserved():
    assert text_mod.normalize_token("X!?") == "x"
    assert text_mod.tokenize("A! a") == ["a", "a"]
    assert stats_mod.normalize_token("Y-z") == "yz"
    assert stats_mod.tokenize("B b!") == ["b", "b"]

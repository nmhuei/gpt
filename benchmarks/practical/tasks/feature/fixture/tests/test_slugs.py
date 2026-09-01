"""Smoke tests for slugkit. They fail until the feature is implemented
per SPEC. Do not modify this file -- implement the functions instead.
"""

from slugkit import slugify, unique_slug


def test_basic_slug():
    assert slugify("Hello World") == "hello-world"


def test_collapses_separator_runs_and_strips_edges():
    assert slugify("  many    gaps  ") == "many-gaps"


def test_ascii_folding():
    assert slugify("Crème Brûlée") == "creme-brulee"


def test_unique_slug_avoids_collision():
    assert unique_slug("Post", {"post"}) == "post-2"

"""Hidden conformance suite for the FEATURE task (run by grade.py only).

Operationalizes the slugkit SPEC exactly as pinned in task.json:
slugify = NFKD-fold-to-ASCII -> lowercase -> non-[a-z0-9] runs to one
separator -> strip separator edges; unique_slug appends -2/-3/...
"""

import pytest
from slugkit import slugify, unique_slug


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Hello World", "hello-world"),
        ("  Multiple   spaces and---dashes  ", "multiple-spaces-and-dashes"),
        ("Café Résumé!", "cafe-resume"),
        ("---weird***punct---", "weird-punct"),
        ("123 456", "123-456"),
        ("!!!", ""),
        ("already-a-slug", "already-a-slug"),
        ("MiXeD CaSe", "mixed-case"),
        ("tabs\tand\nnewlines", "tabs-and-newlines"),
        ("a!@#b", "a-b"),
    ],
)
def test_slugify_spec(text, expected):
    assert slugify(text) == expected


def test_slugify_custom_separator():
    assert slugify("Hello World", sep="_") == "hello_world"


@pytest.mark.parametrize(
    "text,existing,expected",
    [
        ("Post", None, "post"),
        ("Post", set(), "post"),
        ("Post", {"post"}, "post-2"),
        ("Post", {"post", "post-2"}, "post-3"),
        ("A B", {"a-b"}, "a-b-2"),
        ("Post", {"post"}, "post-2"),
    ],
)
def test_unique_slug_spec(text, existing, expected):
    assert unique_slug(text, existing) == expected

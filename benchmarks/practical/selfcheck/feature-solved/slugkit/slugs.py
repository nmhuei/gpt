"""Slug generation implemented per SPEC (see task.json)."""

import re
import unicodedata

_NON_ALNUM_RUN = re.compile(r"[^a-z0-9]+")


def _fold(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text)
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii")
    return ascii_only.lower()


def slugify(text: str, sep: str = "-") -> str:
    folded = _fold(text)
    dashed = _NON_ALNUM_RUN.sub(sep, folded)
    return dashed.strip(sep)


def unique_slug(text: str, existing=None, sep: str = "-") -> str:
    base = slugify(text, sep)
    taken = set(existing) if existing else set()
    if base not in taken:
        return base
    n = 2
    while f"{base}{sep}{n}" in taken:
        n += 1
    return f"{base}{sep}{n}"

"""Slug generation. Implement the two functions below EXACTLY per SPEC
(reproduced in task.json). Do not rename files, packages or functions.
"""

import unicodedata


def _fold(text: str) -> str:
    """Placeholder helper kept from the skeleton."""
    return unicodedata.normalize("NFKD", text)


def slugify(text: str, sep: str = "-") -> str:
    """Return the slug for ``text``.

    SPEC:
      1. Unicode NFKD-normalize, then drop everything non-ASCII
         ("caf\\u00e9" -> "cafe").
      2. Lowercase.
      3. Replace every maximal run of characters outside [a-z0-9]
         with exactly one ``sep``.
      4. Strip leading/trailing ``sep`` characters.
      5. If nothing alphanumeric remains, return "".
    """
    raise NotImplementedError("slugify is not implemented yet")


def unique_slug(text: str, existing=None, sep: str = "-") -> str:
    """Like :func:`slugify` but guaranteed unique against ``existing``.

    SPEC:
      - ``existing`` may be None or any iterable of taken slugs; None and
        empty mean "nothing taken".
      - If the base slug is not taken, return it unchanged.
      - Otherwise return ``base + sep + "2"``, ``base + sep + "3"``, ...
        using the first free suffix.
    """
    raise NotImplementedError("unique_slug is not implemented yet")

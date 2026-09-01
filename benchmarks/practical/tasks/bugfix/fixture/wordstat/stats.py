"""Word frequency statistics built on top of counter tokens."""


from .counter import token_edges


def word_freq(text: str) -> dict[str, int]:
    """Frequency map of words in ``text`` (keys lowercased)."""
    freq: dict[str, int] = {}
    for raw in text.split():
        key = token_edges(raw).lower()
        if key:
            freq[key] = freq.get(key, 0) + 1
    return freq

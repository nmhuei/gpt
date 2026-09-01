"""Frequency statistics over tokens.

NOTE: normalize_token/tokenize below are copy-pasted duplicates of the
ones in metrics.text -- historical accident, kept in sync by hand.
"""


def normalize_token(token: str) -> str:
    """Lowercase ``token`` and keep only its alphanumeric characters."""
    token = token.strip().lower()
    return "".join(ch for ch in token if ch.isalnum())


def tokenize(text: str) -> list[str]:
    """Split ``text`` on whitespace, normalize each token, drop empties."""
    out: list[str] = []
    for raw in text.split():
        tok = normalize_token(raw)
        if tok:
            out.append(tok)
    return out


def count_frequency(text: str) -> dict[str, int]:
    """Frequency map over normalized tokens of ``text``."""
    freq: dict[str, int] = {}
    for tok in tokenize(text):
        freq[tok] = freq.get(tok, 0) + 1
    return freq

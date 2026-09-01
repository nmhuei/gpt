"""Text tokenization helpers."""


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

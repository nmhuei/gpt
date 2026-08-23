from __future__ import annotations

from gpt.types import ResponseDelta


class MutableTextAccumulator:
    """Converts mutable rendered DOM text into explicit stream revisions.

    Browser-rendered Markdown may revise earlier text. Consumers must not treat
    such a revision as append-only; the gateway buffers it until completion.
    """

    def __init__(self) -> None:
        self._text = ""

    @property
    def text(self) -> str:
        return self._text

    def update(self, current: str) -> ResponseDelta | None:
        if current == self._text:
            return None
        revision = bool(self._text and not current.startswith(self._text))
        delta = "" if revision else current[len(self._text) :]
        self._text = current
        return ResponseDelta(text=delta, accumulated_text=current, revision=revision)

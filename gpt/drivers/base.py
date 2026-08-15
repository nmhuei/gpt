from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol

from gpt.types import ModelInfo, SendRequest, SessionEvent, Turn, TurnResult

EventCallback = Callable[[SessionEvent], Awaitable[None] | None]


class ChatDriver(Protocol):
    """Boundary between session lifecycle and a concrete ChatGPT transport."""

    async def send(
        self,
        request: SendRequest,
        event_callback: EventCallback | None = None,
    ) -> TurnResult: ...

    async def history(self) -> list[Turn]: ...

    async def models(self) -> list[ModelInfo]: ...

    async def select_model(self, model: str) -> ModelInfo: ...

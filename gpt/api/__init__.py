from __future__ import annotations

from typing import Any

from gpt.api.openai_types import format_openai_chat_response, format_openai_chunk
from gpt.api.tool_transpiler import ToolTranspiler


def __getattr__(name: str) -> Any:
    """Lazy server compatibility exports avoid an api<->gateway import cycle."""
    if name in {"WebChatAPIServer", "create_api_app"}:
        from gpt.api import server

        return getattr(server, name)
    raise AttributeError(name)


__all__ = [
    "ToolTranspiler",
    "WebChatAPIServer",
    "create_api_app",
    "format_openai_chat_response",
    "format_openai_chunk",
]

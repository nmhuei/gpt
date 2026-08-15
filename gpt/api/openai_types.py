from __future__ import annotations

import time
import uuid
from typing import Any


def format_openai_chat_response(
    content: str | None,
    tool_calls: list[dict[str, Any]] | None = None,
    model: str = "chatgpt-web",
    system_fingerprint: str = "fp_webgpt_ui",
) -> dict[str, Any]:
    finish_reason = "tool_calls" if tool_calls else "stop"
    message: dict[str, Any] = {
        "role": "assistant",
        "content": content,
    }
    if tool_calls:
        message["tool_calls"] = tool_calls

    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "system_fingerprint": system_fingerprint,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": finish_reason,
            }
        ],
        "usage": None,
    }


def format_openai_chunk(
    delta_content: str | None = None,
    delta_tool_calls: list[dict[str, Any]] | None = None,
    finish_reason: str | None = None,
    model: str = "chatgpt-web",
    completion_id: str | None = None,
) -> str:
    delta: dict[str, Any] = {}
    if delta_content is not None:
        delta["content"] = delta_content
    if delta_tool_calls is not None:
        delta["tool_calls"] = [
            {"index": index, **tool_call}
            for index, tool_call in enumerate(delta_tool_calls)
        ]

    chunk = {
        "id": completion_id or f"chatcmpl-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": delta,
                "finish_reason": finish_reason,
            }
        ],
    }

    import json

    return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"

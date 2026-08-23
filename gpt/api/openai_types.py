from __future__ import annotations

import time
import uuid
from typing import Any

from gpt.assistantturn import AssistantTurn


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


def format_openai_turn(
    turn: AssistantTurn,
    *,
    model: str | None = None,
    system_fingerprint: str = "fp_webgpt_ui",
) -> dict[str, Any]:
    return format_openai_chat_response(
        turn.content,
        turn.tool_calls or None,
        model=model or turn.model or "chatgpt-web",
        system_fingerprint=system_fingerprint,
    )


def format_openai_usage_chunk(
    *,
    model: str = "chatgpt-web",
    completion_id: str | None = None,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
) -> str:
    import json

    chunk = {
        "id": completion_id or f"chatcmpl-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }
    return f"data: {json.dumps(chunk, ensure_ascii=False)}\\n\\n"


def format_openai_chunk(
    delta_content: str | None = None,
    delta_tool_calls: list[dict[str, Any]] | None = None,
    finish_reason: str | None = None,
    model: str = "chatgpt-web",
    completion_id: str | None = None,
    delta_role: str | None = None,
) -> str:
    delta: dict[str, Any] = {}
    if delta_role is not None:
        delta["role"] = delta_role
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

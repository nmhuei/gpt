from __future__ import annotations

import time
import uuid
from typing import Any

from gpt.api.protocol_adapters import (
    estimate_text_chars_to_tokens,
    estimate_tokens_from_chars,
)
from gpt.assistantturn import AssistantTurn


def format_openai_chat_response(
    content: str | None,
    tool_calls: list[dict[str, Any]] | None = None,
    model: str = "chatgpt-web",
    system_fingerprint: str = "fp_webgpt_ui",
    prompt_text: str | None = None,
) -> dict[str, Any]:
    finish_reason = "tool_calls" if tool_calls else "stop"
    message: dict[str, Any] = {
        "role": "assistant",
        "content": content,
    }
    if tool_calls:
        message["tool_calls"] = tool_calls

    # OPENAI-USAGE-WIRE: callers that know the prompt text get a locally
    # estimated usage object (chars/4, PARITY-P0-1) instead of ``None``;
    # unwired callers keep the legacy shape.
    usage = (
        estimate_openai_usage(
            prompt_text=prompt_text,
            completion_text=content,
            completion_tool_calls=tool_calls,
        )
        if prompt_text is not None
        else None
    )

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
        "usage": usage,
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


def _completion_output_chars(
    content: str | None,
    tool_calls: list[dict[str, Any]] | None,
) -> int:
    """Chars a tokenizer would charge as output: text plus tool arguments."""
    total = len(content or "")
    for call in tool_calls or []:
        function = call.get("function") or {}
        total += len(str(function.get("arguments") or ""))
    return total


def estimate_openai_usage(
    *,
    prompt_text: str | None = None,
    completion_text: str | None = None,
    completion_tool_calls: list[dict[str, Any]] | None = None,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
) -> dict[str, Any]:
    """Standard OpenAI usage object estimated locally as chars/4.

    PARITY-P0-1: the web backend exposes no tokenizer, so both stream chunks
    and non-stream responses share this estimator, mirroring the Anthropic
    ``StreamUsageEstimator`` semantics (prompt floors at one token, output at
    zero). Text arguments override explicit token counts when present.
    """
    if prompt_text is not None:
        prompt_tokens = estimate_tokens_from_chars(len(prompt_text))
    if completion_text is not None or completion_tool_calls:
        completion_tokens = estimate_text_chars_to_tokens(
            _completion_output_chars(completion_text, completion_tool_calls)
        )
    return {
        "prompt_tokens": int(prompt_tokens),
        "completion_tokens": int(completion_tokens),
        "total_tokens": int(prompt_tokens) + int(completion_tokens),
    }


def format_openai_usage_chunk(
    *,
    model: str = "chatgpt-web",
    completion_id: str | None = None,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    prompt_text: str | None = None,
    completion_text: str | None = None,
    completion_tool_calls: list[dict[str, Any]] | None = None,
) -> str:
    import json

    # PARITY-P0-1: when the caller supplies raw text instead of token counts
    # (the common case, since the web backend exposes no tokenizer), estimate
    # tokens locally as chars/4 rounded up.  Explicit token counts win when
    # both are provided.
    usage = estimate_openai_usage(
        prompt_text=prompt_text,
        completion_text=completion_text,
        completion_tool_calls=completion_tool_calls,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
    )

    chunk = {
        "id": completion_id or f"chatcmpl-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [],
        "usage": usage,
    }
    # Real newlines, not the escaped "\\n\\n" this used to emit: SSE framing
    # requires an actual blank line to terminate the event.
    return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"


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

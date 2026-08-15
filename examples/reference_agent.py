"""Minimal standard OpenAI tool loop for the WebGPT gateway.

Usage is intentionally framework-neutral: provide ordinary Python callables and
point the OpenAI client at http://127.0.0.1:8765/v1.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from openai import OpenAI


def run_agent(
    client: OpenAI,
    model: str,
    task: str,
    tools: list[dict[str, Any]],
    executors: dict[str, Callable[..., str]],
    max_steps: int = 20,
) -> str:
    messages: list[dict[str, Any]] = [{"role": "user", "content": task}]
    for _ in range(max_steps):
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            tools=tools,
        )
        assistant = response.choices[0].message
        calls = assistant.tool_calls or []
        if not calls:
            return assistant.content or ""
        messages.append(
            {
                "role": "assistant",
                "content": assistant.content,
                "tool_calls": [call.model_dump() for call in calls],
            }
        )
        for call in calls:
            executor = executors.get(call.function.name)
            if executor is None:
                result = f"ERROR: unknown local tool {call.function.name}"
            else:
                try:
                    result = executor(**json.loads(call.function.arguments))
                except Exception as exc:
                    result = f"ERROR: {type(exc).__name__}: {exc}"
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": result,
                }
            )
    raise RuntimeError(f"Agent exceeded max_steps={max_steps}")

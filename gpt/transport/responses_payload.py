from __future__ import annotations

import json
import re
from typing import Any

PROMPT_BLOCK_RE = re.compile(
    r'<WEBGPT_MESSAGE role="(?P<role>[^"\n]+)">\n(?P<message_body>.*?)\n</WEBGPT_MESSAGE>'
    r"|<WEBGPT_TOOL_RESULT>\n(?P<result_body>.*?)\n</WEBGPT_TOOL_RESULT>",
    re.DOTALL,
)


def strip_reasoning_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop replayed Responses reasoning items when store=false."""
    return [item for item in items if item.get("type") != "reasoning"]


def user_input_item(text: str) -> dict[str, Any]:
    return {
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": text}],
    }


def assistant_message_item(text: str) -> dict[str, Any]:
    return {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": text}],
    }


def function_call_item(call: dict[str, Any]) -> dict[str, Any] | None:
    function = call.get("function")
    name = call.get("name")
    arguments = call.get("arguments")
    if isinstance(function, dict):
        name = name or function.get("name")
        arguments = arguments if arguments is not None else function.get("arguments")
    if not isinstance(name, str) or not name:
        return None
    item: dict[str, Any] = {
        "type": "function_call",
        "name": name,
        "arguments": (
            arguments
            if isinstance(arguments, str)
            else json.dumps(arguments or {}, ensure_ascii=False)
        ),
    }
    call_id = call.get("id") or call.get("call_id")
    if isinstance(call_id, str) and call_id:
        item["call_id"] = call_id
    return item


def absorb_message_block(
    role: str,
    body: str,
    instructions: list[str],
    input_items: list[dict[str, Any]],
) -> set[str]:
    content = ""
    tool_calls: list[dict[str, Any]] = []
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, dict):
        raw_content = payload.get("content")
        if isinstance(raw_content, str):
            content = raw_content
        elif raw_content is not None:
            content = json.dumps(raw_content, ensure_ascii=False)
        raw_calls = payload.get("tool_calls")
        if isinstance(raw_calls, list):
            tool_calls = [call for call in raw_calls if isinstance(call, dict)]

    if role in {"system", "developer"}:
        if content:
            instructions.append(content)
        return set()

    if role == "assistant" and tool_calls:
        emitted: set[str] = set()
        if content:
            input_items.append(assistant_message_item(content))
        for call in tool_calls:
            item = function_call_item(call)
            if item is None:
                continue
            call_id = item.get("call_id")
            if isinstance(call_id, str):
                emitted.add(call_id)
            input_items.append(item)
        return emitted

    if role == "assistant":
        input_items.append(assistant_message_item(content))
        return set()

    input_items.append(user_input_item(content))
    return set()


def absorb_tool_result_block(
    body: str,
    input_items: list[dict[str, Any]],
    function_call_ids: set[str],
) -> None:
    call_id: str | None = None
    content = ""
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, dict):
        raw_id = payload.get("id")
        if isinstance(raw_id, str) and raw_id:
            call_id = raw_id
        raw_content = payload.get("content")
        if isinstance(raw_content, str):
            content = raw_content
        elif raw_content is not None:
            content = json.dumps(raw_content, ensure_ascii=False)

    if call_id is not None and call_id in function_call_ids:
        input_items.append(
            {"type": "function_call_output", "call_id": call_id, "output": content}
        )
        return

    text = (
        body
        if call_id is not None
        else f"<WEBGPT_TOOL_RESULT>\n{body}\n</WEBGPT_TOOL_RESULT>"
    )
    input_items.append(user_input_item(text))


def split_prompt_for_responses(
    text: str,
) -> tuple[list[str], list[dict[str, Any]]]:
    matches = list(PROMPT_BLOCK_RE.finditer(text))
    if not matches:
        return [], [user_input_item(text)]

    instructions: list[str] = []
    input_items: list[dict[str, Any]] = []
    function_call_ids: set[str] = set()
    cursor = 0

    def absorb_gap(segment: str) -> None:
        stripped = segment.strip()
        if stripped:
            input_items.append(user_input_item(stripped))

    for match in matches:
        absorb_gap(text[cursor : match.start()])
        cursor = match.end()
        role = match.group("role")
        if role is not None:
            function_call_ids.update(
                absorb_message_block(
                    role,
                    match.group("message_body") or "",
                    instructions,
                    input_items,
                )
            )
        else:
            absorb_tool_result_block(
                match.group("result_body") or "",
                input_items,
                function_call_ids,
            )
    absorb_gap(text[cursor:])
    return instructions, input_items


__all__ = [
    "PROMPT_BLOCK_RE",
    "absorb_message_block",
    "absorb_tool_result_block",
    "assistant_message_item",
    "function_call_item",
    "split_prompt_for_responses",
    "strip_reasoning_items",
    "user_input_item",
]

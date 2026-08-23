from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from typing import Any

from gpt.api.messages import content_text
from gpt.api.requests import (
    ChatCompletionRequest,
    RequestValidationError,
    parse_chat_completion_request,
)


@dataclass(frozen=True)
class AdaptedRequest:
    request: ChatCompletionRequest
    previous_response_id: str | None = None


def _text_blocks(content: Any, allowed_types: set[str]) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(
        str(block.get("text", ""))
        for block in content
        if isinstance(block, dict) and block.get("type") in allowed_types
    )


def _responses_tools(raw_tools: Any) -> list[dict[str, Any]]:
    if raw_tools is None:
        return []
    if not isinstance(raw_tools, list):
        raise RequestValidationError("tools must be an array")
    converted: list[dict[str, Any]] = []
    for tool in raw_tools:
        if not isinstance(tool, dict) or tool.get("type") != "function":
            raise RequestValidationError("Only function tools are supported.")
        if isinstance(tool.get("function"), dict):
            converted.append(tool)
            continue
        name = tool.get("name")
        if not isinstance(name, str) or not name.strip():
            raise RequestValidationError("Function tool requires a non-empty name.")
        converted.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": tool.get("description", ""),
                    "parameters": tool.get("parameters", {}),
                },
            }
        )
    return converted


def _responses_tool_choice(raw: Any) -> Any:
    if raw is None or isinstance(raw, str):
        return raw if raw is not None else "auto"
    if not isinstance(raw, dict):
        raise RequestValidationError("Unsupported tool_choice.")
    if raw.get("type") == "function" and isinstance(raw.get("function"), dict):
        return raw
    if raw.get("type") == "function" and isinstance(raw.get("name"), str):
        return {"type": "function", "function": {"name": raw["name"]}}
    raise RequestValidationError("Only function tool_choice objects are supported.")


def parse_responses_request(body: Any) -> AdaptedRequest:
    if not isinstance(body, dict):
        raise RequestValidationError("Request body must be an object.")
    raw_input = body.get("input")
    messages: list[dict[str, Any]] = []
    if isinstance(raw_input, str):
        messages.append({"role": "user", "content": raw_input})
    elif isinstance(raw_input, list):
        for item in raw_input:
            if not isinstance(item, dict):
                raise RequestValidationError("input items must be objects.")
            item_type = item.get("type")
            if item_type == "function_call_output":
                call_id = item.get("call_id")
                if not isinstance(call_id, str) or not call_id:
                    raise RequestValidationError("function_call_output requires call_id.")
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": content_text(item.get("output")),
                    }
                )
                continue
            role = item.get("role", "user")
            if role not in {"system", "developer", "user", "assistant"}:
                raise RequestValidationError("Unsupported Responses input role.")
            text = _text_blocks(item.get("content"), {"input_text", "output_text", "text"})
            if not text and isinstance(item.get("content"), str):
                text = item["content"]
            messages.append({"role": role, "content": text})
    else:
        raise RequestValidationError("input must be a string or non-empty array.")
    if not messages:
        raise RequestValidationError("input must not be empty.")

    instructions = body.get("instructions")
    if instructions is not None:
        if not isinstance(instructions, str):
            raise RequestValidationError("instructions must be a string.")
        messages.insert(0, {"role": "system", "content": instructions})
    tools = _responses_tools(body.get("tools"))
    synthetic = {
        "model": body.get("model") or "chatgpt-web",
        "messages": messages,
        "tools": tools,
        "tool_choice": _responses_tool_choice(body.get("tool_choice")),
        "stream": body.get("stream", False),
        "reasoning": body.get("reasoning"),
    }
    if "reasoning_effort" in body:
        synthetic["reasoning_effort"] = body["reasoning_effort"]
    parsed = parse_chat_completion_request(synthetic, protocol="openai_responses")
    previous = body.get("previous_response_id")
    if previous is not None and (not isinstance(previous, str) or not previous):
        raise RequestValidationError("previous_response_id must be a non-empty string.")
    return AdaptedRequest(request=parsed, previous_response_id=previous)


def _anthropic_tools(raw_tools: Any) -> list[dict[str, Any]]:
    if raw_tools is None:
        return []
    if not isinstance(raw_tools, list):
        raise RequestValidationError("tools must be an array")
    tools: list[dict[str, Any]] = []
    for tool in raw_tools:
        if not isinstance(tool, dict):
            raise RequestValidationError("tools items must be objects")
        name = tool.get("name")
        if not isinstance(name, str) or not name:
            raise RequestValidationError("Anthropic tool requires name.")
        parameters = tool.get("input_schema", {})
        if not isinstance(parameters, dict):
            raise RequestValidationError("input_schema must be an object.")
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": tool.get("description", ""),
                    "parameters": parameters,
                },
            }
        )
    return tools


def _anthropic_tool_choice(raw: Any) -> Any:
    if raw is None:
        return "auto"
    if not isinstance(raw, dict):
        raise RequestValidationError("tool_choice must be an object.")
    kind = raw.get("type", "auto")
    if kind == "auto":
        return "auto"
    if kind == "any":
        return "required"
    if kind == "tool":
        name = raw.get("name")
        if not isinstance(name, str) or not name:
            raise RequestValidationError("tool_choice type=tool requires name.")
        return {"type": "function", "function": {"name": name}}
    raise RequestValidationError("Unsupported Anthropic tool_choice.")


def parse_anthropic_request(body: Any) -> AdaptedRequest:
    if not isinstance(body, dict):
        raise RequestValidationError("Request body must be an object.")
    raw_messages = body.get("messages")
    if not isinstance(raw_messages, list) or not raw_messages:
        raise RequestValidationError("messages must be a non-empty array.")
    messages: list[dict[str, Any]] = []
    system = body.get("system")
    if isinstance(system, str):
        messages.append({"role": "system", "content": system})
    elif isinstance(system, list):
        messages.append({"role": "system", "content": _text_blocks(system, {"text"})})
    elif system is not None:
        raise RequestValidationError("system must be a string or text block array.")

    for message in raw_messages:
        if not isinstance(message, dict):
            raise RequestValidationError("messages items must be objects.")
        raw_role = str(message.get("role", "user")).lower()
        if raw_role in {"system", "developer"}:
            messages.append({"role": "system", "content": content_text(message.get("content"))})
            continue
        if raw_role in {"user", "human"}:
            role = "user"
        elif raw_role in {"assistant", "model", "ai"}:
            role = "assistant"
        else:
            role = "user"
        content = message.get("content")
        if isinstance(content, str):
            messages.append({"role": role, "content": content})
            continue
        if not isinstance(content, list):
            raise RequestValidationError("message content must be a string or block array.")
        text = _text_blocks(content, {"text"})
        assistant_calls: list[dict[str, Any]] = []
        if role == "user" and text:
            messages.append({"role": "user", "content": text})
        for block in content:
            if not isinstance(block, dict):
                continue
            if role == "assistant" and block.get("type") == "tool_use":
                call_id, name = block.get("id"), block.get("name")
                if not isinstance(call_id, str) or not isinstance(name, str):
                    raise RequestValidationError("tool_use requires id and name.")
                assistant_calls.append(
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": name,
                            "arguments": json.dumps(block.get("input", {})),
                        },
                    }
                )
            elif role == "user" and block.get("type") == "tool_result":
                call_id = block.get("tool_use_id")
                if not isinstance(call_id, str):
                    raise RequestValidationError("tool_result requires tool_use_id.")
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": _text_blocks(block.get("content"), {"text"})
                        if isinstance(block.get("content"), list)
                        else content_text(block.get("content")),
                    }
                )
        if role == "assistant" and (text or assistant_calls):
            assistant: dict[str, Any] = {"role": "assistant", "content": text or None}
            if assistant_calls:
                assistant["tool_calls"] = assistant_calls
            messages.append(assistant)
    if not messages:
        raise RequestValidationError("messages must contain supported content.")
    tools = _anthropic_tools(body.get("tools"))
    synthetic = {
        "model": body.get("model") or "chatgpt-web",
        "messages": messages,
        "tools": tools,
        "tool_choice": _anthropic_tool_choice(body.get("tool_choice")),
        "stream": body.get("stream", False),
    }
    if "max_tokens" in body:
        synthetic["max_tokens"] = body.get("max_tokens")
    return AdaptedRequest(
        request=parse_chat_completion_request(synthetic, protocol="anthropic_messages")
    )


def estimate_anthropic_input_tokens(body: Any) -> int:
    """Return a deterministic local estimate for the Anthropic token-count API.

    ChatGPT Web does not expose Anthropic's tokenizer, so this intentionally
    uses the same normalized message/tool representation that is submitted to
    the gateway and estimates four UTF-8 bytes per token.  Valid requests
    always report at least one input token.
    """
    adapted = parse_anthropic_request(body)
    normalized = {
        "system_and_messages": adapted.request.messages,
        "tools": adapted.request.tools,
        "tool_choice": adapted.request.tool_choice,
    }
    encoded = json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return max(1, (len(encoded) + 3) // 4)


def response_to_responses(
    response: dict[str, Any], *, response_id: str | None = None
) -> dict[str, Any]:
    message = response["choices"][0]["message"]
    output: list[dict[str, Any]] = []
    if message.get("content") is not None:
        output.append(
            {
                "id": f"msg_{uuid.uuid4().hex[:16]}",
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": message["content"], "annotations": []}],
            }
        )
    for call in message.get("tool_calls") or []:
        function = call["function"]
        output.append(
            {
                "id": call["id"],
                "type": "function_call",
                "status": "completed",
                "call_id": call["id"],
                "name": function["name"],
                "arguments": function["arguments"],
            }
        )
    return {
        "id": response_id or f"resp_{uuid.uuid4().hex[:16]}",
        "object": "response",
        "created_at": int(time.time()),
        "status": "completed",
        "model": response["model"],
        "output": output,
        "error": None,
        "incomplete_details": None,
        "usage": None,
    }


def response_to_anthropic(response: dict[str, Any]) -> dict[str, Any]:
    message = response["choices"][0]["message"]
    content: list[dict[str, Any]] = []
    if message.get("content") is not None:
        content.append({"type": "text", "text": message["content"]})
    for call in message.get("tool_calls") or []:
        function = call["function"]
        try:
            arguments = json.loads(function["arguments"])
        except (TypeError, ValueError):
            arguments = {}
        content.append(
            {
                "type": "tool_use",
                "id": call["id"],
                "name": function["name"],
                "input": arguments,
            }
        )
    return {
        "id": f"msg_{uuid.uuid4().hex[:16]}",
        "type": "message",
        "role": "assistant",
        "model": response["model"],
        "content": content,
        "stop_reason": "tool_use" if message.get("tool_calls") else "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }

from __future__ import annotations

import json
import re
import uuid
from typing import Any

from gpt.state import MalformedToolCall

_OPEN = "<WEBGPT_TOOL_CALL>"
_CLOSE = "</WEBGPT_TOOL_CALL>"
_BLOCK_RE = re.compile(r"<WEBGPT_TOOL_CALL>\s*([\s\S]*?)\s*</WEBGPT_TOOL_CALL>")
_TOOL_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$")


class ToolTranspiler:
    """Strict controller/model tool protocol mapped to OpenAI tool calls."""

    @staticmethod
    def validate_tools(tools: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        validated: dict[str, dict[str, Any]] = {}
        for tool in tools:
            if not isinstance(tool, dict) or tool.get("type") != "function":
                raise ValueError("Only tools with type='function' are supported.")
            function = tool.get("function")
            if not isinstance(function, dict):
                raise ValueError("Tool function must be an object.")
            name = function.get("name")
            if not isinstance(name, str) or not _TOOL_NAME_RE.fullmatch(name):
                raise ValueError(f"Invalid tool name: {name!r}")
            if name in validated:
                raise ValueError(f"Duplicate tool name: {name}")
            parameters = function.get("parameters", {"type": "object"})
            if not isinstance(parameters, dict):
                raise ValueError(f"parameters for {name} must be an object.")
            validated[name] = function
        return validated

    @classmethod
    def build_tool_instructions(
        cls,
        tools: list[dict[str, Any]],
        tool_choice: Any = "auto",
    ) -> str:
        available = cls.validate_tools(tools)
        if not available:
            return ""
        choice_instruction = "Use a tool only when needed."
        if not (
            isinstance(tool_choice, dict)
            or tool_choice is None
            or (isinstance(tool_choice, str) and tool_choice in {"auto", "none", "required"})
        ):
            raise ValueError(f"Unsupported tool_choice: {tool_choice!r}")
        if tool_choice == "none":
            choice_instruction = "Do not call any tool; answer using available information."
        elif tool_choice == "required":
            choice_instruction = "You must call exactly one available tool."
        elif isinstance(tool_choice, dict):
            selected = tool_choice.get("function", {}).get("name")
            if selected not in available:
                raise ValueError(f"tool_choice refers to an unknown tool: {selected}")
            choice_instruction = f"You must call exactly the tool named {selected}."
        declarations = [
            {
                "name": name,
                "description": definition.get("description", ""),
                "parameters": definition.get("parameters", {"type": "object"}),
            }
            for name, definition in available.items()
        ]
        return (
            "WEBGPT CONTROLLER TOOL PROTOCOL (highest priority for tool formatting):\n"
            f"Available tools: {json.dumps(declarations, ensure_ascii=False, separators=(',', ':'))}\n"
            f"{choice_instruction}\n"
            "These are external controller functions, not native ChatGPT integrations. "
            "You are not expected to execute or access them yourself. Requesting one means "
            "printing the envelope below; the controller will execute it and return a result. "
            "Do not claim an advertised function is unavailable.\n"
            "To request tools, output one or more sentinel blocks and no other text. "
            "Use one block per call:\n"
            f"{_OPEN}\n"
            '{"name":"tool_name","arguments":{"arg":"value"}}\n'
            f"{_CLOSE}\n"
            "Never invent tool results. Only WEBGPT_TOOL_RESULT blocks supplied by the "
            "controller are authoritative. Ordinary prose, Markdown, and JSON are not tool calls."
        )

    @classmethod
    def parse_tool_calls(
        cls,
        text: str,
        allowed_tools: set[str] | None = None,
        max_arguments_bytes: int = 65_536,
    ) -> tuple[str | None, list[dict[str, Any]]]:
        if not isinstance(text, str) or not text:
            return None, []
        has_sentinel = _OPEN in text or _CLOSE in text
        blocks = list(_BLOCK_RE.finditer(text))
        if not has_sentinel:
            return text, []
        if (
            text.count(_OPEN) != text.count(_CLOSE)
            or text.count(_OPEN) != len(blocks)
            or not blocks
        ):
            raise MalformedToolCall("WEBGPT_TOOL_CALL blocks are incomplete or nested.")
        outside = _BLOCK_RE.sub("", text)
        if outside.strip():
            raise MalformedToolCall("Tool call cannot be mixed with final assistant prose.")
        calls: list[dict[str, Any]] = []
        signatures: set[tuple[str, str]] = set()
        for match in blocks:
            try:
                payload = json.loads(match.group(1))
            except json.JSONDecodeError as exc:
                raise MalformedToolCall("Tool call payload is invalid JSON.") from exc
            if not isinstance(payload, dict) or set(payload) != {"name", "arguments"}:
                raise MalformedToolCall("Tool call requires exactly name and arguments.")
            name = payload["name"]
            arguments = payload["arguments"]
            if not isinstance(name, str) or not _TOOL_NAME_RE.fullmatch(name):
                raise MalformedToolCall("Tool call name is missing or invalid.")
            if allowed_tools is not None and name not in allowed_tools:
                raise MalformedToolCall(f"Unknown tool requested: {name}")
            if not isinstance(arguments, dict):
                raise MalformedToolCall("Tool call arguments must be a JSON object.")
            arguments_json = json.dumps(
                arguments, ensure_ascii=False, separators=(",", ":")
            )
            if len(arguments_json.encode("utf-8")) > max_arguments_bytes:
                raise MalformedToolCall("Tool call arguments exceed the configured limit.")
            signature = (name, arguments_json)
            if signature in signatures:
                raise MalformedToolCall("Duplicate tool call blocks are ambiguous.")
            signatures.add(signature)
            calls.append(
                {
                    "id": f"call_{uuid.uuid4().hex[:16]}",
                    "type": "function",
                    "function": {"name": name, "arguments": arguments_json},
                }
            )
        return None, calls

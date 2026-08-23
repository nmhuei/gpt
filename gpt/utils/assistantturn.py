from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from gpt.state import MalformedToolCall
from gpt.toolcall import ToolTranspiler

FinishReason = Literal["stop", "tool_calls", "interrupted", "error"]


@dataclass
class AssistantTurn:
    """Protocol-neutral semantic result of one model completion."""

    raw_text: str
    content: str | None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    finish_reason: FinishReason = "stop"
    model: str | None = None
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class AssistantTurnBuilder:
    @staticmethod
    def from_model_text(
        text: str,
        *,
        tools: list[dict[str, Any]],
        tool_choice: Any,
        model: str | None = None,
    ) -> AssistantTurn:
        allowed = set(ToolTranspiler.validate_tools(tools))
        content, calls = ToolTranspiler.parse_tool_calls(
            text,
            allowed_tools=allowed,
            tool_definitions=tools,
        )
        if tool_choice == "none" and calls:
            raise MalformedToolCall("Model emitted a tool call while tool_choice=none.")
        required = tool_choice == "required" or isinstance(tool_choice, dict)
        if required and not calls:
            raise MalformedToolCall("Model did not emit the required tool call.")
        if isinstance(tool_choice, dict) and calls:
            function = tool_choice.get("function")
            selected = function.get("name") if isinstance(function, dict) else None
            if any(call["function"]["name"] != selected for call in calls):
                raise MalformedToolCall("Model called a tool different from tool_choice.")
        return AssistantTurn(
            raw_text=text,
            content=content,
            tool_calls=calls,
            finish_reason="tool_calls" if calls else "stop",
            model=model,
        )


__all__ = ["AssistantTurn", "AssistantTurnBuilder", "FinishReason"]

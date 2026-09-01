from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from gpt.state import MalformedToolCall
from gpt.toolcall import ToolTranspiler
from gpt.utils.toolcall import resolve_tool_protocol

_OPEN = "<WEBGPT_TOOL_CALL>"
_SOFT_OPENS = ("<cmd>", "<json>")


@dataclass
class ToolStreamResult:
    text_deltas: list[str] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)


class ToolStreamSieve:
    """Incremental anti-leak sieve for the strict WEBGPT tool protocol.

    A pure tool response must contain only sentinel blocks and whitespace. The
    sieve therefore buffers output while it can still be a tool response. Once
    ordinary prose is proven, text can stream, but a later sentinel is treated
    as a malformed mixed response and is never promoted to an executable call.
    """

    def __init__(
        self,
        *,
        tools: list[dict[str, Any]],
        tool_choice: Any = "auto",
        tail_guard: int | None = None,
    ) -> None:
        self.tools = tools
        self.tool_choice = tool_choice
        self.protocol = resolve_tool_protocol()
        candidate_opens = (_OPEN, *_SOFT_OPENS) if self.protocol == "soft" else (_OPEN,)
        self._candidate_opens = candidate_opens
        self.tail_guard = tail_guard or max(max(map(len, candidate_opens)) - 1, 1)
        self._buffer = ""
        self._all_text = ""
        self._mode = "undecided"
        self._mixed_sentinel = False

    @property
    def mode(self) -> str:
        return self._mode

    def feed(self, chunk: str) -> ToolStreamResult:
        if not chunk:
            return ToolStreamResult()
        self._all_text += chunk
        self._buffer += chunk
        emitted: list[str] = []

        if self._mode == "undecided":
            stripped = self._buffer.lstrip()
            if not stripped:
                return ToolStreamResult()
            if any(
                opening.startswith(stripped) or stripped.startswith(opening)
                for opening in self._candidate_opens
            ):
                self._mode = "tool_candidate"
                return ToolStreamResult()
            self._mode = "text"

        if self._mode == "tool_candidate":
            # Never leak a possible sentinel payload. Finalize performs the
            # authoritative strict parse once the whole assistant turn exists.
            return ToolStreamResult()

        # Text mode: withhold a short suffix so a tool marker split
        # across chunks cannot leak before we can classify it as a mixed
        # malformed response. Under the soft protocol this includes <cmd> and
        # <json>, not only the historical WEBGPT sentinel.
        marker_positions = [
            (self._buffer.index(opening), opening)
            for opening in self._candidate_opens
            if opening in self._buffer
        ]
        if marker_positions:
            sentinel_at, _opening = min(marker_positions, key=lambda item: item[0])
            prefix = self._buffer[:sentinel_at]
            if prefix:
                emitted.append(prefix)
            self._buffer = self._buffer[sentinel_at:]
            self._mixed_sentinel = True
            return ToolStreamResult(text_deltas=emitted)

        if len(self._buffer) > self.tail_guard:
            safe = self._buffer[: -self.tail_guard]
            self._buffer = self._buffer[-self.tail_guard :]
            if safe:
                emitted.append(safe)
        return ToolStreamResult(text_deltas=emitted)

    def finalize(self) -> ToolStreamResult:
        if self._mode == "tool_candidate":
            clean, calls = ToolTranspiler.parse_tool_calls(
                self._all_text,
                allowed_tools=set(ToolTranspiler.validate_tools(self.tools)),
                tool_definitions=self.tools,
            )
            required = self.tool_choice == "required" or isinstance(self.tool_choice, dict)
            if clean is not None and required:
                raise MalformedToolCall("Model did not emit the required tool call.")
            if self.tool_choice == "none" and calls:
                raise MalformedToolCall("Model emitted a tool call while tool_choice=none.")
            if isinstance(self.tool_choice, dict) and calls:
                function = self.tool_choice.get("function")
                selected = function.get("name") if isinstance(function, dict) else None
                if any(call["function"]["name"] != selected for call in calls):
                    raise MalformedToolCall("Model called a tool different from tool_choice.")
            return ToolStreamResult(text_deltas=[clean] if clean else [], tool_calls=calls)

        if self._mixed_sentinel:
            raise MalformedToolCall(
                "Tool sentinel appeared after ordinary assistant prose; mixed output is not executable."
            )
        tail = self._buffer
        self._buffer = ""
        clean, calls = ToolTranspiler.parse_tool_calls(
            self._all_text,
            allowed_tools=set(ToolTranspiler.validate_tools(self.tools)),
            tool_definitions=self.tools,
        )
        required = self.tool_choice == "required" or isinstance(self.tool_choice, dict)
        if required and not calls:
            raise MalformedToolCall("Model did not emit the required tool call.")
        if calls:
            # This should only happen if a response became a tool candidate
            # after text was already proven; fail closed rather than exposing a
            # late tool call whose prefix may have streamed as prose.
            raise MalformedToolCall("Late tool call cannot be safely streamed.")
        return ToolStreamResult(text_deltas=[tail] if tail else [])


__all__ = ["ToolStreamResult", "ToolStreamSieve"]

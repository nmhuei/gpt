from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from gpt.toolcall import ToolTranspiler

_ALLOWED_ROLES = frozenset({"system", "developer", "user", "assistant", "tool"})
_REASONING_EFFORTS = frozenset({"instant", "low", "medium", "high", "max"})
_KNOWN_FIELDS = frozenset(
    {
        "model",
        "messages",
        "tools",
        "tool_choice",
        "stream",
        "temperature",
        "reasoning_effort",
        "reasoning",
        "top_p",
        "n",
        "seed",
        "logprobs",
        "response_format",
        "parallel_tool_calls",
        "stream_options",
        "stop",
        "max_tokens",
        "max_completion_tokens",
        "presence_penalty",
        "frequency_penalty",
        "user",
    }
)


class RequestValidationError(ValueError):
    """A client request cannot be represented by the gateway runtime."""


def _validate_compatibility_fields(body: dict[str, Any]) -> None:
    unknown = sorted(set(body) - _KNOWN_FIELDS)
    if unknown:
        raise RequestValidationError(
            f"Unsupported request field(s): {', '.join(unknown)}"
        )
    top_p = body.get("top_p")
    if top_p is not None and top_p != 1 and top_p != 1.0:
        raise RequestValidationError("top_p is unsupported except for the neutral value 1")
    n = body.get("n")
    if n is not None and n != 1:
        raise RequestValidationError("n is unsupported except for n=1")
    for field_name in ("presence_penalty", "frequency_penalty"):
        value = body.get(field_name)
        if value is not None and value != 0 and value != 0.0:
            raise RequestValidationError(
                f"{field_name} is unsupported except for the neutral value 0"
            )
    if body.get("seed") is not None:
        raise RequestValidationError("seed cannot be represented by ChatGPT Web")
    if body.get("logprobs") not in {None, False}:
        raise RequestValidationError("logprobs are not supported")
    response_format = body.get("response_format")
    if response_format is not None and not (
        isinstance(response_format, dict)
        and response_format.get("type") == "text"
        and set(response_format) == {"type"}
    ):
        raise RequestValidationError("Only response_format={'type':'text'} is supported")
    if body.get("parallel_tool_calls") is False:
        raise RequestValidationError(
            "parallel_tool_calls=false cannot be enforced by the current Web tool protocol"
        )
    stream_options = body.get("stream_options")
    if stream_options is not None:
        if not isinstance(stream_options, dict):
            raise RequestValidationError("stream_options must be an object")
        if set(stream_options) - {"include_usage"}:
            raise RequestValidationError("Unsupported stream_options field")
        include_usage = stream_options.get("include_usage")
        if include_usage is not None and not isinstance(include_usage, bool):
            raise RequestValidationError("stream_options.include_usage must be a boolean")
    stop = body.get("stop")
    if stop is not None and stop != "" and stop != []:
        raise RequestValidationError("stop sequences are not supported")
    for field_name in ("max_tokens", "max_completion_tokens"):
        value = body.get(field_name)
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
        ):
            raise RequestValidationError(f"{field_name} must be a positive integer")
    user = body.get("user")
    if user is not None and not isinstance(user, str):
        raise RequestValidationError("user must be a string when provided")


@dataclass(frozen=True)
class ChatCompletionRequest:
    """Protocol-neutral input used by the conversation runtime.

    The HTTP adapter owns JSON decoding; the runtime only receives this
    validated, immutable representation. Protocol/client metadata is structural
    evidence only and never changes browser semantics.
    """

    messages: list[dict[str, Any]]
    requested_model: str
    tools: list[dict[str, Any]]
    tool_choice: Any
    stream: bool
    reasoning_effort: str | None
    temperature: float | None
    protocol: str = "openai_chat"
    client: str = "unknown"
    stream_include_usage: bool = False
    max_tokens_advisory: int | None = None
    # Transport metadata is retained for protocol adapters but never rendered
    # into the ChatGPT Web prompt.
    request_headers: dict[str, str] = field(default_factory=dict)


def parse_chat_completion_request(
    body: Any,
    *,
    protocol: str = "openai_chat",
    client: str = "unknown",
) -> ChatCompletionRequest:
    if not isinstance(body, dict):
        raise RequestValidationError("Request body must be an object")
    _validate_compatibility_fields(body)

    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise RequestValidationError("messages must be a non-empty array")
    if any(not isinstance(message, dict) for message in messages):
        raise RequestValidationError("Each message must be an object")
    if any(message.get("role") not in _ALLOWED_ROLES for message in messages):
        raise RequestValidationError("Unsupported message role")

    requested_model = body.get("model") or "chatgpt-web"
    if not isinstance(requested_model, str) or not requested_model.strip():
        raise RequestValidationError("model must be a non-empty string")

    tools = body.get("tools") or []
    if not isinstance(tools, list):
        raise RequestValidationError("tools must be an array")
    try:
        ToolTranspiler.validate_tools(tools)
        ToolTranspiler.validate_tool_choice(tools, body.get("tool_choice", "auto"))
    except ValueError as exc:
        raise RequestValidationError(str(exc)) from exc

    stream = body.get("stream", False)
    if not isinstance(stream, bool):
        raise RequestValidationError("stream must be a boolean")

    temperature = body.get("temperature")
    if temperature is not None and (
        isinstance(temperature, bool) or not isinstance(temperature, (int, float))
    ):
        raise RequestValidationError("temperature must be a number")

    reasoning_effort = body.get("reasoning_effort")
    if reasoning_effort is None and isinstance(body.get("reasoning"), dict):
        reasoning_effort = body["reasoning"].get("effort")
    if reasoning_effort is not None:
        if not isinstance(reasoning_effort, str):
            raise RequestValidationError("reasoning_effort must be a string")
        reasoning_effort = reasoning_effort.strip().lower()
        if reasoning_effort not in _REASONING_EFFORTS:
            allowed = ", ".join(sorted(_REASONING_EFFORTS))
            raise RequestValidationError(f"reasoning_effort must be one of: {allowed}")

    stream_options = body.get("stream_options") or {}
    max_tokens_advisory = body.get("max_completion_tokens")
    if max_tokens_advisory is None:
        max_tokens_advisory = body.get("max_tokens")

    return ChatCompletionRequest(
        messages=messages,
        requested_model=requested_model.strip(),
        tools=tools,
        tool_choice=body.get("tool_choice", "auto"),
        stream=stream,
        reasoning_effort=reasoning_effort,
        temperature=float(temperature) if temperature is not None else None,
        protocol=protocol,
        client=client,
        stream_include_usage=bool(stream_options.get("include_usage", False)),
        max_tokens_advisory=max_tokens_advisory,
    )

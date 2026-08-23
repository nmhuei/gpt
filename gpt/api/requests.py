"""Backward-compatible API imports for protocol-neutral request normalization."""

from gpt.requests import (
    ChatCompletionRequest,
    RequestValidationError,
    parse_chat_completion_request,
)

__all__ = [
    "ChatCompletionRequest",
    "RequestValidationError",
    "parse_chat_completion_request",
]

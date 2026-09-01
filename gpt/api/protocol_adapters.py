from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any

from gpt.api.messages import content_text, render_messages
from gpt.api.requests import (
    ChatCompletionRequest,
    RequestValidationError,
    parse_chat_completion_request,
)
from gpt.promptcompat import image_placeholder_enabled

logger = logging.getLogger(__name__)


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


# ---------------------------------------------------------------------------
# ANTHROPIC-INGRESS-IMAGE (2026-08-26)
#
# ``parse_anthropic_request`` used to strip every ``type=image`` block at
# ingress (``_text_blocks(content, {"text"})``), so the P1-2A render-layer
# placeholder in ``gpt.utils.promptcompat`` never fired on the real
# ``/v1/messages`` path -- images were still dropped silently one hop earlier.
#
# Fix: user-role block arrays and ``tool_result`` block arrays are walked in
# original order here; each ``image`` block contributes the SAME shared P1-2A
# placeholder line (via ``content_text([block])``, which honors the
# ``WEBGPT_IMAGE_PLACEHOLDER=0`` rollback switch by contributing nothing).
# Only metadata travels downstream -- the base64 payload itself is discarded
# at this boundary and never decoded or uploaded.  Text-only payloads are
# byte-identical to the previous ``_text_blocks`` output.
#
# ANTHROPIC-FIELDS-EXPLICIT (2026-08-26): ``type=document`` blocks (PDFs)
# get the same treatment via a local ``[document omitted: ...]`` marker --
# ``content_text`` cannot host it because that helper lives outside this
# file's ownership boundary, so the marker mirrors the image pattern here.
# The same rollback switch gates it: with ``WEBGPT_IMAGE_PLACEHOLDER=0``
# documents are silently dropped again, exactly like images.
# ---------------------------------------------------------------------------

_IMAGE_UPLOAD_WEB_FLAG = "WEBGPT_IMAGE_UPLOAD_WEB"
_IMAGE_MARKER_TMPL = '<WEBGPT_IMAGE_DATA mime="{mime}">{data}</WEBGPT_IMAGE_DATA>'
_IMAGE_MAX_B64_CHARS = 20 * 1024 * 1024
_IMAGE_MIME_RE = re.compile(r"[A-Za-z0-9.+-]+/[A-Za-z0-9.+-]+")
_IMAGE_B64_RE = re.compile(r"[A-Za-z0-9+/=]+")
_TRUTHY = {"1", "true", "yes", "on"}


def _image_upload_ingest_enabled() -> bool:
    """Whether Anthropic inline images should survive ingress for web upload."""
    return os.environ.get(_IMAGE_UPLOAD_WEB_FLAG, "").strip().casefold() in _TRUTHY


def _image_upload_marker(block: dict[str, Any]) -> str | None:
    """Return a strict transport marker for one inline Anthropic base64 image."""
    source = block.get("source")
    if not isinstance(source, dict) or source.get("type") != "base64":
        return None
    mime = source.get("media_type")
    data = source.get("data")
    if not isinstance(mime, str) or not isinstance(data, str):
        return None
    mime = mime.strip()
    compact = "".join(data.split())
    if not mime or not compact:
        return None
    if _IMAGE_MIME_RE.fullmatch(mime) is None or _IMAGE_B64_RE.fullmatch(compact) is None:
        logger.info("Dropped Anthropic image with malformed mime/base64 upload payload.")
        return None
    if len(compact) > _IMAGE_MAX_B64_CHARS:
        logger.warning(
            "Skipping Anthropic image upload marker (%s, ~%dKB): exceeds %dMB ingress cap.",
            mime,
            len(compact) // 1024,
            _IMAGE_MAX_B64_CHARS // (1024 * 1024),
        )
        return None
    return _IMAGE_MARKER_TMPL.format(mime=mime, data=compact)


def _document_placeholder(block: dict[str, Any]) -> str:
    """Stand-in text describing one document (PDF) block this gateway cannot upload."""
    descriptors: list[str] = []
    title = block.get("title")
    if isinstance(title, str) and title.strip():
        descriptors.append(title.strip())
    source = block.get("source")
    if isinstance(source, dict):
        media_type = source.get("media_type")
        if isinstance(media_type, str) and media_type.strip():
            descriptors.append(media_type.strip())
    description = ", ".join(descriptors) if descriptors else "unknown"
    return f"[document omitted: {description}]"


def _block_sequence_text(content: Any) -> str:
    """Ordered text extraction with image/document placeholders for Anthropic blocks.

    Mirrors ``_text_blocks(content, {"text"})`` for text-only payloads;
    additionally converts ``type=image`` blocks into the shared placeholder
    line and ``type=document`` (PDF) blocks into the local document marker so
    they survive ingress and reach the render layer as plain text.
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "image":
            upload_marker = _image_upload_marker(block) if _image_upload_ingest_enabled() else None
            if upload_marker:
                parts.append(upload_marker)
            else:
                marker = content_text([block])
                if marker:
                    parts.append(marker)
        elif block_type == "document":
            if image_placeholder_enabled():
                parts.append(_document_placeholder(block))
        elif block_type == "text":
            parts.append(str(block.get("text", "")))
    return "\n".join(parts)


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


# ---------------------------------------------------------------------------
# Usage estimation (PARITY-P0-1)
#
# ChatGPT Web does not expose an Anthropic-compatible tokenizer, so every
# usage figure this module emits is a LOCAL ESTIMATE: one token per four
# characters, rounded up.  Claude Code only needs a roughly-correct context
# size to trigger auto-compact before the prompt ceiling is hit (P0-1 in
# docs/reports/api-parity-audit-2026-08-24.md); chars/4 approximates that
# well enough for English/code payloads.
# ---------------------------------------------------------------------------

_ESTIMATED_CHARS_PER_TOKEN = 4


def estimate_tokens_from_chars(char_count: int) -> int:
    """Ceil(char_count / 4), minimum one token.

    This is an estimate because the web backend exposes no tokenizer; see the
    PARITY-P0-1 comment block above.
    """
    return max(1, (int(char_count) + _ESTIMATED_CHARS_PER_TOKEN - 1) // _ESTIMATED_CHARS_PER_TOKEN)


def estimate_text_chars_to_tokens(char_count: int) -> int:
    """Ceil(char_count / 4) for generated output; empty output stays at zero.

    Unlike prompt inputs (whose estimates floor at one token), an empty
    completion legitimately reports zero output tokens.
    """
    return (int(char_count) + _ESTIMATED_CHARS_PER_TOKEN - 1) // _ESTIMATED_CHARS_PER_TOKEN


def estimate_text_tokens(text: str | None) -> int:
    """Estimated token count for generated text (0 for empty text)."""
    stripped = text or ""
    if not stripped:
        return 0
    return estimate_text_chars_to_tokens(len(stripped))


def anthropic_usage(input_tokens: int = 0, output_tokens: int = 0) -> dict[str, Any]:
    """Full standard Anthropic usage object.

    Cache fields stay at zero: the web backend performs no prompt caching we
    can observe, but Claude Code expects the keys to exist in the schema.
    """
    return {
        "input_tokens": int(input_tokens),
        "output_tokens": int(output_tokens),
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    }


def _response_output_chars(message: dict[str, Any]) -> int:
    """Count response characters that a tokenizer would charge as output."""
    total = len(message.get("content") or "")
    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        total += len(function.get("arguments") or "")
    return total


class StreamUsageEstimator:
    """Incremental chars/4 usage tracker for Anthropic SSE envelopes.

    Usage pattern for an SSE turn:

    * construct with the rendered prompt text, then emit ``snapshot()`` as the
      ``message_start`` ``usage`` (prompt-only estimate);
    * call :meth:`add_delta` for every ``content_block_delta`` text chunk;
    * emit ``snapshot()`` again as the final ``message_delta`` ``usage`` so
      output_tokens rises monotonically to the full-text estimate.

    All values are estimates (chars/4) because the web backend exposes no
    tokenizer; see the PARITY-P0-1 comment block above.
    """

    def __init__(self, prompt_text: str = "") -> None:
        self._input_tokens = estimate_tokens_from_chars(len(prompt_text or ""))
        self._chars = 0

    def add_delta(self, delta: str | None) -> None:
        """Accumulate one streamed text delta."""
        self._chars += len(delta or "")

    @property
    def output_tokens(self) -> int:
        return estimate_text_chars_to_tokens(self._chars)

    def snapshot(self) -> dict[str, Any]:
        """Current usage object, safe to embed directly in an SSE envelope."""
        return anthropic_usage(self._input_tokens, self.output_tokens)


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

    # -----------------------------------------------------------------------
    # ANTHROPIC-FIELDS-EXPLICIT (2026-08-26): request-level fields that the
    # real Anthropic API handles explicitly must not disappear silently here.
    #
    # * ``stop_sequences`` non-empty -> HTTP 400 via RequestValidationError
    #   (the /v1/messages handler maps it to the standard
    #   ``{"type":"error","error":{"type":"invalid_request_error",...}}``
    #   envelope).  Rejecting beats silently ignoring: otherwise clients such
    #   as Claude Code believe stop sequences are in effect when they are not.
    #   An empty array is accepted as a no-op, matching real-API semantics.
    # * ``thinking`` with ``type="enabled"`` -> same 400 treatment;
    #   ``type="disabled"``/absent pass through, and any other type (current
    #   Claude Code sends ``"adaptive"`` on every request) is accepted and
    #   logged -- rejecting those would break the production client.
    # * ``metadata`` -> accept-and-ignore, but a debug log line records it so
    #   operators can see what was discarded; never raises.
    # -----------------------------------------------------------------------
    if "stop_sequences" in body and body.get("stop_sequences") is not None:
        raw_stop_sequences = body["stop_sequences"]
        if not isinstance(raw_stop_sequences, list):
            raise RequestValidationError("stop_sequences must be an array.")
        if raw_stop_sequences:
            raise RequestValidationError(
                "stop_sequences is not supported by this gateway yet; "
                "the request is rejected instead of silently ignoring it. "
                "Omit the field or pass an empty array."
            )
    thinking = body.get("thinking")
    if thinking is not None:
        if not isinstance(thinking, dict) or not isinstance(thinking.get("type"), str):
            raise RequestValidationError("thinking must be an object with a string 'type' field.")
        if thinking["type"] == "enabled":
            raise RequestValidationError(
                "extended thinking (thinking.type='enabled') is not supported "
                "by this gateway yet; set thinking.type='disabled' or omit "
                "the field."
            )
        # Current Claude Code ships ``{"type":"adaptive", ...}`` on every
        # request, so anything other than an explicit ``enabled`` stays
        # accepted -- logged at debug so the ignored request is visible
        # without breaking the production client.
        if thinking["type"] != "disabled":
            logger.debug(
                "anthropic thinking accepted-and-ignored type=%s", thinking["type"]
            )
    metadata = body.get("metadata")
    if metadata is not None:
        if isinstance(metadata, dict):
            logger.debug(
                "anthropic metadata accepted-and-ignored keys=%s",
                sorted(str(key) for key in metadata),
            )
        else:
            logger.debug(
                "anthropic metadata accepted-and-ignored type=%s", type(metadata).__name__
            )

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
        text = (
            _block_sequence_text(content)
            if role == "user"
            else _text_blocks(content, {"text"})
        )
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
                tool_message: dict[str, Any] = {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": _block_sequence_text(block.get("content"))
                    if isinstance(block.get("content"), list)
                    else content_text(block.get("content")),
                }
                # P1-4-IS-ERROR: propagate Anthropic's error marker so the
                # rendered payload tells the model the tool failed (otherwise
                # an errored result looks like success and the model may
                # declare completion on garbage output).
                if block.get("is_error"):
                    tool_message["is_error"] = True
                messages.append(tool_message)
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


def rendered_request_prompt(request: ChatCompletionRequest) -> str:
    """Fully rendered ChatGPT-Web turn text for one canonical request.

    USAGE-CONTRACT-ALIGN: single source of truth shared by ``count_tokens``
    (:func:`estimate_anthropic_input_tokens`) and every server-side
    ``usage`` input estimate -- ``render_messages(initial=True)`` over the
    canonical messages with this request's tools/tool_choice, i.e. exactly
    the scaffold-inclusive text submitted for the turn.  Estimating from raw
    message content instead made the two endpoints disagree for identical
    payloads.
    """
    return render_messages(
        request.messages,
        initial=True,
        tools=request.tools,
        tool_choice=request.tool_choice,
    )


def estimate_rendered_input_tokens(request: ChatCompletionRequest) -> int:
    """chars/4 input-token estimate over :func:`rendered_request_prompt`.

    Valid requests always report at least one input token.
    """
    return estimate_tokens_from_chars(len(rendered_request_prompt(request)))


def estimate_anthropic_input_tokens(body: Any) -> int:
    """Return a deterministic local estimate for the Anthropic token-count API.

    PARITY-P0-5 alignment: this uses the SAME formula as the ``usage`` figures
    emitted for streamed turns (see :class:`StreamUsageEstimator`) — parse the
    request, render the canonical messages exactly as the submitted turn is
    rendered (``initial=True`` bootstrap included), then ceil(chars/4).  The
    previous implementation hashed the JSON-normalized request instead, so
    count_tokens and usage.input_tokens disagreed for identical payloads and
    clients (Claude Code) trusted whichever number they saw last.  Valid
    requests always report at least one input token.
    """
    adapted = parse_anthropic_request(body)
    return estimate_rendered_input_tokens(adapted.request)


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


def response_to_anthropic(
    response: dict[str, Any], *, prompt_text: str | None = None
) -> dict[str, Any]:
    """Convert an OpenAI-shaped completion into an Anthropic message envelope.

    ``usage`` is estimated locally (chars/4) because the web backend exposes
    no tokenizer; see the PARITY-P0-1 comment block above.  ``prompt_text``
    is the fully rendered prompt submitted for this turn; when supplied it
    feeds the input_tokens estimate.  ``None`` means the caller could not
    observe the prompt and reports 0; a known-empty prompt (``""``) floors at
    one token exactly like the streaming estimator.
    """
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
    # Output tokens: full generated text (plus serialized tool arguments,
    # which a real tokenizer would also charge as output) divided by four.
    output_tokens = estimate_text_chars_to_tokens(_response_output_chars(message))
    # Floor-to-one rule, matching StreamUsageEstimator: distinguish an
    # unobserved prompt (None -> 0) from a known-empty rendered prompt
    # ("" still floors at one token via estimate_tokens_from_chars).
    if prompt_text is None:
        input_tokens = 0
    else:
        input_tokens = estimate_tokens_from_chars(len(prompt_text))
    return {
        "id": f"msg_{uuid.uuid4().hex[:16]}",
        "type": "message",
        "role": "assistant",
        "model": response["model"],
        "content": content,
        "stop_reason": "tool_use" if message.get("tool_calls") else "end_turn",
        "stop_sequence": None,
        "usage": anthropic_usage(input_tokens, output_tokens),
    }

from __future__ import annotations

import json
import os
import re
from typing import Any

from gpt.state import ProtocolChanged

COMPLETION_STATUSES = frozenset({"finished_successfully", "finished", "complete"})
V1_PARTS_PATH = "/message/content/parts/0"
V1_STATUS_PATH = "/message/status"
DEDUPE_FLAG = "WEBGPT_STREAM_DEDUPE"
STRIP_PREFIX_FLAG = "WEBGPT_STREAM_STRIP_PREFIX"
FINAL_CHANNEL = "final"

_NOISE_PREFIX_PATTERNS = (
    re.compile(r"^Thinking[^\S\n]*\r?\n"),
    re.compile(r"^Thinking\Z"),
    re.compile(r"^Thought[^\S\n]*:[^\S\n]*"),
)
_NOISE_PREFIX_WORDS = ("Thinking", "Thought")


def flag_enabled(name: str) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return True
    return raw.strip().casefold() in {"1", "true", "yes", "on"}


def collapse_duplicate(new_text: str, old_text: str) -> str:
    if not flag_enabled(DEDUPE_FLAG):
        return new_text
    if not old_text or not new_text.startswith(old_text):
        return new_text
    extra = new_text[len(old_text) :]
    if len(extra) <= 0.9 * len(old_text):
        return new_text
    trimmed = extra
    while trimmed.startswith(old_text):
        trimmed = trimmed[len(old_text) :]
    return old_text + trimmed if trimmed != extra else new_text


def merge_candidate(text: str, candidate: str) -> tuple[str, str]:
    if candidate.startswith(text):
        merged = collapse_duplicate(candidate, text)
    elif not text.startswith(candidate):
        merged = collapse_duplicate(text + candidate, text)
    else:
        return text, ""
    delta = merged[len(text) :] if len(merged) > len(text) else ""
    return merged, delta


def strip_leading_noise(text: str) -> tuple[str, bool]:
    for pattern in _NOISE_PREFIX_PATTERNS:
        match = pattern.match(text)
        if match:
            return text[match.end() :], True
    if any(word.startswith(text) for word in _NOISE_PREFIX_WORDS):
        return text, False
    return text, True


def consume_record(
    record: str,
    text: str,
    turn_id: str,
    conversation_id: str | None,
    model: str | None,
    capture: dict[str, str] | None = None,
) -> tuple[str, str, str | None, str | None, bool, str]:
    if record == "[DONE]":
        return text, turn_id, conversation_id, model, True, ""
    try:
        payload = json.loads(record)
    except json.JSONDecodeError as exc:
        raise ProtocolChanged("Conversation SSE contained invalid JSON.") from exc
    if not isinstance(payload, dict):
        return text, turn_id, conversation_id, model, False, ""
    if payload.get("error"):
        raise ProtocolChanged("ChatGPT returned an error event in the conversation stream.")
    if "message" not in payload and (payload.get("type") or "v" in payload):
        return consume_v1_record(
            payload,
            text,
            turn_id,
            conversation_id,
            model,
            capture=capture,
        )

    value = payload.get("conversation_id")
    if isinstance(value, str):
        conversation_id = value
    message = payload.get("message")
    delta = ""
    if isinstance(message, dict):
        metadata = message.get("metadata")
        if isinstance(metadata, dict):
            value = metadata.get("model_slug") or metadata.get("resolved_model_slug")
            if isinstance(value, str):
                model = value
        author = message.get("author")
        role = author.get("role") if isinstance(author, dict) else None
        if role is not None and role != "assistant":
            return text, turn_id, conversation_id, model, False, ""

        value = message.get("id")
        if isinstance(value, str):
            turn_id = value
        content = message.get("content")
        if isinstance(content, dict):
            parts = content.get("parts")
            if isinstance(parts, list):
                candidate = "".join(part for part in parts if isinstance(part, str))
                text, delta = merge_candidate(text, candidate)
        complete = message.get("status") in COMPLETION_STATUSES
    else:
        complete = payload.get("status") in COMPLETION_STATUSES
    return text, turn_id, conversation_id, model, complete, delta


def consume_v1_record(
    payload: dict[str, Any],
    text: str,
    turn_id: str,
    conversation_id: str | None,
    model: str | None,
    capture: dict[str, str] | None = None,
) -> tuple[str, str, str | None, str | None, bool, str]:
    record_type = payload.get("type")
    if record_type == "message_stream_complete":
        value = payload.get("conversation_id")
        if isinstance(value, str):
            conversation_id = value
        return text, turn_id, conversation_id, model, True, ""
    if record_type == "server_ste_metadata":
        metadata = payload.get("metadata")
        if isinstance(metadata, dict):
            slug = metadata.get("model_slug")
            if isinstance(slug, str):
                model = slug
        value = payload.get("conversation_id")
        if isinstance(value, str):
            conversation_id = value
        return text, turn_id, conversation_id, model, False, ""
    if record_type == "input_message":
        value = payload.get("conversation_id")
        if isinstance(value, str):
            conversation_id = value
        return text, turn_id, conversation_id, model, False, ""
    if record_type == "resume_conversation_token":
        if capture is not None:
            value = payload.get("token")
            if isinstance(value, str) and value:
                capture["token"] = value
                resume_conversation = payload.get("conversation_id")
                if isinstance(resume_conversation, str) and resume_conversation:
                    capture["conversation_id"] = resume_conversation
        return text, turn_id, conversation_id, model, False, ""
    if record_type in {
        "delta_encoding",
        "message_marker",
        "conversation_detail_metadata",
    }:
        return text, turn_id, conversation_id, model, False, ""

    op = payload.get("o")
    path = payload.get("p") or ""
    target = payload.get("v")

    if isinstance(target, str):
        if path in ("", V1_PARTS_PATH) and op in (None, "append"):
            merged, delta = merge_candidate(text, text + target)
            return merged, turn_id, conversation_id, model, False, delta
        return text, turn_id, conversation_id, model, False, ""

    if isinstance(target, dict) and op in (None, "add"):
        return consume_v1_message(
            target.get("message"),
            text,
            turn_id,
            conversation_id,
            model,
        )

    if isinstance(target, list) and op == "patch":
        delta_total = ""
        complete = False
        for item in target:
            if not isinstance(item, dict):
                continue
            item_path = item.get("p") or ""
            item_op = item.get("o")
            item_value = item.get("v")
            if (
                item_path == V1_PARTS_PATH
                and item_op == "append"
                and isinstance(item_value, str)
            ):
                text, merged_delta = merge_candidate(text, text + item_value)
                delta_total += merged_delta
            elif (
                item_path == V1_STATUS_PATH
                and item_op == "replace"
                and item_value in COMPLETION_STATUSES
            ):
                complete = True
        return text, turn_id, conversation_id, model, complete, delta_total

    return text, turn_id, conversation_id, model, False, ""


def consume_v1_message(
    message: Any,
    text: str,
    turn_id: str,
    conversation_id: str | None,
    model: str | None,
) -> tuple[str, str, str | None, str | None, bool, str]:
    if not isinstance(message, dict):
        return text, turn_id, conversation_id, model, False, ""
    author = message.get("author")
    role = author.get("role") if isinstance(author, dict) else None
    value = message.get("id")
    if isinstance(value, str):
        turn_id = value
    metadata = message.get("metadata")
    if isinstance(metadata, dict):
        slug = metadata.get("model_slug") or metadata.get("resolved_model_slug")
        if isinstance(slug, str):
            model = slug
    value = message.get("conversation_id")
    if isinstance(value, str):
        conversation_id = value
    is_assistant = role == "assistant"
    channel = message.get("channel")
    if channel is not None and channel != FINAL_CHANNEL:
        return text, turn_id, conversation_id, model, False, ""

    complete = False
    delta = ""
    if is_assistant:
        status = message.get("status")
        complete = status in COMPLETION_STATUSES
        content = message.get("content")
        if isinstance(content, dict):
            parts = content.get("parts")
            if isinstance(parts, list):
                candidate = "".join(part for part in parts if isinstance(part, str))
                text, delta = merge_candidate(text, candidate)
    return text, turn_id, conversation_id, model, complete, delta


__all__ = [
    "COMPLETION_STATUSES",
    "DEDUPE_FLAG",
    "FINAL_CHANNEL",
    "STRIP_PREFIX_FLAG",
    "V1_PARTS_PATH",
    "V1_STATUS_PATH",
    "collapse_duplicate",
    "consume_record",
    "consume_v1_message",
    "consume_v1_record",
    "flag_enabled",
    "merge_candidate",
    "strip_leading_noise",
]

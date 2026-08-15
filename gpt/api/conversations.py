from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from gpt.api.messages import canonical_messages


def request_fingerprint(
    messages: list[dict[str, Any]],
    model: str,
    tools: list[dict[str, Any]],
    tool_choice: Any = "auto",
) -> str:
    raw = json.dumps(
        {
            "messages": messages,
            "model": model,
            "tools": tools,
            "tool_choice": tool_choice,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def tool_signature(tools: list[dict[str, Any]]) -> str:
    raw = json.dumps(tools, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


@dataclass
class ConversationRecord:
    session_id: str = field(default_factory=lambda: f"wgs_{uuid.uuid4().hex[:16]}")
    conversation_id: str | None = None
    model: str = "chatgpt-web"
    tool_signature: str = ""
    messages: list[dict[str, Any]] = field(default_factory=list)
    last_request_fingerprint: str | None = None
    last_response: dict[str, Any] | None = None
    last_used: float = field(default_factory=time.monotonic)


class ConversationStore:
    """Correlates standard full-message requests with a persisted web conversation."""

    def __init__(self, max_sessions: int = 64):
        self.max_sessions = max_sessions
        self._records: dict[str, ConversationRecord] = {}

    def resolve(
        self,
        messages: list[dict[str, Any]],
        model: str,
        tools: list[dict[str, Any]],
        explicit_id: str | None = None,
        tool_choice: Any = "auto",
    ) -> tuple[ConversationRecord, list[dict[str, Any]], bool]:
        canonical = canonical_messages(messages)
        fingerprint = request_fingerprint(canonical, model, tools, tool_choice)
        signature = tool_signature(tools)
        if explicit_id:
            record = self._records.get(explicit_id)
            if record is None:
                raise KeyError(explicit_id)
            if record.last_request_fingerprint == fingerprint and record.last_response:
                return record, [], True
            if record.tool_signature != signature:
                raise ValueError("Tool definitions changed within a gateway session.")
            if not self._is_prefix(record.messages, canonical):
                raise ValueError("Request messages diverge from the selected gateway session.")
            return record, canonical[len(record.messages) :], False

        candidates = [
            record
            for record in self._records.values()
            if record.model == model
            and record.tool_signature == signature
            and self._is_prefix(record.messages, canonical)
        ]
        if candidates:
            record = max(candidates, key=lambda item: len(item.messages))
            if record.last_request_fingerprint == fingerprint and record.last_response:
                return record, [], True
            return record, canonical[len(record.messages) :], False
        self._evict_if_needed()
        record = ConversationRecord(model=model, tool_signature=signature)
        self._records[record.session_id] = record
        return record, canonical, False

    def commit(
        self,
        record: ConversationRecord,
        request_messages: list[dict[str, Any]],
        assistant_message: dict[str, Any],
        response: dict[str, Any],
        model: str,
        tools: list[dict[str, Any]],
        conversation_id: str | None,
        tool_choice: Any = "auto",
    ) -> None:
        canonical = canonical_messages(request_messages)
        record.messages = canonical + canonical_messages([assistant_message])
        record.last_request_fingerprint = request_fingerprint(
            canonical, model, tools, tool_choice
        )
        record.last_response = response
        record.conversation_id = conversation_id or record.conversation_id
        record.last_used = time.monotonic()

    def get(self, session_id: str) -> ConversationRecord | None:
        return self._records.get(session_id)

    def __len__(self) -> int:
        return len(self._records)

    @staticmethod
    def _is_prefix(prefix: list[dict[str, Any]], value: list[dict[str, Any]]) -> bool:
        return len(prefix) <= len(value) and prefix == value[: len(prefix)]

    def _evict_if_needed(self) -> None:
        if len(self._records) < self.max_sessions:
            return
        oldest = min(self._records.values(), key=lambda item: item.last_used)
        del self._records[oldest.session_id]

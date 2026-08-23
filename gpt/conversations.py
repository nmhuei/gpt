from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from gpt.promptcompat import canonical_messages
from gpt.state import ConversationConflict

try:  # POSIX advisory lock; local persistence remains best-effort elsewhere.
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None  # type: ignore[assignment]


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
    tools: list[dict[str, Any]] = field(default_factory=list)
    delivered_tool_result_ids: list[str] = field(default_factory=list)
    pending_request_fingerprint: str | None = None
    pending_prompt: str | None = None
    pending_submitted_at: float | None = None
    web_bootstrapped: bool = False
    last_used: float = field(default_factory=time.time)


class ConversationStore:
    """Correlates standard full-message requests with a persisted web conversation."""

    def __init__(
        self,
        max_sessions: int = 64,
        state_path: Path | str | None = None,
        ttl_seconds: float = 86_400,
    ):
        self.max_sessions = max_sessions
        self.ttl_seconds = ttl_seconds
        self.state_path = Path(state_path).expanduser() if state_path else None
        self._records: dict[str, ConversationRecord] = {}
        self._load()

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
                raise ConversationConflict("Tool definitions changed within a gateway session.")
            if not self._is_prefix(record.messages, canonical):
                raise ConversationConflict(
                    "Request messages diverge from the selected gateway session."
                )
            return record, canonical[len(record.messages) :], False

        candidates = [
            record
            for record in self._records.values()
            if record.model == model
            and record.tool_signature == signature
            and self._is_prefix(record.messages, canonical)
            and (
                bool(record.messages)
                or record.pending_request_fingerprint == fingerprint
            )
        ]
        if candidates:
            record = max(candidates, key=lambda item: len(item.messages))
            if record.last_request_fingerprint == fingerprint and record.last_response:
                return record, [], True
            return record, canonical[len(record.messages) :], False
        self._evict_if_needed()
        record = ConversationRecord(
            model=model,
            tool_signature=signature,
            tools=canonical_messages(tools),
        )
        self._records[record.session_id] = record
        self._persist()
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
        record.tools = canonical_messages(tools)
        record.delivered_tool_result_ids = sorted(
            {
                str(message["tool_call_id"])
                for message in canonical
                if message.get("role") == "tool"
                and isinstance(message.get("tool_call_id"), str)
            }
        )
        record.pending_request_fingerprint = None
        record.pending_prompt = None
        record.pending_submitted_at = None
        record.last_used = time.time()
        self._persist()

    def mark_pending(
        self,
        record: ConversationRecord,
        *,
        messages: list[dict[str, Any]],
        model: str,
        tools: list[dict[str, Any]],
        tool_choice: Any,
        prompt: str,
    ) -> str:
        fingerprint = request_fingerprint(
            canonical_messages(messages), model, tools, tool_choice
        )
        record.pending_request_fingerprint = fingerprint
        record.pending_prompt = prompt
        record.pending_submitted_at = time.time()
        record.last_used = time.time()
        self._persist()
        return fingerprint

    def clear_pending(self, record: ConversationRecord) -> None:
        record.pending_request_fingerprint = None
        record.pending_prompt = None
        record.pending_submitted_at = None
        record.last_used = time.time()
        self._persist()

    def pending_matches(
        self,
        record: ConversationRecord,
        messages: list[dict[str, Any]],
        model: str,
        tools: list[dict[str, Any]],
        tool_choice: Any,
    ) -> bool:
        if record.pending_request_fingerprint is None:
            return False
        fingerprint = request_fingerprint(
            canonical_messages(messages), model, tools, tool_choice
        )
        return fingerprint == record.pending_request_fingerprint

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
        self._persist()

    def _load(self) -> None:
        if self.state_path is None or not self.state_path.is_file():
            return
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
            if payload.get("version") != 1:
                return
            records = payload.get("records", [])
            if not isinstance(records, list):
                return
            now = time.time()
            for raw in records:
                if not isinstance(raw, dict):
                    continue
                raw = dict(raw)
                saved_at = raw.pop("saved_at", 0)
                if not isinstance(saved_at, (int, float)) or now - saved_at > self.ttl_seconds:
                    continue
                required = {"session_id", "model", "tool_signature", "messages"}
                if not required.issubset(raw):
                    continue
                if not isinstance(raw["session_id"], str) or not isinstance(raw["model"], str):
                    continue
                if not isinstance(raw["tool_signature"], str) or not isinstance(raw["messages"], list):
                    continue
                raw.setdefault("tools", [])
                raw.setdefault("delivered_tool_result_ids", [])
                raw.setdefault("web_bootstrapped", False)
                raw["last_used"] = saved_at
                record = ConversationRecord(**raw)
                self._records[record.session_id] = record
            self._evict_if_needed()
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            # A gateway store is a best-effort local cache; a malformed file
            # must not prevent the browser backend from starting.
            self._records = {}

    def _persist(self) -> None:
        if self.state_path is None:
            return
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            os.chmod(self.state_path.parent, 0o700)
            with self._write_lock():
                payload = {
                    "version": 1,
                    "records": [
                        {
                            "session_id": record.session_id,
                            "conversation_id": record.conversation_id,
                            "model": record.model,
                            "tool_signature": record.tool_signature,
                            "tools": record.tools,
                            "delivered_tool_result_ids": record.delivered_tool_result_ids,
                            "messages": record.messages,
                            "last_request_fingerprint": record.last_request_fingerprint,
                            "last_response": record.last_response,
                            "pending_request_fingerprint": record.pending_request_fingerprint,
                            "pending_prompt": record.pending_prompt,
                            "pending_submitted_at": record.pending_submitted_at,
                            "last_used": record.last_used,
                            "saved_at": time.time(),
                        }
                        for record in self._records.values()
                    ],
                }
                temporary = self.state_path.with_suffix(
                    f"{self.state_path.suffix}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
                )
                temporary.write_text(
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                    encoding="utf-8",
                )
                os.chmod(temporary, 0o600)
                temporary.replace(self.state_path)
            os.chmod(self.state_path, 0o600)
        except OSError:
            # Persistence is opt-in and must never break an active chat turn.
            return

    @contextmanager
    def _write_lock(self):
        if self.state_path is None or fcntl is None:
            yield
            return
        lock_path = self.state_path.with_suffix(f"{self.state_path.suffix}.lock")
        with lock_path.open("a", encoding="utf-8") as lock_file:
            os.chmod(lock_path, 0o600)
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import threading
import time
import uuid
from collections import OrderedDict
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


def _sync_persist_forced() -> bool:
    """True when WEBGPT_SYNC_PERSIST=1 forces the legacy in-loop persist."""
    return os.environ.get("WEBGPT_SYNC_PERSIST", "").strip().lower() in {"1", "true", "yes", "on"}


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


# Bounded memo for the canonical-messages + request-fingerprint pair.  Keyed by
# (message count, content hash of the raw messages, model, tool signature,
# tool-choice repr) so any change in inputs misses the cache.  Cache hits return
# a deep copy of the canonical list, making this bit-for-bit equivalent to
# recomputing while skipping re-canonicalization of unchanged histories.
# The LRU bound is env-tunable (WEBGPT_CANONICAL_MEMO_MAX): each entry holds a
# full canonical transcript copy, so 256 entries can pin tens of MB on long
# claude-code histories; operators of small-RAM hosts can lower the cap.
DEFAULT_CANONICAL_MEMO_MAX = 256


def _resolve_canonical_memo_max() -> int:
    """Resolve ``WEBGPT_CANONICAL_MEMO_MAX``; invalid values keep default."""
    raw = os.environ.get("WEBGPT_CANONICAL_MEMO_MAX", "").strip()
    if not raw:
        return DEFAULT_CANONICAL_MEMO_MAX
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_CANONICAL_MEMO_MAX
    return value if value >= 1 else DEFAULT_CANONICAL_MEMO_MAX


_CANONICAL_MEMO_MAX = _resolve_canonical_memo_max()
_canonical_memo: OrderedDict[tuple[Any, ...], tuple[list[dict[str, Any]], str]] = OrderedDict()


def _canonical_with_fingerprint(
    messages: list[dict[str, Any]],
    model: str,
    tools: list[dict[str, Any]],
    signature: str,
    tool_choice: Any = "auto",
) -> tuple[list[dict[str, Any]], str]:
    """Return (canonical_messages, fingerprint), memoized per input content."""
    try:
        raw = json.dumps(messages, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        # Unserializable payload: fall back to the plain computation.
        canonical = canonical_messages(messages)
        return canonical, request_fingerprint(canonical, model, tools, tool_choice)
    key = (
        len(messages),
        hashlib.sha1(raw.encode()).hexdigest(),
        model,
        signature,
        repr(tool_choice),
    )
    hit = _canonical_memo.get(key)
    if hit is not None:
        canonical, fingerprint = hit
        _canonical_memo.move_to_end(key)
        return copy.deepcopy(canonical), fingerprint
    canonical = canonical_messages(messages)
    fingerprint = request_fingerprint(canonical, model, tools, tool_choice)
    if len(_canonical_memo) >= _CANONICAL_MEMO_MAX:
        _canonical_memo.popitem(last=False)
    _canonical_memo[key] = (canonical, fingerprint)
    return canonical, fingerprint


@dataclass
class ConversationRecord:
    session_id: str = field(default_factory=lambda: f"wgs_{uuid.uuid4().hex[:16]}")
    conversation_id: str | None = None
    account_name: str | None = None
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
        # Background-flush coordination (see _request_persist / _drain_dirty).
        self._persist_lock = threading.Lock()
        self._dirty = False
        self._flush_active = False
        self._load()

    def resolve(
        self,
        messages: list[dict[str, Any]],
        model: str,
        tools: list[dict[str, Any]],
        explicit_id: str | None = None,
        tool_choice: Any = "auto",
    ) -> tuple[ConversationRecord, list[dict[str, Any]], bool]:
        signature = tool_signature(tools)
        canonical, fingerprint = _canonical_with_fingerprint(
            messages, model, tools, signature, tool_choice
        )
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

        # TTL-based eviction: drop records whose last_used is older than the
        # configured TTL even when no new record needs to be created.
        self._evict_expired()
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
        self._request_persist()
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
        canonical, fingerprint = _canonical_with_fingerprint(
            request_messages, model, tools, tool_signature(tools), tool_choice
        )
        record.messages = canonical + canonical_messages([assistant_message])
        record.last_request_fingerprint = fingerprint
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
        self._evict_expired()
        self._request_persist()

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
        fingerprint = _canonical_with_fingerprint(
            messages, model, tools, tool_signature(tools), tool_choice
        )[1]
        record.pending_request_fingerprint = fingerprint
        record.pending_prompt = prompt
        record.pending_submitted_at = time.time()
        record.last_used = time.time()
        # Durability-first: the two-phase crash-recovery design requires the
        # pending marker to be ON DISK before the request is submitted to the
        # web backend, otherwise a crash in flight would lose the
        # duplicate-submission guard.  This is the only mutation that keeps a
        # synchronous write (once per turn, unlike the hot commit path).
        self._persist()
        return fingerprint

    def clear_pending(self, record: ConversationRecord) -> None:
        record.pending_request_fingerprint = None
        record.pending_prompt = None
        record.pending_submitted_at = None
        record.last_used = time.time()
        self._request_persist()

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
        fingerprint = _canonical_with_fingerprint(
            messages, model, tools, tool_signature(tools), tool_choice
        )[1]
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
        self._request_persist()

    def _evict_expired(self) -> int:
        """Drop records whose last_used exceeded the TTL; returns the count."""
        now = time.time()
        expired = [
            session_id
            for session_id, record in self._records.items()
            if record.last_used is not None and now - record.last_used > self.ttl_seconds
        ]
        for session_id in expired:
            del self._records[session_id]
        if expired:
            self._request_persist()
        return len(expired)

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
        """Synchronous durable write; caller blocks until the state file is replaced."""
        if self.state_path is None:
            return
        with self._persist_lock:
            self._write_state_locked()

    def close(self) -> None:
        """Shutdown hook: flush everything synchronously before process exit.

        Background flushes are daemon threads that would die with the process,
        so callers must invoke this (or ``persist_async``) on the shutdown path
        to guarantee the latest dirty state reaches disk.
        """
        self._dirty = False
        self._persist()

    def _request_persist(self) -> None:
        """Mark the store dirty and persist without blocking a running loop.

        Default behavior inside an asyncio loop: set ``_dirty`` and spawn a
        coalescing background flush if none is in flight (a single daemon
        worker drains the flag in a loop, so bursts of mutations produce at
        most one write each time the worker re-checks).  With no running loop
        (sync callers/tests) or with ``WEBGPT_SYNC_PERSIST=1`` the legacy
        synchronous write is kept.

        Data-safety note: the store is fail-open best-effort cache.  If the
        process dies between a mutation and its background flush, at most one
        flush interval of state is lost — except for ``mark_pending``, which
        always writes synchronously to preserve the two-phase crash-recovery
        guarantee.
        """
        if self.state_path is None:
            return
        if _sync_persist_forced():
            self._persist()
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            # No event loop: keep the legacy direct write so sync callers and
            # existing tests observe persisted state immediately.
            self._persist()
            return
        with self._persist_lock:
            self._dirty = True
            if self._flush_active:
                return  # In-flight worker will pick up the dirty flag.
            self._flush_active = True
        threading.Thread(
            target=self._drain_dirty, name="webgpt-store-flush", daemon=True
        ).start()

    def _drain_dirty(self) -> None:
        """Worker body: write while dirty, then release the flush slot.

        Runs off the event-loop thread.  The dirty check-and-clear happens
        under ``_persist_lock`` together with the slot release, so a mutation
        landing between the last write and worker exit either sees the active
        slot (and relies on this loop) or respawns a fresh worker.
        """
        while True:
            with self._persist_lock:
                if not self._dirty:
                    self._flush_active = False
                    return
                self._dirty = False
                self._write_state_locked()

    def _write_state_locked(self) -> None:
        """Serialize records to disk atomically. Caller must hold _persist_lock."""
        state_path = self.state_path
        if state_path is None:
            return
        try:
            state_path.parent.mkdir(parents=True, exist_ok=True)
            os.chmod(state_path.parent, 0o700)
            with self._write_lock():
                payload = {
                    "version": 1,
                    "records": [
                        {
                            "session_id": record.session_id,
                            "conversation_id": record.conversation_id,
                            "account_name": record.account_name,
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
                            "web_bootstrapped": record.web_bootstrapped,
                            "last_used": record.last_used,
                            "saved_at": time.time(),
                        }
                        for record in self._records.values()
                    ],
                }
                temporary = state_path.with_suffix(
                    f"{state_path.suffix}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
                )
                temporary.write_text(
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                    encoding="utf-8",
                )
                os.chmod(temporary, 0o600)
                temporary.replace(state_path)
            os.chmod(state_path, 0o600)
        except OSError:
            # Persistence is opt-in and must never break an active chat turn.
            return

    async def persist_async(self) -> None:
        """Async persistence path for callers inside a running event loop.

        Wraps the synchronous ``_persist`` in ``asyncio.to_thread`` so the event
        loop is not blocked by disk I/O. ``WEBGPT_SYNC_PERSIST=1`` forces the
        old in-loop synchronous behavior (also used for shutdown paths, where a
        thread would race process exit).
        """
        if self.state_path is None or _sync_persist_forced():
            self._persist()
            return
        await asyncio.to_thread(self._persist)

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

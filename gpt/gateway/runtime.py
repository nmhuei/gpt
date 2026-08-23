from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import os
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gpt.assistantturn import AssistantTurn, AssistantTurnBuilder
from gpt.conversations import ConversationRecord, ConversationStore
from gpt.promptcompat import compact_messages, message_content_chars, render_messages
from gpt.reverse.redact import default_redactor
from gpt.state import (
    CommitUnknown,
    ConversationNotFound,
    EmptyModelResponse,
    MalformedToolCall,
)
from gpt.toolcall import ToolTranspiler
from gpt.tracing import RuntimeTraceBus
from gpt.transport.session import ChatGPTWebSession
from gpt.types import ResponseDelta, StateChanged, TurnResult

SessionLeaseFactory = Callable[[], AbstractAsyncContextManager[ChatGPTWebSession]]


def _looks_like_tool_refusal(text: str, tools: list[dict[str, Any]]) -> bool:
    if not tools or not isinstance(text, str):
        return False
    lowered = text.casefold().replace("\u2019", "'").replace("\u2018", "'")
    tool_names = {
        str(tool.get("function", {}).get("name", "")).casefold()
        for tool in tools
        if isinstance(tool.get("function"), dict)
    }
    tool_names.discard("")
    if tool_names and "not exposed" in lowered and any(name in lowered for name in tool_names):
        return True
    if tool_names and "not exposed" in lowered and any(
        marker in lowered for marker in ("filesystem", "controller", "executable tool", "callable tool")
    ):
        return True
    if tool_names and "fabricat" in lowered and "tool" in lowered:
        return True
    if tool_names and "truthfully" in lowered and any(name in lowered for name in tool_names):
        return True
    refusal_markers = (
        "can't run",

        "cannot run",
        "can't directly run",
        "cannot directly run",
        "can't execute",
        "cannot execute",
        "can't directly execute",
        "cannot directly execute",
        "can't directly create",
        "cannot directly create",
        "can't create files",
        "cannot create files",
        "can't directly modify",
        "cannot directly modify",
        "can't modify files",
        "cannot modify files",
        "can't directly write",
        "cannot directly write",
        "can't write files",
        "cannot write files",
        "can't write to",
        "cannot write to",
        "unable to create",
        "unable to execute",
        "unable to run",
        "unable to modify",
        "unable to write",
        "unable to call tools",
        "don't have access to that shell",
        "do not have access to that shell",
        "don't have access to your environment",
        "do not have access to your environment",
        "don't have access to your system",
        "do not have access to your system",
        "don't have filesystem access",
        "do not have filesystem access",
        "cannot access your filesystem",
        "can't access your filesystem",
        "cannot interact with your local",
        "can't interact with your local",
        "from this chat session",
        "in your environment from this chat",
        "from this chat",
        "no access to the shell",
        "no shell",
        "no filesystem",
        "filesystem/bash/read/edit tools",
        "not actually exposed",
        "not exposed as an executable tool",
        "not exposed to this chatgpt turn",
        "tools are unavailable",
        "function is unavailable",
        "i can't complete the filesystem",
        "i cannot complete the filesystem",
        "cannot truthfully claim that i created files",
        "cannot truthfully claim that files were created",
        "without fabricating tool execution",
        "cannot provide the requested completion summary",
        "actual claude code bash/read/edit filesystem tools",
        "tool surface exposed to this conversation does not provide",
        "does not provide the requested local filesystem",
        "does not provide the requested local filesystem/shell operations",
        "write, edit, bash, or an equivalent",
        "write, edit, and bash operations",
        "not exposed to me here",
        "i'm unable to complete the requested filesystem implementation",
        "unable to complete the requested filesystem implementation",
        "filesystem write, edit, and bash operations needed",
        "i therefore did not fabricate file changes",
        "i won't fabricate tool results",
    )
    return any(marker in lowered for marker in refusal_markers)





def _message_content_text(message: dict[str, Any]) -> str:
    value = message.get("content")
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(
            str(item.get("text", ""))
            for item in value
            if isinstance(item, dict) and isinstance(item.get("text"), str)
        )
    return "" if value is None else str(value)


def _has_parseable_tool_call(text: str, tools: list[dict[str, Any]]) -> bool:
    if not tools:
        return False
    try:
        _, calls = ToolTranspiler.parse_tool_calls(
            text,
            allowed_tools=set(ToolTranspiler.validate_tools(tools)),
            tool_definitions=tools,
        )
        return bool(calls)
    except Exception:
        # Let the normal AssistantTurnBuilder path surface malformed tool blocks.
        return True


def _parseable_tool_call_count(text: str, tools: list[dict[str, Any]]) -> int:
    if not tools:
        return 0
    try:
        _, calls = ToolTranspiler.parse_tool_calls(
            text,
            allowed_tools=set(ToolTranspiler.validate_tools(tools)),
            tool_definitions=tools,
        )
        return len(calls)
    except Exception:
        return 0


def _looks_like_tool_directed_task(
    tail: list[dict[str, Any]],
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
) -> bool:
    if not tools:
        return False
    names = {tool.get("function", {}).get("name") for tool in tools if isinstance(tool.get("function"), dict)}
    names = {str(name).casefold() for name in names if name}
    last_user_text = next(
        (
            _message_content_text(message).casefold()
            for message in reversed(messages)
            if message.get("role") == "user" and _message_content_text(message).strip()
        ),
        "",
    )
    last_user_text = re.sub(r"<system-reminder>.*?</system-reminder>", "", last_user_text, flags=re.DOTALL).strip()
    if not last_user_text:
        return False
    called_names: set[str] = set()
    for message in messages:
        if message.get("role") != "assistant":
            continue
        for call in message.get("tool_calls") or []:
            function = call.get("function") if isinstance(call, dict) else None
            if isinstance(function, dict) and isinstance(function.get("name"), str):
                called_names.add(function["name"].casefold())
    explicitly_requested: set[str] = set()
    for name in names:
        if (
            f"use the {name} tool" in last_user_text
            or f"use {name} tool" in last_user_text
            or f"call {name}" in last_user_text
            or f"use {name} " in last_user_text
            or last_user_text.startswith(f"{name} ")
        ):
            explicitly_requested.add(name)
    if explicitly_requested - called_names:
        return True
    post_read_work_markers = (
        "create ",
        "write ",
        "implement",
        "run python",
        "compileall",
        "pytest",
        "run tests",
        "test suite",
        "source files",
        "project",
    )
    if (
        any(message.get("role") == "tool" for message in tail)
        and called_names <= {"read"}
        and any(marker in last_user_text for marker in post_read_work_markers)
        and bool(names & {"bash", "edit", "write"})
    ):
        return True
    # After an actual tool result, a final text answer is often correct, provided
    # every explicitly requested tool has already been called.  Refusal text is
    # handled separately by _looks_like_tool_refusal.
    if any(message.get("role") == "tool" for message in tail):
        return False
    tool_task_markers = (
        "implement",
        "create the project",
        "create a file",
        "create file",
        "create files",
        "create ",
        "write a file",
        "write file",
        "write files",
        "write ",
        "modify files",
        "modify file",
        "modify ",
        "edit files",
        "edit file",
        "edit ",
        "run pytest",
        "run tests",
        "run python",
        "run ",
        "read spec",
        "first read spec",
        "use write/edit/bash",
        "use write",
        "use bash",
        "use edit",
    )
    return any(marker in last_user_text for marker in tool_task_markers) and bool(names & {"bash", "write", "edit", "read"})


def _tool_choice_requires_call(tool_choice: Any) -> bool:
    return tool_choice == "required" or isinstance(tool_choice, dict)


def _tool_correction_issue(
    text: str,
    *,
    tail: list[dict[str, Any]],
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    tool_choice: Any,
) -> tuple[str, str] | None:
    if not tools or tool_choice == "none":
        return None
    try:
        _, calls = ToolTranspiler.parse_tool_calls(
            text,
            allowed_tools=set(ToolTranspiler.validate_tools(tools)),
            tool_definitions=tools,
        )
    except Exception as exc:
        detail = str(exc)
        upper = detail.upper()
        reason = (
            "INVALID_WRITE"
            if "WRITE" in upper or "PYTHON" in upper or "JSON" in upper or "TOML" in upper
            else "MALFORMED_TOOL"
        )
        return reason, detail
    if len(calls) > 1:
        return "MULTI_TOOL", f"model returned {len(calls)} tool calls; exactly one is allowed"
    if _looks_like_tool_refusal(text, tools):
        return "TOOL_REFUSAL", "model denied access to controller-provided tools"
    if not calls and _tool_choice_requires_call(tool_choice):
        return "MISSING_REQUIRED_TOOL", "tool_choice requires a tool call"
    if not calls and _looks_like_tool_directed_task(tail, messages, tools):
        return "FALSE_COMPLETION", "task requires a controller tool but model returned only prose"
    return None


def _controller_tool_correction_prompt(
    tools: list[dict[str, Any]],
    tool_choice: Any,
    *,
    reason: str,
    detail: str,
) -> str:
    available = ToolTranspiler.validate_tools(tools)
    shell_name = "Bash" if "Bash" in available else "bash" if "bash" in available else "shell"
    return (
        "WEBGPT CONTROLLER CORRECTION:\n"
        f"Correction reason: {reason}.\n"
        f"Validation detail: {detail[:600]}.\n"
        "The listed tools are real external controller functions executed by Claude Code/controller. "
        "You do not execute them directly; you request them by outputting the tool block.\n\n"
        "Return ONLY one valid tool call block containing exactly one invoke for the current user task. "
        "Do not apologize, explain, or answer in prose. Choose the single next action only. "
        f"For shell commands use {shell_name}.command. For creating files use Write.file_path plus Write.lines for source code. "
        "For modifying files use an advertised edit tool. For reading files use an advertised read tool.\n\n"
        + ToolTranspiler.build_tool_instructions(tools, tool_choice)
    )


@dataclass(frozen=True)
class CompletionExecution:
    turn: AssistantTurn
    web_result: TurnResult
    prompt: str
    reconciled: bool = False


class CompletionRuntime:
    """Protocol-neutral transaction runtime for one ChatGPT Web completion.

    HTTP adapters resolve their external request shape before entering this
    runtime. This layer owns browser positioning, prompt rendering, pending-send
    durability and uncertain-commit reconciliation; it never emits OpenAI or
    Anthropic response JSON.
    """

    def __init__(
        self,
        conversations: ConversationStore,
        lease_session: SessionLeaseFactory,
        trace: RuntimeTraceBus | None = None,
        prompt_debug_dir: str | Path | None = None,
        generation_timeout_seconds: float = 120.0,
    ) -> None:
        self.conversations = conversations
        self.lease_session = lease_session
        self.trace = trace or RuntimeTraceBus()
        env_prompt_debug_dir = os.environ.get("WEBGPT_PROMPT_DEBUG_DIR")
        resolved_prompt_debug_dir = prompt_debug_dir or env_prompt_debug_dir
        self.prompt_debug_dir = (
            Path(resolved_prompt_debug_dir).expanduser()
            if resolved_prompt_debug_dir
            else None
        )
        if generation_timeout_seconds <= 0:
            raise ValueError("generation_timeout_seconds must be positive")
        self.generation_timeout_seconds = generation_timeout_seconds
        self.max_corrections = 2
        self.max_prompt_chars = int(os.environ.get("WEBGPT_MAX_PROMPT_CHARS", "200000"))
        if self.max_prompt_chars < 4_000:
            raise ValueError("WEBGPT_MAX_PROMPT_CHARS must be at least 4000")
        self._active_gateway_session_id: str | None = None

    def _trace_session_events(
        self, session: ChatGPTWebSession, record: ConversationRecord
    ) -> None:
        drain = getattr(session, "drain_events", None)
        if not callable(drain) or inspect.iscoroutinefunction(drain):
            return
        try:
            events = drain()
        except Exception:
            return
        if inspect.isawaitable(events):
            close = getattr(events, "close", None)
            if callable(close):
                close()
            return
        if not isinstance(events, list):
            return
        for event in events:
            if not isinstance(event, StateChanged):
                continue
            self.trace.emit(
                "session",
                "state_transition",
                session_id=record.session_id,
                conversation_id=record.conversation_id,
                metadata={
                    "from": event.old_state,
                    "to": event.new_state,
                    "evidence": event.reason or "session_state_machine",
                    "duration_ms": event.duration_ms,
                },
            )

    @staticmethod
    async def _forward_response_deltas(
        session: ChatGPTWebSession, callback: Callable[[str], Awaitable[None]]
    ) -> None:
        events = getattr(session, "events", None)
        if not callable(events):
            return
        try:
            async for event in events():
                if isinstance(event, ResponseDelta) and not event.revision and event.text:
                    await callback(event.text)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Optional live deltas must never compromise the completed turn.
            return

    def _write_prompt_debug(
        self,
        *,
        prompt: str,
        record: ConversationRecord,
        model: str,
        tail_messages: int,
        tool_count: int,
        trace_sequence: int,
        tag: str = "pre_gpt",
        protocol: str = "unknown",
        client: str = "unknown",
        tool_names: list[str] | None = None,
    ) -> str | None:
        """Write the exact redacted prompt sent to ChatGPT Web for debugging.

        This intentionally lives outside RuntimeTraceBus because trace events must
        stay structural by default.  The dump is opt-in via --prompt-debug-dir or
        WEBGPT_PROMPT_DEBUG_DIR and is mode-0600 local evidence.
        """
        if self.prompt_debug_dir is None:
            return None
        target_dir = self.prompt_debug_dir
        target_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(target_dir, 0o700)
        except PermissionError:
            pass
        redacted_prompt = default_redactor.redact_string(prompt)
        prompt_sha256 = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        redacted_sha256 = hashlib.sha256(redacted_prompt.encode("utf-8")).hexdigest()
        safe_session = re.sub(r"[^A-Za-z0-9_.-]", "_", record.session_id)[:80]
        stem = f"{trace_sequence:06d}_{safe_session}_{tag}"
        path = target_dir / f"{stem}.txt"
        metadata_path = target_dir / f"{stem}.json"
        header = {
            "kind": "webgpt_pre_gpt_prompt_debug",
            "trace_sequence": trace_sequence,
            "session_id": record.session_id,
            "conversation_id": record.conversation_id,
            "client": client,
            "protocol": protocol,
            "model": model,
            "tool_names": tool_names or [],
            "tail_messages": tail_messages,
            "tool_count": tool_count,
            "prompt_chars": len(prompt),
            "redacted_prompt_chars": len(redacted_prompt),
            "prompt_sha256": prompt_sha256,
            "redacted_prompt_sha256": redacted_sha256,
            "correction": tag == "correction",
            "note": "This is the redacted prompt body sent to ChatGPT Web after client/API adapter conversion.",
        }
        path.write_text(
            "# WEBGPT PRE-GPT PROMPT DEBUG\n"
            + json.dumps(header, ensure_ascii=False, indent=2)
            + "\n\n--- REDACTED_PROMPT_SENT_TO_CHATGPT_WEB ---\n"
            + redacted_prompt,
            encoding="utf-8",
        )
        metadata_path.write_text(
            json.dumps(header, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.chmod(path, 0o600)
        os.chmod(metadata_path, 0o600)
        return str(path)

    @property
    def active_gateway_session_id(self) -> str | None:
        return self._active_gateway_session_id

    async def position_session(
        self,
        session: ChatGPTWebSession,
        record: ConversationRecord,
        ui_model: str | None,
        reasoning_effort: str | None = None,
    ) -> None:
        if record.conversation_id:
            if session.conversation_id != record.conversation_id:
                await session.open(record.conversation_id)
        elif record.messages:
            if self._active_gateway_session_id != record.session_id:
                raise ConversationNotFound(
                    "This conversation has no persisted ChatGPT conversation id and cannot be restored on another worker."
                )
        else:
            # Freshly booted ChatGPT pages already sit on a blank composer.
            # Opening a new chat here adds a full extra UI action to the first
            # prompt.  Only click New Chat when this browser worker previously
            # served a different logical gateway conversation.
            if self._active_gateway_session_id is not None and self._active_gateway_session_id != record.session_id:
                await session.new_conversation()
        if ui_model is not None:
            await session.select_model(ui_model)
        if reasoning_effort:
            await session.select_reasoning_effort(reasoning_effort)

    async def execute_raw(
        self,
        record: ConversationRecord,
        tail: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        model: str,
        ui_model: str | None,
        tools: list[dict[str, Any]],
        tool_choice: Any,
        reasoning_effort: str | None = None,
        protocol: str = "unknown",
        client: str = "unknown",
        stream_callback: Callable[[str], Awaitable[None]] | None = None,
    ) -> tuple[TurnResult, str]:
        lease_started = time.monotonic()
        async with self.lease_session() as session:
            self.trace.emit(
                "completionruntime",
                "lease_acquired",
                session_id=record.session_id,
                conversation_id=record.conversation_id,
                metadata={"queue_ms": int((time.monotonic() - lease_started) * 1_000)},
            )
            return await self.execute_raw_on_session(
                session,
                record,
                tail,
                messages,
                model,
                ui_model,
                tools,
                tool_choice,
                reasoning_effort,
                protocol,
                client,
                stream_callback,
            )

    async def execute_raw_on_session(
        self,
        session: ChatGPTWebSession,
        record: ConversationRecord,
        tail: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        model: str,
        ui_model: str | None,
        tools: list[dict[str, Any]],
        tool_choice: Any,
        reasoning_effort: str | None = None,
        protocol: str = "unknown",
        client: str = "unknown",
        stream_callback: Callable[[str], Awaitable[None]] | None = None,
    ) -> tuple[TurnResult, str]:
        if not tail:
            raise ValueError("Request does not add a new message to this conversation.")
        # If a logical assistant/tool exchange was synthesized by the gateway
        # before touching ChatGPT Web, the first real browser submission must
        # carry the full transcript plus tool protocol.  Otherwise GPT Web sees
        # only a role=tool suffix and cannot know what controller tools exist.
        prompt_messages = messages if not record.web_bootstrapped else tail
        tool_schema_chars = len(
            ToolTranspiler.build_tool_instructions(tools, tool_choice) if tools else ""
        )
        conversation_chars = sum(message_content_chars(item) for item in prompt_messages)
        prompt = render_messages(
            prompt_messages,
            initial=not record.web_bootstrapped,
            tools=tools,
            tool_choice=tool_choice,
        )
        if len(prompt) > self.max_prompt_chars:
            message_budget = max(1_000, self.max_prompt_chars - tool_schema_chars - 2_000)
            compacted = compact_messages(
                prompt_messages,
                max_content_chars=message_budget,
            )
            compacted_prompt = render_messages(
                compacted,
                initial=not record.web_bootstrapped,
                tools=tools,
                tool_choice=tool_choice,
            )
            self.trace.emit(
                "promptcompat",
                "prompt_compacted",
                session_id=record.session_id,
                conversation_id=record.conversation_id,
                metadata={
                    "before_chars": len(prompt),
                    "after_chars": len(compacted_prompt),
                    "before_messages": len(prompt_messages),
                    "after_messages": len(compacted),
                    "max_prompt_chars": self.max_prompt_chars,
                },
            )
            prompt_messages = compacted
            prompt = compacted_prompt
        if len(prompt) > self.max_prompt_chars:
            raise ValueError(
                f"Prompt exceeds WEBGPT_MAX_PROMPT_CHARS={self.max_prompt_chars} after deterministic compaction."
            )
        prompt_event = self.trace.emit(
            "promptcompat",
            "prompt_built",
            session_id=record.session_id,
            conversation_id=record.conversation_id,
            metadata={
                "model": model,
                "tail_messages": len(tail),
                "tool_count": len(tools),
                "prompt_chars": len(prompt),
                "estimated_tokens": (len(prompt) + 3) // 4,
                "tool_schema_chars": tool_schema_chars,
                "conversation_chars": conversation_chars,
                "prompt_message_count": len(prompt_messages),
            },
        )
        prompt_debug_path = self._write_prompt_debug(
            prompt=prompt,
            record=record,
            model=model,
            tail_messages=len(tail),
            tool_count=len(tools),
            trace_sequence=prompt_event.sequence,
            protocol=protocol,
            client=client,
            tool_names=[
                str(tool.get("function", {}).get("name"))
                for tool in tools
                if isinstance(tool.get("function"), dict)
            ],
        )
        if prompt_debug_path:
            self.trace.emit(
                "promptcompat",
                "prompt_debug_written",
                session_id=record.session_id,
                conversation_id=record.conversation_id,
                metadata={
                    "path": prompt_debug_path,
                    "trace_sequence": prompt_event.sequence,
                },
            )
        self.conversations.mark_pending(
            record,
            messages=messages,
            model=model,
            tools=tools,
            tool_choice=tool_choice,
            prompt=prompt,
        )
        self.trace.emit(
            "conversation_store",
            "pending_marked",
            session_id=record.session_id,
            conversation_id=record.conversation_id,
        )
        browser_started = time.monotonic()
        self.trace.emit(
            "webchat",
            "position_start",
            session_id=record.session_id,
            conversation_id=record.conversation_id,
            metadata={"ui_model_requested": ui_model is not None, "effort_requested": reasoning_effort is not None},
        )
        await self.position_session(session, record, ui_model, reasoning_effort)
        self._trace_session_events(session, record)
        self.trace.emit(
            "webchat",
            "position_done",
            session_id=record.session_id,
            conversation_id=record.conversation_id,
        )
        self.trace.emit(
            "completionruntime",
            "submit_start",
            session_id=record.session_id,
            conversation_id=record.conversation_id,
        )
        event_task: asyncio.Task[None] | None = None
        if stream_callback is not None:
            event_task = asyncio.create_task(
                self._forward_response_deltas(session, stream_callback)
            )
        try:
            result = await session.send(prompt, timeout_seconds=self.generation_timeout_seconds)
            self._trace_session_events(session, record)
            correction_count = 0
            while True:
                issue = _tool_correction_issue(
                    result.text,
                    tail=tail,
                    messages=messages,
                    tools=tools,
                    tool_choice=tool_choice,
                )
                if issue is None:
                    break
                reason, detail = issue
                if correction_count >= self.max_corrections:
                    raise MalformedToolCall(
                        f"Tool correction budget exhausted ({reason}): {detail}"
                    )
                correction_count += 1
                self.trace.emit(
                    "completionruntime",
                    "tool_correction",
                    session_id=record.session_id,
                    conversation_id=record.conversation_id,
                    metadata={
                        "assistant_chars": len(result.text),
                        "reason": reason,
                        "correction_index": correction_count,
                        "max_corrections": self.max_corrections,
                    },
                )
                correction_prompt = _controller_tool_correction_prompt(
                    tools,
                    tool_choice,
                    reason=reason,
                    detail=detail,
                )
                correction_event = self.trace.emit(
                    "promptcompat",
                    "correction_prompt_built",
                    session_id=record.session_id,
                    conversation_id=record.conversation_id,
                    metadata={
                        "model": model,
                        "tail_messages": 0,
                        "tool_count": len(tools),
                        "prompt_chars": len(correction_prompt),
                        "reason": reason,
                        "correction_index": correction_count,
                    },
                )
                correction_debug_path = self._write_prompt_debug(
                    prompt=correction_prompt,
                    record=record,
                    model=model,
                    tail_messages=0,
                    tool_count=len(tools),
                    trace_sequence=correction_event.sequence,
                    tag="correction",
                    protocol=protocol,
                    client=client,
                    tool_names=[
                        str(tool.get("function", {}).get("name"))
                        for tool in tools
                        if isinstance(tool.get("function"), dict)
                    ],
                )
                if correction_debug_path:
                    self.trace.emit(
                        "promptcompat",
                        "prompt_debug_written",
                        session_id=record.session_id,
                        conversation_id=record.conversation_id,
                        metadata={
                            "path": correction_debug_path,
                            "trace_sequence": correction_event.sequence,
                            "tag": "correction",
                            "reason": reason,
                            "correction_index": correction_count,
                        },
                    )
                result = await session.send(
                    correction_prompt,
                    timeout_seconds=self.generation_timeout_seconds,
                )
                self._trace_session_events(session, record)
            if not result.text.strip():
                raise EmptyModelResponse(
                    "ChatGPT Web completed without assistant text or a tool call."
                )
        except CommitUnknown as exc:
            self._trace_session_events(session, record)
            if exc.conversation_id:
                record.conversation_id = exc.conversation_id
            self.conversations.mark_pending(
                record,
                messages=messages,
                model=model,
                tools=tools,
                tool_choice=tool_choice,
                prompt=prompt,
            )
            self._active_gateway_session_id = record.session_id
            self.trace.emit(
                "completionruntime",
                "commit_unknown",
                session_id=record.session_id,
                conversation_id=record.conversation_id,
            )
            raise
        except Exception as exc:
            self._trace_session_events(session, record)
            self.conversations.clear_pending(record)
            self.trace.emit(
                "completionruntime",
                "submit_failed_before_commit_unknown",
                session_id=record.session_id,
                conversation_id=record.conversation_id,
                metadata={"error_type": type(exc).__name__},
            )
            raise
        finally:
            if event_task is not None:
                event_task.cancel()
                with suppress(asyncio.CancelledError):
                    await event_task
        if result.conversation_id:
            record.conversation_id = result.conversation_id
        record.web_bootstrapped = True
        self._active_gateway_session_id = record.session_id
        self.trace.emit(
            "completionruntime",
            "submit_completed",
            session_id=record.session_id,
            conversation_id=record.conversation_id,
            metadata={
                "assistant_chars": len(result.text),
                "browser_ms": int((time.monotonic() - browser_started) * 1_000),
                "correction_count": correction_count,
                "turn_id": result.turn_id,
            },
        )
        return result, prompt

    async def execute(
        self,
        record: ConversationRecord,
        tail: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        model: str,
        ui_model: str | None,
        tools: list[dict[str, Any]],
        tool_choice: Any,
        reasoning_effort: str | None = None,
        protocol: str = "unknown",
        client: str = "unknown",
        stream_callback: Callable[[str], Awaitable[None]] | None = None,
    ) -> CompletionExecution:
        result, prompt = await self.execute_raw(
            record,
            tail,
            messages,
            model,
            ui_model,
            tools,
            tool_choice,
            reasoning_effort,
            protocol,
            client,
            stream_callback,
        )
        parse_started = time.monotonic()
        turn = AssistantTurnBuilder.from_model_text(
            result.text,
            tools=tools,
            tool_choice=tool_choice,
            model=model,
        )
        parse_ms = int((time.monotonic() - parse_started) * 1_000)
        self.trace.emit(
            "assistantturn",
            "parsed",
            session_id=record.session_id,
            conversation_id=record.conversation_id,
            metadata={
                "finish_reason": turn.finish_reason,
                "tool_calls": len(turn.tool_calls),
                "content_chars": len(turn.content or ""),
                "parse_ms": parse_ms,
            },
        )
        return CompletionExecution(turn=turn, web_result=result, prompt=prompt)

    async def reconcile_pending(
        self,
        record: ConversationRecord,
        messages: list[dict[str, Any]],
        model: str,
        ui_model: str | None,
        tools: list[dict[str, Any]],
        tool_choice: Any,
        reasoning_effort: str | None = None,
    ) -> CompletionExecution | None:
        prompt = record.pending_prompt
        if not prompt:
            return None
        self.trace.emit(
            "completionruntime",
            "reconcile_start",
            session_id=record.session_id,
            conversation_id=record.conversation_id,
        )
        async with self.lease_session() as session:
            if record.conversation_id:
                await self.position_session(session, record, ui_model, reasoning_effort)
            else:
                if ui_model is not None:
                    await session.select_model(ui_model)
                if reasoning_effort:
                    await session.select_reasoning_effort(reasoning_effort)
            reconciliation = await session.reconcile(prompt)
        self.trace.emit(
            "completionruntime",
            "reconcile_observed",
            session_id=record.session_id,
            conversation_id=reconciliation.conversation_id or record.conversation_id,
            metadata={
                "user_turn_present": reconciliation.user_turn_present,
                "assistant_present": reconciliation.assistant_text is not None,
            },
        )
        if not reconciliation.user_turn_present:
            return None
        if reconciliation.conversation_id:
            record.conversation_id = reconciliation.conversation_id
        if reconciliation.assistant_text is None:
            raise CommitUnknown(
                "The pending user turn is persisted but its assistant completion is not yet available.",
                conversation_id=record.conversation_id,
            )
        result = TurnResult(
            turn_id=f"reconciled_{uuid.uuid4().hex[:12]}",
            conversation_id=record.conversation_id,
            text=reconciliation.assistant_text,
            model=model,
        )
        turn = AssistantTurnBuilder.from_model_text(
            result.text,
            tools=tools,
            tool_choice=tool_choice,
            model=model,
        )
        self._active_gateway_session_id = record.session_id
        return CompletionExecution(
            turn=turn,
            web_result=result,
            prompt=prompt,
            reconciled=True,
        )


__all__ = ["CompletionExecution", "CompletionRuntime", "SessionLeaseFactory"]

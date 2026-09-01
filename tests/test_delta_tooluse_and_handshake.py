"""R5 pipeline fixes -- BUG-A (delta-path tool_use dropped) + BUG-B (handshake).

Evidence: docs/reports/live-cli-verify-round5-2026-08-24.md. T2 passed but T3
failed 3/3 with two precisely diagnosed gateway bugs:

BUG-A: a tool_use parsed on the feedback-delta turn (the turn reached through
``_record_for_pending_tool_results`` -> ``complete_normalized(append_to_session=
True)``) never reached the client execution loop over the live SSE stream: the
protocol-blind stream sieve either leaked the raw ``<cmd>`` tags as text deltas
or failed closed ("Late tool call cannot be safely streamed"), and the post-task
reconciliation only flushed the first TEXT block. The next client POST replayed
a transcript missing that round -> prose finale with RC=0 (T3c/T-D/T-D2,
reproduced 3/3). Fix: tool-bearing requests replay the complete finalized
content (no live delta forwarding), and trailing blocks after streamed text are
always emitted.

BUG-B: ``soft_handshake_appended`` was only true on the first turn of a fresh
tool conversation, while every later web thread is a NEW model instance that
never saw the <cmd> convention -> deflection to prose. Fix: re-append whenever
the record is not yet web-bootstrapped OR the observed web thread changed.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest

from gpt.conversations import ConversationRecord
from gpt.gateway.runtime import (
    CompletionRuntime,
    _with_soft_handshake,
)
from gpt.gateway.server import create_api_app as create_gateway_app
from gpt.promptcompat import render_messages
from gpt.types import TurnResult
from gpt.utils.assistantturn import AssistantTurnBuilder

BASH_TOOL_ANTHROPIC = {
    "name": "Bash",
    "description": "run a shell command",
    "input_schema": {
        "type": "object",
        "properties": {"command": {"type": "string"}},
        "required": ["command"],
    },
}
BASH_TOOL = {
    "type": "function",
    "function": {
        "name": "Bash",
        "description": "run a shell command",
        "parameters": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    },
}

USER_TASK = "Doc file task.md roi tao fizzbuzz.py va chay no."
HANDSHAKE_MARK = "<cmd>the exact shell command</cmd>"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sse_events(lines: list[str]) -> list[dict[str, object]]:
    """Parse an SSE line stream into [{event, data}] pairs."""
    events: list[dict[str, object]] = []
    current: str | None = None
    for line in lines:
        if line.startswith("event: "):
            current = line.removeprefix("event: ").strip()
        elif line.startswith("data: ") and current is not None:
            events.append({"event": current, "data": json.loads(line[6:])})
            current = None
    return events


def _tool_use_blocks(events: list[dict[str, object]]) -> list[dict[str, object]]:
    return [
        event["data"]
        for event in events
        if event["event"] == "content_block_start"
        and isinstance(event["data"], dict)
        and event["data"].get("content_block", {}).get("type") == "tool_use"
    ]


def _stop_reason(events: list[dict[str, object]]) -> str | None:
    for event in events:
        if event["event"] == "message_delta" and isinstance(event["data"], dict):
            return event["data"]["delta"]["stop_reason"]
    return None


def _fake_session(responses: list[str]) -> MagicMock:
    session = MagicMock()
    session.conversation_id = None
    session.new_conversation = AsyncMock()
    session.open = AsyncMock()
    session.select_model = AsyncMock()
    session.select_reasoning_effort = AsyncMock()

    prompts: list[str] = []

    async def send(prompt, timeout_seconds=None):
        index = len(prompts)
        prompts.append(prompt)
        return TurnResult(
            turn_id=f"turn_{index}",
            conversation_id=f"conv_web_{index}",
            text=responses[min(index, len(responses) - 1)],
        )

    setattr(send, "prompts", prompts)  # noqa: B010 -- intentional callable test state
    session.send = send
    return session


def _install_fake_lease(monkeypatch, server, session: MagicMock) -> None:
    @asynccontextmanager
    async def lease(*args, affinity_key=None):
        yield session

    monkeypatch.setattr(server, "_lease_session", lease)
    monkeypatch.setattr(server.completion_runtime, "lease_session", lease)


async def _post_messages(app, payload: dict[str, object]) -> list[dict[str, object]]:
    from httpx import ASGITransport, AsyncClient

    transport = ASGITransport(app=app)
    async with (
        AsyncClient(transport=transport, base_url="http://gateway") as client,
        client.stream("POST", "/v1/messages", json=payload) as response,
    ):
        assert response.status_code == 200
        lines = [line async for line in response.aiter_lines()]
    return _sse_events(lines)


def _anthropic_user(text: str) -> dict[str, object]:
    return {"role": "user", "content": [{"type": "text", "text": text}]}


def _anthropic_tool_result(call_id: str, content: str) -> dict[str, object]:
    return {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": call_id, "content": content}
        ],
    }


def _anthropic_tool_use(call_id: str, command: str) -> dict[str, object]:
    return {
        "role": "assistant",
        "content": [
            {
                "type": "tool_use",
                "id": call_id,
                "name": "Bash",
                "input": {"command": command},
            }
        ],
    }


# ---------------------------------------------------------------------------
# BUG-A: tool_use parsed on the feedback-delta path reaches the client loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delta_turn_tool_use_reaches_client_execution(monkeypatch):
    """The R5 T-D reproduction over /v1/messages streaming.

    Round 1 emits a tool call; the client executes it and resends the
    transcript plus the result (the feedback-delta path). Round 2 MUST deliver
    its own parsed tool_use block to the client instead of being dropped.
    """
    monkeypatch.setenv("WEBGPT_TOOL_PROTOCOL", "soft")
    monkeypatch.delenv("WEBGPT_PROMPT_DEBUG_DIR", raising=False)
    session = _fake_session(
        [
            "<cmd>echo one > d/one.txt</cmd>",
            "<cmd>cp d/one.txt d/two.txt</cmd>",
        ]
    )
    app = create_gateway_app()
    _install_fake_lease(monkeypatch, app.state.server, session)

    # Round 1: fresh conversation.
    events1 = await _post_messages(
        app,
        {
            "model": "claude-code-local",
            "max_tokens": 64,
            "stream": True,
            "tools": [BASH_TOOL_ANTHROPIC],
            "messages": [_anthropic_user(USER_TASK)],
        },
    )
    assert "event: error" not in " ".join(str(e["event"]) for e in events1)
    blocks1 = _tool_use_blocks(events1)
    assert len(blocks1) == 1, events1
    call_id = blocks1[0]["content_block"]["id"]  # type: ignore[index]
    assert _stop_reason(events1) == "tool_use"

    # Round 2: transcript + tool result resent through the delta path.
    events2 = await _post_messages(
        app,
        {
            "model": "claude-code-local",
            "max_tokens": 64,
            "stream": True,
            "tools": [BASH_TOOL_ANTHROPIC],
            "messages": [
                _anthropic_user(USER_TASK),
                _anthropic_tool_use(call_id, "echo one > d/one.txt"),
                _anthropic_tool_result(call_id, "one"),
            ],
        },
    )
    event_names = [str(e["event"]) for e in events2]
    assert "error" not in event_names, events2
    # BUG-A regression: the second round's parsed tool_use must arrive as a
    # real tool_use content block (pre-fix it was dropped from the SSE stream).
    blocks2 = _tool_use_blocks(events2)
    assert len(blocks2) == 1, events2
    deltas = [
        str(e["data"].get("delta", {}).get("partial_json", ""))  # type: ignore[attr-defined]
        for e in events2
        if e["event"] == "content_block_delta"
    ]
    assert any("cp d/one.txt d/two.txt" in delta for delta in deltas), json.dumps(events2)
    assert _stop_reason(events2) == "tool_use"


@pytest.mark.asyncio
async def test_transcript_after_delta_tool_round_contains_the_full_round(monkeypatch):
    """Gate (b): the replayable record after a delta tool round carries BOTH the
    tool_use and its result, so any later full render/replay includes them."""
    monkeypatch.setenv("WEBGPT_TOOL_PROTOCOL", "soft")
    monkeypatch.delenv("WEBGPT_PROMPT_DEBUG_DIR", raising=False)
    session = _fake_session(
        [
            "<cmd>echo one > d/one.txt</cmd>",
            "<cmd>cp d/one.txt d/two.txt</cmd>",
            "Both steps are done.",
        ]
    )
    app = create_gateway_app()
    server = app.state.server
    _install_fake_lease(monkeypatch, server, session)

    base = {
        "model": "claude-code-local",
        "max_tokens": 64,
        "stream": True,
        "tools": [BASH_TOOL_ANTHROPIC],
    }
    events1 = await _post_messages(
        app, {**base, "messages": [_anthropic_user(USER_TASK)]}
    )
    call_id = _tool_use_blocks(events1)[0]["content_block"]["id"]  # type: ignore[index]
    events2 = await _post_messages(
        app,
        {
            **base,
            "messages": [
                _anthropic_user(USER_TASK),
                _anthropic_tool_use(call_id, "echo one > d/one.txt"),
                _anthropic_tool_result(call_id, "RESULT_ONE_MARKER"),
            ],
        },
    )
    # Round 3 proves the loop keeps working on top of the committed round.
    # The client echoes back the exact tool_use id the gateway emitted.
    call_id2 = _tool_use_blocks(events2)[0]["content_block"]["id"]  # type: ignore[index]
    events3 = await _post_messages(
        app,
        {
            **base,
            "messages": [
                _anthropic_user(USER_TASK),
                _anthropic_tool_use(call_id, "echo one > d/one.txt"),
                _anthropic_tool_result(call_id, "RESULT_ONE_MARKER"),
                _anthropic_tool_use(call_id2, "cp d/one.txt d/two.txt"),
                _anthropic_tool_result(call_id2, "RESULT_TWO_MARKER"),
            ],
        },
    )
    assert _stop_reason(events3) == "end_turn"

    record = next(iter(server.conversations._records.values()))
    roles = [message["role"] for message in record.messages]
    assert roles.count("tool") >= 2
    assistant_calls = json.dumps(
        [
            message.get("tool_calls")
            for message in record.messages
            if message.get("role") == "assistant"
        ],
        ensure_ascii=False,
    )
    assert "cp d/one.txt d/two.txt" in assistant_calls
    tool_contents = " ".join(
        str(message.get("content"))
        for message in record.messages
        if message.get("role") == "tool"
    )
    assert "RESULT_ONE_MARKER" in tool_contents
    # A full initial render of the record (what a fresh thread would see)
    # replays both rounds: the heredoc-style loss from R5 cannot recur.
    replayed = render_messages(
        record.messages,
        initial=True,
        tools=[BASH_TOOL],
        tool_choice=None,
        tool_protocol="soft",
    )
    assert "RESULT_ONE_MARKER" in replayed
    assert "cp d/one.txt d/two.txt" in replayed


# ---------------------------------------------------------------------------
# BUG-B: soft handshake follows the web-thread audience
# ---------------------------------------------------------------------------


def _runtime_with_session(session: MagicMock) -> CompletionRuntime:
    @asynccontextmanager
    async def lease(*_args, **_kwargs):
        yield session

    return CompletionRuntime(conversations=MagicMock(), lease_session=lease)


def _run_turn(runtime, session, record, messages, tail=None):
    return runtime.execute_raw_on_session(
        session,
        record,
        tail=tail if tail is not None else [messages[-1]],
        messages=messages,
        model="chatgpt-web",
        ui_model=None,
        tools=[BASH_TOOL],
        tool_choice=None,
    )


@pytest.mark.asyncio
async def test_handshake_reappended_when_web_thread_changes(monkeypatch):
    """Gate (c): a rotated/reset web conversation is a new audience."""
    monkeypatch.setenv("WEBGPT_TOOL_PROTOCOL", "soft")
    session = _fake_session(["<cmd>pwd</cmd>", "<cmd>ls</cmd>"])
    runtime = _runtime_with_session(session)

    record = ConversationRecord()
    messages = [{"role": "user", "content": USER_TASK}]
    await _run_turn(runtime, session, record, messages)
    assert HANDSHAKE_MARK in session.send.prompts[0]

    # Failover-style reset: the record is rebound to a brand-new web thread.
    record.conversation_id = "conv_rotated_b"
    tail2 = [
        {
            "role": "user",
            "content": "WEBGPT_TOOL_RESULT {\"content\":\"ok\"} Continue.",
        }
    ]
    await _run_turn(
        runtime, session, record, [*messages, {"role": "tool", "tool_call_id": "call_x", "content": "ok"}],
        tail=tail2,
    )
    assert HANDSHAKE_MARK in session.send.prompts[1]


@pytest.mark.asyncio
async def test_handshake_on_fresh_record_replaying_tool_traffic(monkeypatch):
    """The exact live R5 case: every POST opened a NEW gateway record whose
    transcript already contained controller tool traffic -- the old
    ``_fresh_tool_conversation`` gate suppressed the handshake there."""
    monkeypatch.setenv("WEBGPT_TOOL_PROTOCOL", "soft")
    session = _fake_session(["<cmd>cat task.md</cmd>"])
    runtime = _runtime_with_session(session)

    transcript = [
        {"role": "user", "content": USER_TASK},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_prev",
                    "type": "function",
                    "function": {"name": "Bash", "arguments": "{\"command\": \"cat task.md\"}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_prev", "content": "task body"},
    ]
    record = ConversationRecord()  # brand-new record, never web-bootstrapped
    await _run_turn(runtime, session, record, transcript)
    assert HANDSHAKE_MARK in session.send.prompts[0]


@pytest.mark.asyncio
async def test_handshake_not_repeated_within_same_conversation(monkeypatch):
    """Gate (d): consecutive turns on the same bound web thread keep it once."""
    monkeypatch.setenv("WEBGPT_TOOL_PROTOCOL", "soft")
    session = _fake_session(["<cmd>pwd</cmd>", "2 + 2 equals 4."])
    runtime = _runtime_with_session(session)

    record = ConversationRecord()
    messages = [{"role": "user", "content": USER_TASK}]
    await _run_turn(runtime, session, record, messages)
    turn = AssistantTurnBuilder.from_model_text(
        "<cmd>pwd</cmd>", tools=[BASH_TOOL], tool_choice=None
    )
    assert HANDSHAKE_MARK in session.send.prompts[0]

    messages2 = [*messages, {"role": "assistant", "content": "", "tool_calls": turn.tool_calls}, {"role": "user", "content": "Thanks! What is 2 + 2?"}]
    await _run_turn(runtime, session, record, messages2)
    assert HANDSHAKE_MARK not in session.send.prompts[1]


def test_handshake_helper_still_single_sentence():
    suffixed = _with_soft_handshake("task")
    assert suffixed.count(HANDSHAKE_MARK) == 1

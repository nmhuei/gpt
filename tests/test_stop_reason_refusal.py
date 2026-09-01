"""STOP-REASON-REFUSAL (parity-delta-audit 2026-08-26 row M / G5).

Pins the new terminal-refusal contract end to end:

1. Runtime: a definitive model refusal (persistent hard TOOL_REFUSAL,
   persistent soft refusal, refusal-shaped correction-budget exhaustion)
   raises ``ModelRefusalError`` -- a ``MalformedToolCall`` subclass so every
   fail-closed guard stays intact -- while a repeated MALFORMED_TOOL keeps
   raising plain ``MalformedToolCall``.
2. Anthropic boundary (/v1/messages): ``ModelRefusalError`` becomes HTTP 200
   with ``stop_reason:"refusal"`` plus an honest text block, both non-stream
   and SSE stream (content block + ``message_delta`` stop_reason refusal +
   ``message_stop``, never an ``event: error``).  Infrastructure failures
   keep their retryable statuses: plain MalformedToolCall -> 502 api_error,
   RateLimited -> 429 rate_limit_error, overloaded RateLimited -> 529.
3. OpenAI boundary (/v1/chat/completions): unchanged 502
   malformed_model_tool_call -- the refusal stop-reason is Anthropic wire.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.testclient import TestClient

from gpt.api.protocol_adapters import parse_anthropic_request
from gpt.api.server import create_api_app as create_api_server_app
from gpt.conversations import ConversationRecord
from gpt.gateway.runtime import CompletionRuntime, ModelRefusalError
from gpt.gateway.server import create_api_app as create_gateway_app
from gpt.state import MalformedToolCall, RateLimited
from gpt.types import TurnResult

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

CLI_TOOLS = [
    {
        "type": "function",
        "function": {"name": name, "parameters": {"type": "object"}},
    }
    for name in ("Bash", "Read")
]

TASK_MESSAGES = [
    {
        "role": "user",
        "content": [
            {
                "type": "text",
                "text": (
                    "<system-reminder>\nWorking directory: /tmp/cc-refusal\n"
                    "</system-reminder>\n\n"
                    "# Task\n"
                    "Chay script python fizzbuzz.py va ghi output vao output.txt.\n"
                ),
            }
        ],
    }
]

HARD_REFUSAL_TEXT = "I cannot execute that tool because tools are unavailable."
SOFT_REFUSAL_TEXT = (
    "Bạn muốn tôi tiếp tục phần nào? Could you tell me more about the task?"
)

MALFORMED_BLOCK = (
    "<tool_calls>\n"
    '  <invoke name="Bash">\n'
    "    <parameter>oops-no-name-attribute</parameter>\n"
    "  </invoke>\n"
    "</tool_calls>"
)


def _turn(text: str, turn_id: str) -> TurnResult:
    return TurnResult(turn_id=turn_id, conversation_id="conv_refusal", text=text)


def _fake_session(*turns: str) -> MagicMock:
    session = MagicMock()
    session.conversation_id = None
    session.new_conversation = AsyncMock()
    session.open = AsyncMock()
    session.select_model = AsyncMock()
    session.select_reasoning_effort = AsyncMock()
    session.send = AsyncMock(side_effect=[_turn(t, f"refusal_{i}") for i, t in enumerate(turns)])
    return session


def _runtime(session: MagicMock) -> CompletionRuntime:
    @asynccontextmanager
    async def lease(*_args, **_kwargs):
        yield session

    return CompletionRuntime(
        conversations=MagicMock(),
        lease_session=lease,
    )


def _run(session: MagicMock, runtime: CompletionRuntime):
    return runtime.execute_raw_on_session(
        session,
        ConversationRecord(),
        tail=TASK_MESSAGES,
        messages=TASK_MESSAGES,
        model="claude-3-5-sonnet",
        ui_model=None,
        tools=CLI_TOOLS,
        tool_choice=None,
    )


def _events(runtime: CompletionRuntime, kind: str) -> list:
    return [
        event
        for event in runtime.trace.snapshot()
        if event.component == "completionruntime" and event.kind == kind
    ]


def _messages_payload(**overrides):
    payload = {
        "model": "chatgpt-web",
        "max_tokens": 64,
        "messages": [{"role": "user", "content": "Use Bash to run pwd"}],
    }
    payload.update(overrides)
    return payload


def _refusal_exc(message: str = "Persistent TOOL_REFUSAL after correction: nope") -> ModelRefusalError:
    return ModelRefusalError(message)


# ---------------------------------------------------------------------------
# (1) Runtime raise classification
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_persistent_hard_refusal_raises_model_refusal_error(monkeypatch):
    """Two identical hard refusals terminate as ModelRefusalError."""
    monkeypatch.setenv("WEBGPT_MAX_CORRECTIONS", "3")
    monkeypatch.delenv("WEBGPT_PROMPT_DEBUG_DIR", raising=False)
    session = _fake_session(HARD_REFUSAL_TEXT, HARD_REFUSAL_TEXT)
    runtime = _runtime(session)

    with pytest.raises(ModelRefusalError) as excinfo:
        await _run(session, runtime)

    # Subclass contract: every existing fail-closed guard still catches it.
    assert isinstance(excinfo.value, MalformedToolCall)
    assert "Persistent TOOL_REFUSAL after correction" in str(excinfo.value)
    assert session.send.await_count == 2
    assert len(_events(runtime, "persistent_tool_failure")) == 1


@pytest.mark.asyncio
async def test_persistent_soft_refusal_raises_model_refusal_error(monkeypatch):
    monkeypatch.setenv("WEBGPT_MAX_CORRECTIONS", "3")
    monkeypatch.delenv("WEBGPT_PROMPT_DEBUG_DIR", raising=False)
    session = _fake_session(SOFT_REFUSAL_TEXT, SOFT_REFUSAL_TEXT)
    runtime = _runtime(session)

    with pytest.raises(ModelRefusalError) as excinfo:
        await _run(session, runtime)

    assert isinstance(excinfo.value, MalformedToolCall)
    assert "Persistent tool refusal after correction" in str(excinfo.value)
    assert len(_events(runtime, "persistent_tool_refusal")) == 1


@pytest.mark.asyncio
async def test_refusal_budget_exhaustion_raises_model_refusal_error(monkeypatch):
    """Budget exhaustion on a refusal-shaped reason converts too."""
    monkeypatch.setenv("WEBGPT_MAX_CORRECTIONS", "1")
    monkeypatch.delenv("WEBGPT_PROMPT_DEBUG_DIR", raising=False)
    session = _fake_session(HARD_REFUSAL_TEXT, HARD_REFUSAL_TEXT)
    runtime = _runtime(session)

    with pytest.raises(ModelRefusalError):
        await _run(session, runtime)

    exhausted = _events(runtime, "correction_budget_exhausted")
    assert len(exhausted) == 1
    assert exhausted[0].metadata["reason"] == "TOOL_REFUSAL"


@pytest.mark.asyncio
async def test_persistent_malformed_tool_keeps_plain_malformed_tool_call(monkeypatch):
    """Regression: repeated MALFORMED_TOOL is NOT a refusal -> stays 502-class."""
    monkeypatch.setenv("WEBGPT_MAX_CORRECTIONS", "3")
    monkeypatch.delenv("WEBGPT_PROMPT_DEBUG_DIR", raising=False)
    session = _fake_session(MALFORMED_BLOCK, MALFORMED_BLOCK)
    runtime = _runtime(session)

    with pytest.raises(MalformedToolCall) as excinfo:
        await _run(session, runtime)

    assert type(excinfo.value) is MalformedToolCall
    assert not isinstance(excinfo.value, ModelRefusalError)
    assert len(_events(runtime, "persistent_tool_failure")) == 1


# ---------------------------------------------------------------------------
# (2) Anthropic boundary: /v1/messages
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_anthropic_messages_refusal_returns_200_stop_reason_refusal():
    app = create_api_server_app(headless=True)
    server = app.state.server

    async def refuse(_adapted, stream_callback=None):
        raise _refusal_exc()

    server._complete_anthropic = refuse

    with TestClient(app) as client:
        response = client.post("/v1/messages", json=_messages_payload())

    assert response.status_code == 200
    payload = response.json()
    assert payload["type"] == "message"
    assert payload["role"] == "assistant"
    assert payload["stop_reason"] == "refusal"
    assert payload["stop_sequence"] is None
    assert len(payload["content"]) == 1
    block = payload["content"][0]
    assert block["type"] == "text"
    assert "[webgpt-gateway:model_refusal]" in block["text"]
    assert "Persistent TOOL_REFUSAL" in block["text"]
    usage = payload["usage"]
    assert usage["output_tokens"] > 0
    assert usage["input_tokens"] > 0


@pytest.mark.asyncio
async def test_anthropic_stream_refusal_closes_completed_turn_with_refusal():
    app = create_api_server_app(headless=True)
    server = app.state.server

    async def refuse(_adapted, stream_callback=None):
        raise _refusal_exc()

    server._complete_anthropic = refuse
    request = MagicMock()
    request.is_disconnected = AsyncMock(return_value=False)
    adapted = parse_anthropic_request({**_messages_payload(), "stream": True})

    events = [event async for event in server._anthropic_live_stream(request, adapted)]

    assert events[0].startswith("event: message_start")
    assert any(e.startswith("event: content_block_start") for e in events)
    deltas = [e for e in events if e.startswith("event: content_block_delta")]
    assert deltas and "[webgpt-gateway:model_refusal]" in deltas[0]
    assert events[-1] == "event: message_stop\ndata: {\"type\": \"message_stop\"}\n\n"
    message_deltas = [e for e in events if e.startswith("event: message_delta")]
    assert len(message_deltas) == 1
    frame = json.loads(message_deltas[0].split("\ndata: ", 1)[1])
    assert frame["delta"]["stop_reason"] == "refusal"
    # A completed turn never carries a retryable error signal (R4-DOUBLING).
    assert not any(e.startswith("event: error") for e in events), events


@pytest.mark.asyncio
async def test_anthropic_stream_refusal_after_content_appends_into_block():
    """Refusal raised AFTER live text was streamed: append into the open
    block (never replay), close once, end with stop_reason:"refusal"."""

    app = create_api_server_app(headless=True)
    server = app.state.server

    async def refuse_mid_stream(_adapted, stream_callback=None):
        if stream_callback is not None:
            await stream_callback("Working on it...")
        raise _refusal_exc()

    server._complete_anthropic = refuse_mid_stream
    request = MagicMock()
    request.is_disconnected = AsyncMock(return_value=False)
    adapted = parse_anthropic_request({**_messages_payload(), "stream": True})

    events = [event async for event in server._anthropic_live_stream(request, adapted)]

    starts = [e for e in events if e.startswith("event: content_block_start")]
    assert len(starts) == 1
    deltas = [e for e in events if e.startswith("event: content_block_delta")]
    assert len(deltas) == 2
    second = json.loads(deltas[1].split("\ndata: ", 1)[1])
    # Appended into the SAME open block, separated by a blank line.
    assert second["delta"]["text"].startswith("\n\n")
    assert "[webgpt-gateway:model_refusal]" in second["delta"]["text"]
    message_deltas = [e for e in events if e.startswith("event: message_delta")]
    frame = json.loads(message_deltas[0].split("\ndata: ", 1)[1])
    assert frame["delta"]["stop_reason"] == "refusal"
    assert events[-1].startswith("event: message_stop")
    assert not any(e.startswith("event: error") for e in events), events


@pytest.mark.asyncio
async def test_anthropic_messages_malformed_still_502():
    """Regression: non-refusal MalformedToolCall keeps 502 api_error."""
    app = create_api_server_app(headless=True)
    server = app.state.server

    async def broken(_adapted, stream_callback=None):
        raise MalformedToolCall("Correction loop not converging: identical prompt")

    server._complete_anthropic = broken

    with TestClient(app) as client:
        response = client.post("/v1/messages", json=_messages_payload())

    assert response.status_code == 502
    error = response.json()["error"]
    assert error["type"] == "api_error"
    assert "not converging" in error["message"]


@pytest.mark.asyncio
async def test_anthropic_infrastructure_errors_keep_retryable_statuses():
    """Regression (mandatory): breaker/quota paths keep their status codes."""
    app = create_api_server_app(headless=True)
    server = app.state.server

    cases = [
        (RateLimited("You have hit your usage cap"), 429, "rate_limit_error"),
        (RateLimited("model is overloaded right now"), 529, "overloaded_error"),
    ]
    for exc, status, err_type in cases:
        async def fail(_adapted, stream_callback=None, _exc=exc):
            raise _exc

        server._complete_anthropic = fail
        with TestClient(app) as client:
            response = client.post("/v1/messages", json=_messages_payload())
        assert response.status_code == status, (exc, response.status_code)
        assert response.json()["error"]["type"] == err_type, exc


# ---------------------------------------------------------------------------
# (3) Boundaries that must not change
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gateway_app_anthropic_messages_refusal_parity():
    """Mirror parity: gateway/server.py /v1/messages behaves identically."""
    app = create_gateway_app()
    server = app.state.server

    async def refuse(_adapted, stream_callback=None):
        raise _refusal_exc()

    server._complete_anthropic = refuse

    with TestClient(app) as client:
        response = client.post("/v1/messages", json=_messages_payload())

    assert response.status_code == 200
    payload = response.json()
    assert payload["stop_reason"] == "refusal"
    assert "[webgpt-gateway:model_refusal]" in payload["content"][0]["text"]


@pytest.mark.asyncio
async def test_openai_chat_completions_refusal_keeps_502():
    """OpenAI wire has no refusal stop_reason -> unchanged 502 mapping."""
    app = create_api_server_app(headless=True)
    server = app.state.server
    server.completion_runtime.execute = AsyncMock(side_effect=_refusal_exc())

    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "chatgpt-web",
                "messages": [{"role": "user", "content": "Use Bash to run pwd"}],
            },
        )

    assert response.status_code == 502
    error = response.json()["error"]
    assert error["code"] == "malformed_model_tool_call"
    assert error["retryable"] is False

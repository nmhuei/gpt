"""Claude Code Gateway Protocol conformance gates.

These tests use the public ``gpt.gateway`` entry point so they cover the
runtime that Claude Code is configured to call.
"""

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.testclient import TestClient

from gpt.api.protocol_adapters import parse_anthropic_request
from gpt.gateway import create_api_app
from gpt.state import AuthRequired, ConversationNotFound, RateLimited
from gpt.types import TurnResult


def _fake_session(text: str = "Claude Code answer") -> MagicMock:
    session = MagicMock()
    session.new_conversation = AsyncMock()
    session.open = AsyncMock()
    session.select_model = AsyncMock()
    session.select_reasoning_effort = AsyncMock()
    session.send = AsyncMock(
        return_value=TurnResult(turn_id="turn_1", conversation_id="conv_1", text=text)
    )
    session.conversation_id = None
    return session


def _fake_lease(session: MagicMock):
    @asynccontextmanager
    async def lease():
        yield session

    return lease


def _install_fake_session(monkeypatch, server, session: MagicMock) -> None:
    lease = _fake_lease(session)
    monkeypatch.setattr(server, "_lease_session", lease)
    monkeypatch.setattr(server.completion_runtime, "lease_session", lease)


def _messages_payload(**overrides: object) -> dict[str, object]:
    return {
        "model": "claude-code-local",
        "max_tokens": 128,
        "messages": [{"role": "user", "content": "hello gateway"}],
        **overrides,
    }


def _event_names(body: str) -> list[str]:
    return [line.removeprefix("event: ") for line in body.splitlines() if line.startswith("event: ")]


def test_gate_1_messages_contract_and_count_tokens(monkeypatch):
    app = create_api_app()
    _install_fake_session(monkeypatch, app.state.server, _fake_session())

    with TestClient(app) as client:
        message = client.post("/v1/messages", json=_messages_payload())
        versioned_count = client.post("/v1/messages/count_tokens", json=_messages_payload())
        unversioned_count = client.post("/messages/count_tokens", json=_messages_payload())

    assert message.status_code == 200
    assert message.json()["type"] == "message"
    assert message.json()["content"] == [{"type": "text", "text": "Claude Code answer"}]
    assert versioned_count.json() == unversioned_count.json()
    assert isinstance(versioned_count.json()["input_tokens"], int)
    assert versioned_count.json()["input_tokens"] > 0


def test_gate_2_sse_events_are_ordered_and_progressive(monkeypatch):
    app = create_api_app()
    _install_fake_session(
        monkeypatch, app.state.server, _fake_session("one two three four five six seven eight")
    )

    with TestClient(app) as client:
        response = client.post("/v1/messages", json=_messages_payload(stream=True))

    assert response.status_code == 200
    assert _event_names(response.text) == [
        "message_start",
        "content_block_start",
        "content_block_delta",
        "content_block_delta",
        "content_block_stop",
        "message_delta",
        "message_stop",
    ]


def test_gate_3_beta_query_and_claude_headers_are_retained(monkeypatch):
    app = create_api_app()
    server = app.state.server
    captured: dict[str, str] = {}
    complete = server.complete_normalized

    async def capture_headers(*args, **kwargs):
        captured.update(args[0].request_headers)
        return await complete(*args, **kwargs)

    monkeypatch.setattr(server, "complete_normalized", capture_headers)
    _install_fake_session(monkeypatch, server, _fake_session())
    headers = {
        "anthropic-version": "2023-06-01",
        "anthropic-beta": "prompt-caching-2024-07-31,custom-beta-flag",
        "x-api-key": "local-key",
        "x-claude-code-session-id": "session_1",
        "x-claude-code-agent-id": "agent_1",
        "x-claude-code-parent-agent-id": "parent_1",
    }

    with TestClient(app) as client:
        response = client.post("/v1/messages?beta=true", json=_messages_payload(), headers=headers)

    assert response.status_code == 200
    assert captured == {name.lower(): value for name, value in headers.items()}


def test_gate_4_tool_use_is_transpiled_to_anthropic_blocks(monkeypatch):
    app = create_api_app()
    _install_fake_session(
        monkeypatch,
        app.state.server,
        _fake_session(
            '<WEBGPT_TOOL_CALL>\n{"name":"Read","arguments":{"path":"README.md"}}\n</WEBGPT_TOOL_CALL>'
        ),
    )
    payload = _messages_payload(
        tools=[
            {
                "name": "Read",
                "description": "Read a file",
                "input_schema": {"type": "object"},
            }
        ]
    )

    with TestClient(app) as client:
        response = client.post("/v1/messages", json=payload)

    assert response.status_code == 200
    assert response.json()["stop_reason"] == "tool_use"
    assert response.json()["content"][0]["type"] == "tool_use"
    assert response.json()["content"][0]["input"] == {"path": "README.md"}


def test_mock_backend_keeps_claude_streaming_and_tool_calls_available():
    app = create_api_app(mock_backend=True)
    headers = {"user-agent": "claude-code/2.0"}
    payload = _messages_payload(stream=True)

    with TestClient(app) as client:
        streamed = client.post("/v1/messages", json=payload, headers=headers)
        tool_response = client.post(
            "/v1/messages",
            json=_messages_payload(
                messages=[{"role": "user", "content": "Read README.md"}],
                tools=[
                    {
                        "name": "Read",
                        "description": "Read a file",
                        "input_schema": {
                            "type": "object",
                            "properties": {"path": {"type": "string"}},
                            "required": ["path"],
                        },
                    }
                ]
            ),
            headers=headers,
        )

    assert "event: content_block_delta" in streamed.text
    assert tool_response.json()["stop_reason"] == "tool_use"
    assert tool_response.json()["content"][0]["type"] == "tool_use"


def test_mock_backend_answers_conversational_messages_when_tools_are_available():
    app = create_api_app(mock_backend=True)
    payload = _messages_payload(
        messages=[{"role": "user", "content": "hello"}],
        tools=[
            {
                "name": "Read",
                "description": "Read a file",
                "input_schema": {"type": "object"},
            }
        ],
    )

    with TestClient(app) as client:
        response = client.post("/v1/messages", json=payload)

    body = response.json()
    assert body["stop_reason"] == "end_turn"
    assert "Xin chào" in body["content"][0]["text"]


def test_force_initial_tool_skips_conversational_prompt(monkeypatch):
    app = create_api_app(force_anthropic_initial_tool=True)
    session = _fake_session("normal conversational completion")
    _install_fake_session(monkeypatch, app.state.server, session)

    with TestClient(app) as client:
        response = client.post(
            "/v1/messages",
            json=_messages_payload(
                messages=[{"role": "user", "content": "toio laf ai"}],
                tools=[{"name": "Read", "input_schema": {"type": "object"}}],
            ),
        )

    assert response.status_code == 200
    assert response.json()["stop_reason"] == "end_turn"
    assert response.json()["content"] == [
        {"type": "text", "text": "normal conversational completion"}
    ]
    session.send.assert_awaited_once()


@pytest.mark.parametrize(
    ("error", "status", "error_type"),
    [
        (ValueError("bad request"), 400, "invalid_request_error"),
        (AuthRequired("not authenticated"), 401, "authentication_error"),
        (ConversationNotFound("missing"), 404, "not_found_error"),
        (RateLimited("slow down"), 429, "rate_limit_error"),
        (RuntimeError("broken"), 500, "api_error"),
    ],
)
def test_gate_5_errors_have_anthropic_schema(error, status, error_type, monkeypatch):
    app = create_api_app()
    monkeypatch.setattr(app.state.server, "complete_normalized", AsyncMock(side_effect=error))

    with TestClient(app) as client:
        response = client.post("/v1/messages", json=_messages_payload())

    assert response.status_code == status
    assert response.json() == {"type": "error", "error": {"type": error_type, "message": str(error)}}


@pytest.mark.anyio
async def test_gate_6_stream_disconnect_cancels_inflight_work(monkeypatch):
    app = create_api_app()
    server = app.state.server
    cancelled = asyncio.Event()

    async def never_complete(_adapted, stream_callback=None):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    request = MagicMock()
    request.is_disconnected = AsyncMock(return_value=False)
    monkeypatch.setattr(server, "_complete_anthropic", never_complete)
    adapted = parse_anthropic_request(_messages_payload(stream=True))
    stream = server._anthropic_live_stream(request, adapted)

    first = await anext(stream)
    await asyncio.sleep(0)
    await stream.aclose()

    assert "event: message_start" in first
    await asyncio.wait_for(cancelled.wait(), timeout=1)


@pytest.mark.anyio
async def test_anthropic_tool_stream_forwards_safe_text_before_completion(monkeypatch):
    app = create_api_app()
    server = app.state.server
    callback_called = asyncio.Event()
    release_completion = asyncio.Event()
    raw_text = "A progressively streamed response with tools available."

    async def complete(_adapted, stream_callback=None):
        assert stream_callback is not None
        await stream_callback(raw_text)
        callback_called.set()
        await release_completion.wait()
        return (
            {
                "id": "msg_original",
                "type": "message",
                "role": "assistant",
                "model": "claude-code-local",
                "content": [{"type": "text", "text": raw_text}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
            MagicMock(),
        )

    monkeypatch.setattr(server, "_complete_anthropic", complete)
    request = MagicMock()
    request.is_disconnected = AsyncMock(return_value=False)
    adapted = parse_anthropic_request(
        _messages_payload(
            stream=True,
            tools=[
                {
                    "name": "Read",
                    "description": "Read a file",
                    "input_schema": {"type": "object"},
                }
            ],
        )
    )
    stream = server._anthropic_live_stream(request, adapted)

    first = await anext(stream)
    next_event = asyncio.create_task(anext(stream))
    await asyncio.wait_for(callback_called.wait(), timeout=1)
    content_start = await asyncio.wait_for(next_event, timeout=1)
    progressive = await asyncio.wait_for(anext(stream), timeout=1)

    assert "event: message_start" in first
    assert "event: content_block_start" in content_start
    assert "event: content_block_delta" in progressive
    assert "progressively streamed" in progressive

    release_completion.set()
    await stream.aclose()


@pytest.mark.anyio
async def test_anthropic_tool_stream_keeps_tool_sentinel_out_of_text(monkeypatch):
    app = create_api_app()
    server = app.state.server
    raw_tool_call = '<WEBGPT_TOOL_CALL>{"name":"Read","arguments":{"path":"README.md"}}</WEBGPT_TOOL_CALL>'

    async def complete(_adapted, stream_callback=None):
        assert stream_callback is not None
        await stream_callback(raw_tool_call)
        return (
            {
                "id": "msg_original",
                "type": "message",
                "role": "assistant",
                "model": "claude-code-local",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "Read",
                        "input": {"path": "README.md"},
                    }
                ],
                "stop_reason": "tool_use",
                "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
            MagicMock(),
        )

    monkeypatch.setattr(server, "_complete_anthropic", complete)
    request = MagicMock()
    request.is_disconnected = AsyncMock(return_value=False)
    adapted = parse_anthropic_request(
        _messages_payload(
            stream=True,
            tools=[
                {
                    "name": "Read",
                    "description": "Read a file",
                    "input_schema": {"type": "object"},
                }
            ],
        )
    )

    body = "".join(
        [event async for event in server._anthropic_live_stream(request, adapted)]
    )

    assert "WEBGPT_TOOL_CALL" not in body
    assert '"type": "tool_use"' in body

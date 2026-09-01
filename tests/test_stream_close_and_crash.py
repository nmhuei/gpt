"""Regression gates for live-verify bugs found on 2026-08-24.

BUG-A: Claude Code's SDK hangs after HTTP 200 because an SSE stream must end
cleanly right after its terminator (``message_stop`` / ``data: [DONE]``); a
wedged backend used to leave the client on an endless ping treadmill.

BUG-B: a Playwright page crash ("Target crashed") surfaced as a raw exception
and produced a generic 500 instead of a retryable browser_disconnected
classification with ``x-should-retry: true``.
"""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.testclient import TestClient

from gpt.api.protocol_adapters import parse_anthropic_request
from gpt.api.server import create_api_app as create_standalone_app
from gpt.gateway import create_api_app as create_gateway_app
from gpt.state import BrowserDisconnected, EmptyModelResponse, SessionState
from gpt.transport.session import ChatGPTWebSession
from gpt.types import TurnResult


def _fake_session(text: str = "GATEWAY OK") -> MagicMock:
    session = MagicMock()
    session.conversation_id = None
    session.new_conversation = AsyncMock()
    session.open = AsyncMock()
    session.select_model = AsyncMock()
    session.select_reasoning_effort = AsyncMock()
    session.send = AsyncMock(
        return_value=TurnResult(turn_id="turn_1", conversation_id="conv_1", text=text)
    )
    return session


def _install_fake_session(monkeypatch, server, session: MagicMock) -> None:
    @asynccontextmanager
    async def lease(*args, affinity_key=None):
        yield session

    monkeypatch.setattr(server, "_lease_session", lease)
    monkeypatch.setattr(server.completion_runtime, "lease_session", lease)


def _install_mocked_standalone_session(monkeypatch, server, session: MagicMock) -> None:
    """The standalone (gpt.api) app leases via get_or_create_session."""
    monkeypatch.setattr(server, "get_or_create_session", AsyncMock(return_value=session))


def _messages_payload(**overrides: object) -> dict[str, object]:
    return {
        "model": "claude-code-local",
        "max_tokens": 64,
        "messages": [{"role": "user", "content": "hello"}],
        **overrides,
    }


# ---------------------------------------------------------------------------
# BUG-A: streams must reach EOF promptly after their terminator.
# ---------------------------------------------------------------------------


async def test_anthropic_stream_reaches_eof_right_after_message_stop(monkeypatch):
    app = create_gateway_app()
    server = app.state.server
    _install_fake_session(monkeypatch, server, _fake_session())

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://gateway") as client:
        lines: list[str] = []
        t_terminal: float | None = None
        t_eof: float | None = None
        async with client.stream(
            "POST", "/v1/messages", json=_messages_payload(stream=True)
        ) as response:
            assert response.status_code == 200
            async for line in response.aiter_lines():
                lines.append(line)
                if line.strip() == "event: message_stop" and t_terminal is None:
                    t_terminal = time.monotonic()
        t_eof = time.monotonic()

    events = [line.removeprefix("event: ") for line in lines if line.startswith("event: ")]
    assert events[-1] == "message_stop"
    tail = lines[lines.index("event: message_stop") :]
    assert not any(
        line.strip() == ": ping" or line.strip().startswith("event: ping") for line in tail
    ), tail
    assert t_terminal is not None
    # EOF must follow the terminator almost immediately (well under 1 second).
    assert t_eof - t_terminal < 1.0


async def test_openai_stream_reaches_eof_right_after_done(monkeypatch):
    app = create_standalone_app()
    server = app.state.server
    _install_mocked_standalone_session(monkeypatch, server, _fake_session())

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://gateway") as client:
        lines: list[str] = []
        t_terminal: float | None = None
        async with client.stream(
            "POST",
            "/v1/chat/completions",
            json={
                "model": "chatgpt-web",
                "messages": [{"role": "user", "content": "hello"}],
                "stream": True,
            },
        ) as response:
            assert response.status_code == 200
            async for line in response.aiter_lines():
                lines.append(line)
                if line.strip() == "data: [DONE]" and t_terminal is None:
                    t_terminal = time.monotonic()
        t_eof = time.monotonic()

    stripped = [line.strip() for line in lines if line.strip()]
    assert stripped[-1] == "data: [DONE]"
    assert t_terminal is not None
    assert t_eof - t_terminal < 1.0


async def test_wedged_backend_ends_stream_with_error_not_endless_ping(monkeypatch):
    """A backend that never completes must terminate the SSE stream bounded."""
    app = create_gateway_app()
    server = app.state.server
    server.stream_idle_seconds = 0.05
    server.stream_deadline_seconds = 0.3

    async def never_complete(_adapted, stream_callback=None):
        await asyncio.Event().wait()

    monkeypatch.setattr(server, "_complete_anthropic", never_complete)
    request = MagicMock()
    request.is_disconnected = AsyncMock(return_value=False)
    adapted = parse_anthropic_request(_messages_payload(stream=True))

    started = time.monotonic()
    events = [
        event async for event in server._anthropic_live_stream(request, adapted)
    ]
    duration = time.monotonic() - started

    assert events[0].startswith("event: message_start")
    # PING-WIRE: idle heartbeats are canonical ``event: ping`` frames now.
    ping_events = [event for event in events if event.strip().startswith("event: ping")]
    assert ping_events, events
    assert all(json.loads(p.split("\ndata: ", 1)[1]) == {"type": "ping"} for p in ping_events)
    # LATE-FAIL-SURFACE: nothing was delivered before the deadline fired, so
    # the failure is surfaced as a standard Anthropic ``event: error`` -- no
    # content is lost (none was sent) and the CLI learns the turn failed
    # instead of mistaking a dead stream for a completed turn.
    assert events[-1].startswith("event: error"), events[-1]
    error_events = [e for e in events if e.startswith("event: error")]
    assert len(error_events) == 1, events
    payload = json.loads(error_events[0].split("\ndata: ", 1)[1])
    assert payload["type"] == "error"
    assert set(payload["error"]) == {"type", "message"}
    # generation_timeout is not an Anthropic-contract type; it must collapse
    # to api_error so every SDK build can parse the frame.
    assert payload["error"]["type"] == "api_error"
    assert "exceeded" in payload["error"]["message"], payload["error"]
    # No content block was ever opened and no clean terminator follows.
    assert not any(e.startswith("event: content_block") for e in events), events
    assert not any(
        e.startswith("event: message_delta") or e.startswith("event: message_stop")
        for e in events
    ), events
    # Nothing was masked (nothing had been delivered), so the counter stays 0.
    assert server.late_failure_masked == 0
    # No ping may follow the terminal error event.
    error_index = len(events) - 1
    assert not any(
        event.strip() == ": ping" or event.strip().startswith("event: ping")
        for event in events[error_index + 1 :]
    )
    assert duration < 5.0


async def test_late_failure_after_content_keeps_clean_close_and_counts(monkeypatch):
    """LATE-FAIL-SURFACE: once content has been streamed, R4-DOUBLING's clean
    end_turn close is kept verbatim (an error event would make the SDK/CLI
    re-POST and duplicate partial output) -- but the masking is counted."""
    app = create_gateway_app()
    server = app.state.server

    async def stream_then_fail(_adapted, stream_callback=None):
        assert stream_callback is not None
        await stream_callback("partial answer")
        raise EmptyModelResponse("upstream collapsed mid-generation")

    monkeypatch.setattr(server, "_complete_anthropic", stream_then_fail)
    request = MagicMock()
    request.is_disconnected = AsyncMock(return_value=False)
    adapted = parse_anthropic_request(_messages_payload(stream=True))

    events = [
        event async for event in server._anthropic_live_stream(request, adapted)
    ]

    assert events[0].startswith("event: message_start")
    assert not any(event.startswith("event: error") for event in events), events
    assert events[-1].startswith("event: message_stop"), events[-1]
    message_delta = next(
        event for event in events if event.startswith("event: message_delta")
    )
    assert '"stop_reason": "end_turn"' in message_delta
    joined = "".join(events)
    assert "partial answer" in joined
    assert "[webgpt-gateway:" in joined
    starts = [e for e in events if e.startswith("event: content_block_start")]
    stops = [e for e in events if e.startswith("event: content_block_stop")]
    assert len(starts) == len(stops) == 1
    # The truncated turn was masked behind a completed-turn close: counted.
    assert server.late_failure_masked == 1


# ---------------------------------------------------------------------------
# BUG-B: "Target crashed" becomes retryable browser_disconnected, not 500.
# ---------------------------------------------------------------------------


def _crash_message() -> str:
    return "Locator.count: Target crashed"


def _session_whose_driver_crashes(marker: str = _crash_message()) -> ChatGPTWebSession:
    page = MagicMock()
    page.is_closed.return_value = False
    manager = MagicMock()
    manager.connected = True
    session = ChatGPTWebSession(manager, page, owns_browser_manager=False)
    driver = MagicMock()
    driver.conversation_id.return_value = None
    driver.history = AsyncMock(side_effect=RuntimeError(marker))
    session.ui_driver = driver
    return session


async def test_session_reconcile_classifies_page_crash_as_browser_disconnected():
    session = _session_whose_driver_crashes()

    with pytest.raises(BrowserDisconnected):
        await session.reconcile("hello")

    assert session.state == SessionState.BROWSER_DISCONNECTED


async def test_session_history_classifies_page_crash_instead_of_stale_cache():
    session = _session_whose_driver_crashes()

    with pytest.raises(BrowserDisconnected):
        await session.history()

    assert session.state == SessionState.BROWSER_DISCONNECTED


def test_target_crashed_maps_to_retryable_503_on_chat_completions(monkeypatch):
    app = create_standalone_app()
    server = app.state.server
    session = _fake_session()
    session.send = AsyncMock(side_effect=RuntimeError(_crash_message()))
    _install_mocked_standalone_session(monkeypatch, server, session)

    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "chatgpt-web",
                "messages": [{"role": "user", "content": "crash probe"}],
            },
        )

    assert response.status_code == 503
    body = response.json()["error"]
    assert body["code"] == "browser_disconnected"
    assert body["retryable"] is True
    assert response.headers["x-should-retry"] == "true"


def test_target_crashed_maps_to_retryable_503_on_anthropic_messages(monkeypatch):
    app = create_standalone_app()
    server = app.state.server
    session = _fake_session()
    session.send = AsyncMock(side_effect=RuntimeError(_crash_message()))
    _install_mocked_standalone_session(monkeypatch, server, session)

    with TestClient(app) as client:
        response = client.post("/v1/messages", json=_messages_payload())

    assert response.status_code == 503
    assert response.json()["type"] == "error"
    assert response.headers["x-should-retry"] == "true"

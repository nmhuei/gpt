from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.testclient import TestClient

from gpt.api.server import create_api_app
from gpt.state import (
    AnonymousSessionUnavailable,
    BrowserDisconnected,
    CommitUnknown,
    GenerationTimeout,
    RateLimited,
    UIChanged,
)
from gpt.types import TurnResult

BASH_TOOL = {
    "type": "function",
    "function": {
        "name": "Bash",
        "parameters": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
            "additionalProperties": False,
        },
    },
}

AGENT_TOOL = {
    "type": "function",
    "function": {
        "name": "Agent",
        "description": "Launch a new agent for an independent task.",
        "parameters": {
            "type": "object",
            "properties": {
                "description": {"type": "string"},
                "prompt": {"type": "string"},
                "subagent_type": {"type": "string"},
                "run_in_background": {"type": "boolean"},
            },
            "required": ["description", "prompt"],
            "additionalProperties": False,
        },
    },
}


def _fake_session() -> MagicMock:
    session = MagicMock()
    session.conversation_id = None
    session.new_conversation = AsyncMock()
    session.open = AsyncMock()
    session.select_model = AsyncMock()
    session.select_reasoning_effort = AsyncMock()
    return session


@pytest.mark.parametrize(
    ("exception", "status", "code", "retryable"),
    [
        (GenerationTimeout("slow model"), 504, "generation_timeout", True),
        (CommitUnknown("post-submit uncertainty"), 409, "commit_unknown", False),
        (RateLimited("free quota reached"), 429, "rate_limit", False),
        (UIChanged("composer changed"), 503, "web_ui_changed", True),
        (BrowserDisconnected("chromium exited"), 503, "browser_disconnected", True),
    ],
)
def test_runtime_faults_map_to_typed_retryability(
    monkeypatch, exception, status, code, retryable
):
    app = create_api_app()
    server = app.state.server
    session = _fake_session()
    session.send = AsyncMock(side_effect=exception)
    monkeypatch.setattr(server, "get_or_create_session", AsyncMock(return_value=session))

    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "chatgpt-web",
                "messages": [{"role": "user", "content": "fault injection"}],
            },
        )

    assert response.status_code == status
    assert response.json()["error"]["code"] == code
    assert response.json()["error"]["retryable"] is retryable
    assert response.headers["x-should-retry"] == ("true" if retryable else "false")


def test_empty_assistant_response_fails_closed(monkeypatch):
    app = create_api_app()
    server = app.state.server
    session = _fake_session()
    session.send = AsyncMock(
        return_value=TurnResult(
            turn_id="turn_empty",
            conversation_id="conv_empty",
            text="   ",
        )
    )
    monkeypatch.setattr(server, "get_or_create_session", AsyncMock(return_value=session))

    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "chatgpt-web",
                "messages": [{"role": "user", "content": "reply"}],
            },
        )

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "empty_model_response"
    assert response.json()["error"]["retryable"] is True


@pytest.mark.anyio
async def test_require_anonymous_rejects_authenticated_browser(monkeypatch):
    app = create_api_app(require_anonymous=True, max_workers=1)
    server = app.state.server
    session = _fake_session()
    session.ui_driver = MagicMock()
    session.ui_driver.auth_status = AsyncMock(return_value="authenticated")
    session.close = AsyncMock()
    create = AsyncMock(return_value=session)
    monkeypatch.setattr("gpt.api.server.ChatGPTWebSession.create", create)

    with pytest.raises(AnonymousSessionUnavailable):
        await server.get_or_create_session()

    session.close.assert_awaited_once()


def test_require_anonymous_rechecks_existing_session_and_fails_closed(monkeypatch):
    app = create_api_app(require_anonymous=True, max_workers=1)
    server = app.state.server
    session = _fake_session()
    session.ui_driver = MagicMock()
    session.ui_driver.auth_status = AsyncMock(side_effect=["anonymous", "authenticated"])
    session.close = AsyncMock()
    create = AsyncMock(return_value=session)
    monkeypatch.setattr("gpt.api.server.ChatGPTWebSession.create", create)

    with TestClient(app) as client:
        response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json()["backend"] == "AnonymousSessionUnavailable"
    session.close.assert_awaited_once()
    assert server._session is None


@pytest.mark.anyio
async def test_rate_limit_discards_anonymous_session_for_fresh_browser_retry():
    app = create_api_app(require_anonymous=True, max_workers=1)
    server = app.state.server
    session = _fake_session()
    session.ui_driver = MagicMock()
    session.ui_driver.auth_status = AsyncMock(return_value="anonymous")
    session.close = AsyncMock()
    server._session = session

    with pytest.raises(RateLimited):
        async with server._lease_session() as leased:
            assert leased is session
            raise RateLimited("quota reached")

    assert server._session is None
    session.close.assert_awaited_once()


def _tool_block(commands: list[str]) -> str:
    body = []
    for command in commands:
        body.append(
            "  <invoke name=\"Bash\">\n"
            f"    <parameter name=\"command\"><![CDATA[{command}]]></parameter>\n"
            "  </invoke>"
        )
    return "<tool_calls>\n" + "\n".join(body) + "\n</tool_calls>"


def _agent_fanout_block() -> str:
    return (
        '<tool_calls>\n'
        '  <invoke name="Agent">\n'
        '    <parameter name="description"><![CDATA[OSINT methodology]]></parameter>\n'
        '    <parameter name="prompt"><![CDATA[Research a general OSINT challenge workflow.]]></parameter>\n'
        '    <parameter name="subagent_type"><![CDATA[general-purpose]]></parameter>\n'
        '    <parameter name="run_in_background"><![CDATA[true]]></parameter>\n'
        '  </invoke>\n'
        '  <invoke name="Agent">\n'
        '    <parameter name="description"><![CDATA[OSINT geolocation]]></parameter>\n'
        '    <parameter name="prompt"><![CDATA[Research geolocation techniques for OSINT challenges.]]></parameter>\n'
        '    <parameter name="subagent_type"><![CDATA[general-purpose]]></parameter>\n'
        '    <parameter name="run_in_background"><![CDATA[true]]></parameter>\n'
        '  </invoke>\n'
        '</tool_calls>'
    )


def test_fanout_request_rejects_prose_then_accepts_parallel_agent_calls(monkeypatch):
    app = create_api_app()
    server = app.state.server
    session = _fake_session()
    session.send = AsyncMock(
        side_effect=[
            TurnResult(
                turn_id="turn_fanout_prose",
                conversation_id="conv_fanout",
                text=(
                    "Mình sẽ dùng skill OSINT/CTF rồi fan-out vài hướng nghiên cứu "
                    "độc lập để tổng hợp cách tiếp cận."
                ),
            ),
            TurnResult(
                turn_id="turn_fanout_agents",
                conversation_id="conv_fanout",
                text=_agent_fanout_block(),
            ),
        ]
    )
    monkeypatch.setattr(server, "get_or_create_session", AsyncMock(return_value=session))

    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "chatgpt-web",
                "messages": [
                    {
                        "role": "user",
                        "content": "fan out subagents research cách làm 1 bài osint",
                    }
                ],
                "tools": [AGENT_TOOL],
            },
        )

    assert response.status_code == 200
    calls = response.json()["choices"][0]["message"]["tool_calls"]
    assert [call["function"]["name"] for call in calls] == ["Agent", "Agent"]
    assert session.send.await_count == 2
    corrections = [
        event
        for event in server.trace.snapshot()
        if event.component == "completionruntime" and event.kind == "tool_correction"
    ]
    assert [event.metadata.get("reason") for event in corrections] == ["FALSE_COMPLETION"]


def test_fanout_request_rejects_single_agent_then_accepts_parallel_agents(monkeypatch):
    app = create_api_app()
    server = app.state.server
    session = _fake_session()
    single_agent = (
        '<tool_calls><invoke name="Agent">'
        '<parameter name="description"><![CDATA[OSINT methodology]]></parameter>'
        '<parameter name="prompt"><![CDATA[Research OSINT methodology.]]></parameter>'
        '</invoke></tool_calls>'
    )
    session.send = AsyncMock(
        side_effect=[
            TurnResult(
                turn_id="turn_one_agent",
                conversation_id="conv_fanout_single",
                text=single_agent,
            ),
            TurnResult(
                turn_id="turn_two_agents",
                conversation_id="conv_fanout_single",
                text=_agent_fanout_block(),
            ),
        ]
    )
    monkeypatch.setattr(server, "get_or_create_session", AsyncMock(return_value=session))

    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "chatgpt-web",
                "messages": [
                    {
                        "role": "user",
                        "content": "fan out subagents research cách làm 1 bài osint",
                    }
                ],
                "tools": [AGENT_TOOL],
            },
        )

    assert response.status_code == 200
    calls = response.json()["choices"][0]["message"]["tool_calls"]
    assert [call["function"]["name"] for call in calls] == ["Agent", "Agent"]
    assert session.send.await_count == 2
    corrections = [
        event
        for event in server.trace.snapshot()
        if event.component == "completionruntime" and event.kind == "tool_correction"
    ]
    assert [event.metadata.get("reason") for event in corrections] == ["INCOMPLETE_FANOUT"]


def test_sixteen_tool_calls_are_reduced_to_one_bounded_correction(monkeypatch):
    app = create_api_app()
    server = app.state.server
    session = _fake_session()
    session.send = AsyncMock(
        side_effect=[
            TurnResult(
                turn_id="turn_many",
                conversation_id="conv_many",
                text=_tool_block([f"printf {index}" for index in range(16)]),
            ),
            TurnResult(
                turn_id="turn_one",
                conversation_id="conv_many",
                text=_tool_block(["pwd"]),
            ),
        ]
    )
    monkeypatch.setattr(server, "get_or_create_session", AsyncMock(return_value=session))

    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "chatgpt-web",
                "messages": [{"role": "user", "content": "Use Bash once."}],
                "tools": [BASH_TOOL],
            },
        )

    assert response.status_code == 200
    calls = response.json()["choices"][0]["message"]["tool_calls"]
    assert len(calls) == 1
    assert session.send.await_count == 2
    assert any(
        event.component == "completionruntime"
        and event.kind == "tool_correction"
        and event.metadata.get("reason") == "MULTI_TOOL"
        for event in server.trace.snapshot()
    )


def test_malformed_tool_markup_gets_one_correction_then_valid_call(monkeypatch):
    app = create_api_app()
    server = app.state.server
    session = _fake_session()
    session.send = AsyncMock(
        side_effect=[
            TurnResult(
                turn_id="turn_bad",
                conversation_id="conv_bad",
                text='<tool_calls><invoke name="Bash"><parameter name="command">pwd</parameter>',
            ),
            TurnResult(
                turn_id="turn_fixed",
                conversation_id="conv_bad",
                text=_tool_block(["pwd"]),
            ),
        ]
    )
    monkeypatch.setattr(server, "get_or_create_session", AsyncMock(return_value=session))

    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "chatgpt-web",
                "messages": [{"role": "user", "content": "Use Bash to run pwd."}],
                "tools": [BASH_TOOL],
            },
        )

    assert response.status_code == 200
    assert response.json()["choices"][0]["finish_reason"] == "tool_calls"
    assert session.send.await_count == 2
    assert any(
        event.component == "completionruntime"
        and event.kind == "tool_correction"
        and event.metadata.get("reason") == "MALFORMED_TOOL"
        for event in server.trace.snapshot()
    )

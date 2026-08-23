import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.testclient import TestClient

from gpt.api.server import create_api_app
from gpt.types import TurnResult


def test_api_server_health_and_models():
    app = create_api_app(headless=True)
    client = TestClient(app)

    # 1. Healthz
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"

    # 2. Models
    resp = client.get("/v1/models")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert any(m["id"] == "chatgpt-web" for m in data)


@pytest.mark.anyio
async def test_chat_completions_tool_call_flow(monkeypatch):
    app = create_api_app(headless=True)

    # Mock the canonical session boundary, not its UI implementation.
    mock_turn_result = TurnResult(
        turn_id="turn_test123",
        conversation_id="conv_123",
        text='<WEBGPT_TOOL_CALL>\n{"name":"get_weather","arguments":{"location":"Hanoi"}}\n</WEBGPT_TOOL_CALL>',
        status="completed",
        duration_ms=1200,
    )

    mock_send = AsyncMock(return_value=mock_turn_result)
    server_instance = app.routes[0].endpoint.__self__
    mock_session = MagicMock()
    mock_session.new_conversation = AsyncMock()
    mock_session.send = mock_send
    mock_session.drain_events = MagicMock(return_value=[])
    mock_session.conversation_id = None

    monkeypatch.setattr(server_instance, "get_or_create_session", AsyncMock(return_value=mock_session))

    client = TestClient(app)

    request_payload = {
        "model": "chatgpt-web",
        "messages": [{"role": "user", "content": "Thời tiết ở Hà Nội thế nào?"}],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "description": "Lấy thông tin thời tiết",
                    "parameters": {
                        "type": "object",
                        "properties": {"location": {"type": "string"}},
                    },
                },
            }
        ],
    }

    resp = client.post("/v1/chat/completions", json=request_payload)
    assert resp.status_code == 200
    data = resp.json()

    assert data["choices"][0]["finish_reason"] == "tool_calls"
    tool_calls = data["choices"][0]["message"]["tool_calls"]
    assert len(tool_calls) == 1
    assert tool_calls[0]["function"]["name"] == "get_weather"
    assert json.loads(tool_calls[0]["function"]["arguments"]) == {"location": "Hanoi"}
    # Freshly booted blank sessions should not pay an extra New Chat click.
    mock_session.new_conversation.assert_not_awaited()
    mock_session.send.assert_awaited_once()


@pytest.mark.anyio
async def test_gateway_resolves_configured_model_alias_once_before_sending(monkeypatch):
    app = create_api_app(model_aliases={"coding": "GPT Coding"})
    server = app.state.server
    session = MagicMock()
    session.new_conversation = AsyncMock()
    session.select_model = AsyncMock()
    session.select_reasoning_effort = AsyncMock()
    session.send = AsyncMock(
        return_value=TurnResult(
            turn_id="turn_alias",
            conversation_id="conv_alias",
            text="done",
        )
    )
    monkeypatch.setattr(server, "get_or_create_session", AsyncMock(return_value=session))

    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "coding",
                "messages": [{"role": "user", "content": "write code"}],
                "reasoning_effort": "high",
            },
        )

    assert response.status_code == 200
    assert response.json()["model"] == "coding"
    session.select_model.assert_awaited_once_with("GPT Coding")
    session.select_reasoning_effort.assert_awaited_once_with("high")
    session.send.assert_awaited_once()
    assert len(session.send.await_args.args) == 1


@pytest.mark.anyio
async def test_commit_unknown_retry_reconciles_persisted_turn_without_resend(monkeypatch):
    from gpt.state import CommitUnknown
    from gpt.types import ReconciliationResult

    app = create_api_app(headless=True)
    server = app.state.server
    session = MagicMock()
    session.conversation_id = "conv_uncertain"
    session.new_conversation = AsyncMock()
    session.open = AsyncMock()
    session.select_model = AsyncMock()
    session.select_reasoning_effort = AsyncMock()
    session.send = AsyncMock(
        side_effect=CommitUnknown(
            "submitted but completion unknown",
            conversation_id="conv_uncertain",
        )
    )
    session.reconcile = AsyncMock(
        return_value=ReconciliationResult(
            user_turn_present=True,
            assistant_text="recovered answer",
            conversation_id="conv_uncertain",
        )
    )
    monkeypatch.setattr(server, "get_or_create_session", AsyncMock(return_value=session))

    payload = {
        "model": "chatgpt-web",
        "messages": [{"role": "user", "content": "exactly-once marker"}],
    }
    with TestClient(app) as client:
        first = client.post("/v1/chat/completions", json=payload)
        assert first.status_code == 409
        assert first.json()["error"]["code"] == "commit_unknown"

        second = client.post("/v1/chat/completions", json=payload)

    assert second.status_code == 200
    assert second.json()["choices"][0]["message"]["content"] == "recovered answer"
    assert session.send.await_count == 1
    session.reconcile.assert_awaited_once()


@pytest.mark.anyio
async def test_commit_unknown_retry_resends_once_only_after_history_proves_absence(monkeypatch):
    from gpt.state import CommitUnknown
    from gpt.types import ReconciliationResult

    app = create_api_app(headless=True)
    server = app.state.server
    session = MagicMock()
    session.conversation_id = "conv_absent"
    session.new_conversation = AsyncMock()
    session.open = AsyncMock()
    session.select_model = AsyncMock()
    session.select_reasoning_effort = AsyncMock()
    session.send = AsyncMock(
        side_effect=[
            CommitUnknown(
                "submitted but completion unknown",
                conversation_id="conv_absent",
            ),
            TurnResult(
                turn_id="turn_retry",
                conversation_id="conv_absent",
                text="safe retry answer",
            ),
        ]
    )
    session.reconcile = AsyncMock(
        return_value=ReconciliationResult(
            user_turn_present=False,
            assistant_text=None,
            conversation_id="conv_absent",
        )
    )
    monkeypatch.setattr(server, "get_or_create_session", AsyncMock(return_value=session))

    payload = {
        "model": "chatgpt-web",
        "messages": [{"role": "user", "content": "absence marker"}],
    }
    with TestClient(app) as client:
        first = client.post("/v1/chat/completions", json=payload)
        assert first.status_code == 409
        second = client.post("/v1/chat/completions", json=payload)

    assert second.status_code == 200
    assert second.json()["choices"][0]["message"]["content"] == "safe retry answer"
    assert session.send.await_count == 2
    session.reconcile.assert_awaited_once()


@pytest.mark.anyio
async def test_server_max_workers_path_leases_factory_session(monkeypatch):
    from contextlib import asynccontextmanager

    from gpt.api.requests import parse_chat_completion_request

    app = create_api_app(max_workers=2, warm_workers=1)
    server = app.state.server
    session = MagicMock()
    session.conversation_id = None
    session.new_conversation = AsyncMock()
    session.open = AsyncMock()
    session.select_model = AsyncMock()
    session.select_reasoning_effort = AsyncMock()
    session.send = AsyncMock(
        return_value=TurnResult(
            turn_id="turn_pool",
            conversation_id="conv_pool",
            text="pooled answer",
        )
    )

    class FakeFactory:
        def __init__(self):
            self.lease_count = 0
            self.close = AsyncMock()

        @asynccontextmanager
        async def lease(self):
            self.lease_count += 1
            yield session

    fake_factory = FakeFactory()
    server._worker_factory = fake_factory

    normalized = parse_chat_completion_request(
        {
            "model": "chatgpt-web",
            "messages": [{"role": "user", "content": "pool marker"}],
        }
    )
    response, record = await server.complete_normalized(normalized)

    assert response["choices"][0]["message"]["content"] == "pooled answer"
    assert record.conversation_id == "conv_pool"
    assert fake_factory.lease_count == 1
    session.send.assert_awaited_once()
    await server.close()


def test_healthz_is_pure_liveness_and_does_not_boot_browser():
    app = create_api_app(headless=True)
    server = app.state.server
    with TestClient(app) as client:
        response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"ok": True, "status": "ok"}
    assert server._session is None


@pytest.mark.anyio
async def test_readyz_checks_a_real_session_lease_without_sending(monkeypatch):
    from contextlib import asynccontextmanager

    app = create_api_app(headless=True)
    server = app.state.server
    session = MagicMock()
    session.state.value = "ready"
    session.browser_manager.connected = True
    session.ui_driver.auth_status = AsyncMock(return_value="anonymous")

    @asynccontextmanager
    async def lease():
        yield session

    monkeypatch.setattr(server, "_lease_session", lease)
    request = MagicMock()
    response = await server.readiness(request)
    assert response.status_code == 200
    payload = json.loads(bytes(response.body))
    assert payload["ready"] is True
    assert payload["auth_status"] == "anonymous"
    assert not hasattr(session, "send") or session.send.call_count == 0


@pytest.mark.anyio
async def test_two_logical_sessions_use_distinct_factory_workers_without_cross_routing():
    import asyncio
    from contextlib import asynccontextmanager

    from gpt.api.requests import parse_chat_completion_request

    app = create_api_app(max_workers=2, warm_workers=2)
    server = app.state.server

    class RoutedSession:
        def __init__(self, name):
            self.name = name
            self.conversation_id = None
            self.new_conversation = AsyncMock()
            self.open = AsyncMock()
            self.select_model = AsyncMock()
            self.select_reasoning_effort = AsyncMock()

        async def send(self, prompt, timeout_seconds=None):
            await asyncio.sleep(0.02)
            marker = "alpha" if "alpha" in prompt else "beta"
            self.conversation_id = f"conv_{marker}"
            return TurnResult(
                turn_id=f"turn_{marker}",
                conversation_id=self.conversation_id,
                text=f"answer_{marker}_via_{self.name}",
            )

    sessions = [RoutedSession("worker_a"), RoutedSession("worker_b")]

    class FakeFactory:
        def __init__(self):
            self.next_index = 0
            self.active = 0
            self.peak = 0

        @asynccontextmanager
        async def lease(self):
            session = sessions[self.next_index]
            self.next_index += 1
            self.active += 1
            self.peak = max(self.peak, self.active)
            try:
                yield session
            finally:
                self.active -= 1

    factory = FakeFactory()
    server._worker_factory = factory

    alpha = parse_chat_completion_request(
        {"model": "chatgpt-web", "messages": [{"role": "user", "content": "alpha"}]}
    )
    beta = parse_chat_completion_request(
        {"model": "chatgpt-web", "messages": [{"role": "user", "content": "beta"}]}
    )
    (alpha_result, alpha_record), (beta_result, beta_record) = await asyncio.gather(
        server.complete_normalized(alpha),
        server.complete_normalized(beta),
    )

    assert factory.peak == 2
    assert alpha_record.session_id != beta_record.session_id
    assert alpha_record.conversation_id == "conv_alpha"
    assert beta_record.conversation_id == "conv_beta"
    assert "alpha" in alpha_result["choices"][0]["message"]["content"]
    assert "beta" in beta_result["choices"][0]["message"]["content"]
    assert "beta" not in alpha_result["choices"][0]["message"]["content"]
    assert "alpha" not in beta_result["choices"][0]["message"]["content"]


def _parse_openai_sse(body: str):
    events = []
    done = 0
    for block in body.split("\n\n"):
        if not block.startswith("data: "):
            continue
        payload = block[6:]
        if payload == "[DONE]":
            done += 1
        else:
            events.append(json.loads(payload))
    return events, done


def test_openai_stream_contract_has_role_stable_id_finish_and_single_done(monkeypatch):
    app = create_api_app()
    server = app.state.server
    session = MagicMock()
    session.conversation_id = None
    session.new_conversation = AsyncMock()
    session.open = AsyncMock()
    session.select_model = AsyncMock()
    session.select_reasoning_effort = AsyncMock()
    session.send = AsyncMock(
        return_value=TurnResult(
            turn_id="stream_text",
            conversation_id="conv_stream_text",
            text="stream contract text",
        )
    )
    monkeypatch.setattr(server, "get_or_create_session", AsyncMock(return_value=session))

    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "chatgpt-web",
                "messages": [{"role": "user", "content": "stream"}],
                "stream": True,
            },
        )

    assert response.status_code == 200
    events, done = _parse_openai_sse(response.text)
    assert done == 1
    assert events[0]["choices"][0]["delta"] == {"role": "assistant"}
    ids = {event["id"] for event in events}
    assert len(ids) == 1
    content = "".join(
        event["choices"][0]["delta"].get("content", "") for event in events
    )
    assert content == "stream contract text"
    assert events[-1]["choices"][0]["finish_reason"] == "stop"
    assert events[-1]["choices"][0]["delta"] == {}


def test_openai_tool_stream_never_leaks_sentinel_and_finishes_separately(monkeypatch):
    app = create_api_app()
    server = app.state.server
    session = MagicMock()
    session.conversation_id = None
    session.new_conversation = AsyncMock()
    session.open = AsyncMock()
    session.select_model = AsyncMock()
    session.select_reasoning_effort = AsyncMock()
    raw = (
        '<WEBGPT_TOOL_CALL>\n{"name":"lookup","arguments":{"q":"x"}}\n'
        "</WEBGPT_TOOL_CALL>"
    )
    session.send = AsyncMock(
        return_value=TurnResult(
            turn_id="stream_tool",
            conversation_id="conv_stream_tool",
            text=raw,
        )
    )
    tools = [
        {
            "type": "function",
            "function": {
                "name": "lookup",
                "parameters": {
                    "type": "object",
                    "properties": {"q": {"type": "string"}},
                    "required": ["q"],
                },
            },
        }
    ]
    monkeypatch.setattr(server, "get_or_create_session", AsyncMock(return_value=session))

    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "chatgpt-web",
                "messages": [{"role": "user", "content": "lookup"}],
                "tools": tools,
                "stream": True,
            },
        )

    events, done = _parse_openai_sse(response.text)
    assert done == 1
    serialized = response.text
    assert "WEBGPT_TOOL_CALL" not in serialized
    tool_events = [
        event
        for event in events
        if event["choices"][0]["delta"].get("tool_calls")
    ]
    assert len(tool_events) == 1
    assert tool_events[0]["choices"][0]["finish_reason"] is None
    assert tool_events[0]["choices"][0]["delta"]["tool_calls"][0]["function"]["name"] == "lookup"
    assert events[-1]["choices"][0]["finish_reason"] == "tool_calls"
    assert events[-1]["choices"][0]["delta"] == {}


@pytest.mark.anyio
async def test_lifespan_prewarm_failure_keeps_app_startable(monkeypatch):
    from starlette.testclient import TestClient

    app = create_api_app(prewarm=True)
    server = app.state.server
    monkeypatch.setattr(server, "get_or_create_session", AsyncMock(side_effect=RuntimeError("warmup transient")))

    with TestClient(app) as client:
        response = client.get("/healthz")

    assert response.status_code == 200
    assert any(
        event.component == "webchat" and event.kind == "prewarm_failed"
        for event in server.trace.snapshot()
    )


@pytest.mark.anyio
async def test_multiple_tool_calls_are_corrected_to_single_invoke(monkeypatch):
    app = create_api_app(headless=True)
    server = app.state.server
    session = MagicMock()
    session.conversation_id = None
    session.new_conversation = AsyncMock()
    session.open = AsyncMock()
    session.select_model = AsyncMock()
    session.select_reasoning_effort = AsyncMock()
    session.send = AsyncMock(
        side_effect=[
            TurnResult(
                turn_id="turn_many",
                conversation_id="conv_many",
                text=(
                    '<tool_calls><invoke name="get_weather"><parameter name="location"><![CDATA[Hanoi]]></parameter>'
                    '</invoke><invoke name="get_weather"><parameter name="location"><![CDATA[Hue]]></parameter>'
                    '</invoke></tool_calls>'
                ),
            ),
            TurnResult(
                turn_id="turn_one",
                conversation_id="conv_many",
                text='<tool_calls><invoke name="get_weather"><parameter name="location"><![CDATA[Hanoi]]></parameter></invoke></tool_calls>',
            ),
        ]
    )
    monkeypatch.setattr(server, "get_or_create_session", AsyncMock(return_value=session))

    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "chatgpt-web",
                "messages": [{"role": "user", "content": "call one tool at a time"}],
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "get_weather",
                            "parameters": {
                                "type": "object",
                                "properties": {"location": {"type": "string"}},
                                "required": ["location"],
                                "additionalProperties": False,
                            },
                        },
                    }
                ],
            },
        )

    assert response.status_code == 200
    data = response.json()
    calls = data["choices"][0]["message"]["tool_calls"]
    assert len(calls) == 1
    assert json.loads(calls[0]["function"]["arguments"]) == {"location": "Hanoi"}
    assert session.send.await_count == 2
    assert any(
        event.component == "completionruntime"
        and event.kind == "tool_correction"
        and event.metadata.get("reason") == "MULTI_TOOL"
        and event.metadata.get("correction_index") == 1
        for event in server.trace.snapshot()
    )


@pytest.mark.anyio
async def test_server_passes_configured_generation_timeout_to_session(monkeypatch):
    app = create_api_app(headless=True, generation_timeout_seconds=7.5)
    server = app.state.server
    session = MagicMock()
    session.conversation_id = None
    session.new_conversation = AsyncMock()
    session.open = AsyncMock()
    session.select_model = AsyncMock()
    session.select_reasoning_effort = AsyncMock()
    session.send = AsyncMock(
        return_value=TurnResult(
            turn_id="turn_timeout_cfg",
            conversation_id="conv_timeout_cfg",
            text="configured timeout ok",
        )
    )
    monkeypatch.setattr(server, "get_or_create_session", AsyncMock(return_value=session))

    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            json={"model": "chatgpt-web", "messages": [{"role": "user", "content": "hi"}]},
        )

    assert response.status_code == 200
    session.send.assert_awaited_once()
    assert session.send.await_args.kwargs["timeout_seconds"] == 7.5


@pytest.mark.anyio
async def test_tool_correction_budget_is_bounded_and_returns_structured_error(monkeypatch):
    app = create_api_app(headless=True)
    server = app.state.server
    session = MagicMock()
    session.conversation_id = None
    session.new_conversation = AsyncMock()
    session.open = AsyncMock()
    session.select_model = AsyncMock()
    session.select_reasoning_effort = AsyncMock()
    refusal = TurnResult(
        turn_id="turn_refusal",
        conversation_id="conv_refusal",
        text="I cannot execute that tool because tools are unavailable.",
    )
    session.send = AsyncMock(side_effect=[refusal, refusal, refusal])
    monkeypatch.setattr(server, "get_or_create_session", AsyncMock(return_value=session))

    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "chatgpt-web",
                "messages": [{"role": "user", "content": "Use Bash to run pwd"}],
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "Bash",
                            "parameters": {
                                "type": "object",
                                "properties": {"command": {"type": "string"}},
                                "required": ["command"],
                            },
                        },
                    }
                ],
            },
        )

    assert response.status_code == 502
    payload = response.json()["error"]
    assert payload["code"] == "malformed_model_tool_call"
    assert payload["retryable"] is False
    assert "correction budget exhausted" in payload["message"].lower()
    assert session.send.await_count == 3
    corrections = [
        event
        for event in server.trace.snapshot()
        if event.component == "completionruntime" and event.kind == "tool_correction"
    ]
    assert [event.metadata["correction_index"] for event in corrections] == [1, 2]


def test_lifespan_shutdown_is_bounded(monkeypatch):
    app = create_api_app(headless=True)

    async def slow_close():
        await asyncio.sleep(60)

    app.state.server.close = slow_close
    monkeypatch.setenv("WEBGPT_SERVER_CLOSE_TIMEOUT", "0.01")

    with TestClient(app) as client:
        response = client.get("/healthz")

    assert response.status_code == 200
